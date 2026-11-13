"""
AiAS Memory service — the NEDB connector.

A small FastAPI service that holds all three AiAS memory types in NEDB
and exposes them over HTTP:

  POST /v1/chat/completions   OpenAI-shaped completions with memory
                              assembly in front of your upstream model
                              (or, with no upstream configured, a demo
                              mode that returns the assembled prompt).
  POST /v1/memory/assemble    The adapter surface: assemble one turn's
                              messages without calling a model.
  POST /v1/memory/extract     The adapter surface: record a finished
                              turn and run fact extraction over it.
  GET  /v1/memory/facts       Read a session's stored facts.
  GET  /v1/memory/stats       Session / workspace memory statistics.
  GET  /health

This HTTP contract is the surface other agents integrate against — the
OpenClaw context-engine plugin in `openclaw/` is built on exactly these
two /v1/memory/* calls.

Environment:
  AIAS_MEMORY_NEDB_PATH   NEDB database directory
                          (default: ./aias-memory-data)
  AIAS_MEMORY_UPSTREAM    OpenAI-compatible base URL of your model
                          provider, e.g. https://api.openai.com/v1
  AIAS_MEMORY_API_KEY     API key for the upstream / extraction model
  AIAS_MEMORY_HOST        Bind host (default 127.0.0.1)
  AIAS_MEMORY_PORT        Bind port (default 8000)
  NEDB_PYTHON_PATH        Path to the NEDB engine's python/ directory
                          (default ~/workspace/nedb-portal-load/python)

Run:  python -m aias_memory.server     (with src/ on PYTHONPATH)
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .assembly import DirectiveStore, PromptAssembler
from .conversation_memory import ConversationMemory
from .discussion import DiscussionLog
from .nedb_store import NedbRedisAdapter, load_nedb

# --------------------------------------------------------------------------
# Wiring: one NEDB database holds facts, discussion, directives and KB.
# --------------------------------------------------------------------------

NEDB_PATH = os.environ.get(
    "AIAS_MEMORY_NEDB_PATH",
    os.path.join(os.getcwd(), "aias-memory-data"),
)
UPSTREAM = (os.environ.get("AIAS_MEMORY_UPSTREAM") or "").rstrip("/") or None
API_KEY = os.environ.get("AIAS_MEMORY_API_KEY") or None

NEDB = load_nedb()
_nedb = NEDB(NEDB_PATH)
_adapter = NedbRedisAdapter(_nedb)
memory = ConversationMemory(_adapter)
_discussion = DiscussionLog(_nedb)
_store = DirectiveStore(_adapter)
assembler = PromptAssembler(_store, _discussion, memory)

app = FastAPI(
    title="AiAS Memory — powered by NEDB",
    description="Conversational fact memory + discussion window + broadcast "
    "directives/KB, stored in NEDB. Distilled from AiAS (INTERCHAINED LLC, MIT).",
    version="0.1.0",
)


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str = ""


class ChatCompletionRequest(BaseModel):
    model: str = "aias-memory-demo"
    messages: List[ChatMessage] = []
    user: Optional[str] = None
    workspace_id: Optional[str] = "default"
    agent_id: Optional[str] = None
    system_prompt: Optional[str] = None
    stream: bool = False


class AssembleRequest(BaseModel):
    agent_id: Optional[str] = None
    workspace_id: str = "default"
    user: Optional[str] = None
    user_message: str = ""
    message_limit: Optional[int] = None
    org_default: Optional[int] = None
    system_prompt: Optional[str] = None


class ExtractRequest(BaseModel):
    session_id: str
    workspace_id: Optional[str] = None
    user_msg: str = ""
    ai_response: str = ""
    provider: str = "openai"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

OPENAI_COMPATIBLE_PROVIDERS = ("openai", "groq", "mistral")


def _build_openai_client():
    """Build a real OpenAI client from env, or None if not configured."""
    if not API_KEY:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    kwargs: Dict[str, Any] = {"api_key": API_KEY}
    if UPSTREAM:
        kwargs["base_url"] = UPSTREAM
    return OpenAI(**kwargs)


def _completion(model: str, content: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        **extra,
    }


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "aias-memory",
        "storage": "nedb",
        "nedb_path": NEDB_PATH,
        "upstream_configured": bool(UPSTREAM),
        "extraction_configured": bool(API_KEY),
        "extraction_enabled": await memory.is_extraction_enabled(),
    }


@app.post("/v1/memory/assemble")
async def assemble_endpoint(req: AssembleRequest):
    """Assemble one turn's messages: [system, *history, user].

    Pure assembly — nothing is stored. The caller sends the result to
    its model, then reports the finished turn to /v1/memory/extract.
    """
    result = await assembler.assemble_with_info(
        agent_id=req.agent_id,
        workspace_id=req.workspace_id,
        user_message=req.user_message,
        user_id=req.user,
        message_limit=req.message_limit,
        org_default=req.org_default,
        client_system_prompt=req.system_prompt,
    )
    return result


@app.post("/v1/memory/extract")
async def extract_endpoint(req: ExtractRequest):
    """Record a finished turn and run fact extraction over it.

    The turn is appended to the discussion log (when workspace_id is
    given) so the discussion window stays current. Extraction runs the
    real ConversationMemory pipeline (kill-switch, confidence and PII
    filters, dedupe) when an OpenAI-compatible provider is configured
    via AIAS_MEMORY_API_KEY (+ optional AIAS_MEMORY_UPSTREAM).
    """
    if req.workspace_id:
        if req.user_msg:
            _discussion.append_message(req.workspace_id, "user", req.user_msg)
        if req.ai_response:
            _discussion.append_message(req.workspace_id, "assistant", req.ai_response)

    client = _build_openai_client() if req.provider in OPENAI_COMPATIBLE_PROVIDERS else None
    if client is None:
        return {"extracted": 0, "reason": "no provider configured"}

    before = len(await memory.get_session_facts(req.session_id))
    await memory.post_response_extraction(
        session_id=req.session_id,
        user_msg=req.user_msg,
        ai_response=req.ai_response,
        provider=req.provider,
        client=client,
        turn_number=before,
    )
    facts = await memory.get_session_facts(req.session_id)
    return {"extracted": max(0, len(facts) - before), "fact_count": len(facts)}


@app.get("/v1/memory/facts")
async def facts_endpoint(session_id: str = Query(...)):
    facts = await memory.get_session_facts(session_id)
    return {
        "session_id": session_id,
        "count": len(facts),
        "facts": [f.to_dict() for f in facts],
    }


@app.get("/v1/memory/stats")
async def stats_endpoint(
    session_id: Optional[str] = Query(None),
    workspace_id: Optional[str] = Query(None),
):
    if workspace_id:
        return await memory.get_memory_stats_for_workspace(workspace_id)
    if session_id:
        return await memory.get_session_stats(session_id)
    return {
        "service": "aias-memory",
        "extraction_enabled": await memory.is_extraction_enabled(),
        "hint": "pass session_id= or workspace_id=",
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    workspace_id = req.workspace_id or "default"

    # Client system prompt: explicit field wins, then a leading system
    # message (the public_api.py priority rule).
    client_system = req.system_prompt or ""
    incoming = list(req.messages)
    if not client_system and incoming and incoming[0].role == "system":
        client_system = incoming[0].content
        incoming = incoming[1:]

    user_message = ""
    for msg in reversed(incoming):
        if msg.role == "user":
            user_message = msg.content
            break

    assembled = await assembler.assemble_with_info(
        agent_id=req.agent_id,
        workspace_id=workspace_id,
        user_message=user_message,
        user_id=req.user,
        client_system_prompt=client_system or None,
    )
    messages = assembled["messages"]
    info = {
        "session_id": assembled["session_id"],
        "history_limit": assembled["history_limit"],
        "layers": assembled["layers"],
    }

    if UPSTREAM:
        headers = {"Content-Type": "application/json"}
        if API_KEY:
            headers["Authorization"] = f"Bearer {API_KEY}"
        try:
            async with httpx.AsyncClient(timeout=120) as http:
                resp = await http.post(
                    f"{UPSTREAM}/chat/completions",
                    json={"model": req.model, "messages": messages, "stream": False},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream unreachable: {exc}")
        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"upstream error: {resp.text[:500]}",
            )
        data = resp.json()

        assistant_text = ""
        try:
            assistant_text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            pass

        # Record the turn, then extract facts from it (background-safe:
        # post_response_extraction swallows its own failures).
        if user_message:
            _discussion.append_message(workspace_id, "user", user_message)
        if assistant_text:
            _discussion.append_message(workspace_id, "assistant", assistant_text)
        client = _build_openai_client()
        if client is not None and assembled["session_id"] and assistant_text:
            await memory.post_response_extraction(
                session_id=assembled["session_id"],
                user_msg=user_message,
                ai_response=assistant_text,
                provider="openai",
                client=client,
                turn_number=assembled["layers"]["history_messages"] + 1,
            )

        data["aias_memory"] = {**info, "demo_mode": False}
        return data

    # Demo mode: no upstream model configured. Return an OpenAI-shaped
    # completion PLUS the full assembled prompt, so integrators can see
    # exactly what memory contributes to a turn.
    placeholder = (
        "[AiAS Memory demo mode] No upstream model configured "
        "(AIAS_MEMORY_UPSTREAM/AIAS_MEMORY_API_KEY unset), so no model was "
        "called. The assembled prompt for this turn — broadcast directives, "
        "knowledge base, session facts and the discussion window, all read "
        "from NEDB — is in aias_memory.assembled_messages."
    )
    if user_message:
        _discussion.append_message(workspace_id, "user", user_message)
    _discussion.append_message(workspace_id, "assistant", placeholder)

    return _completion(
        req.model,
        placeholder,
        {
            "aias_memory": {
                **info,
                "demo_mode": True,
                "assembled_messages": messages,
            }
        },
    )


def main():
    import uvicorn

    host = os.environ.get("AIAS_MEMORY_HOST", "127.0.0.1")
    port = int(os.environ.get("AIAS_MEMORY_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
