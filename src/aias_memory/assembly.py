"""
Prompt assembly — the third memory type: broadcast context.

Distilled from two AiAS sources:

- `api/routes/public_api.py::build_system_prompt` — the layered system
  prompt: global directives (broadcast to every agent that inherits
  them), then the agent's own directives, then knowledge-base items.
  The final prompt puts this backend foundation first and the client's
  own system prompt on top.
- `api/services/ai_orchestrator.py` — per-turn assembly: session facts
  are loaded from ConversationMemory, the discussion-history limit is
  resolved (workspace message limit 1–50 -> org default -> derived
  `max(5, short_term_buffer // 200)`), and the message list is built as
  [system, *history, user] with the facts block appended to the system
  prompt under the memory visibility guard.

Directives and KB items are stored in NEDB through the same
NedbRedisAdapter the fact memory uses — one storage engine for all
three memory types.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from .conversation_memory import ConversationMemory, MEMORY_VISIBILITY_GUARD
from .discussion import DiscussionLog

__all__ = ["MEMORY_VISIBILITY_GUARD", "DirectiveStore", "PromptAssembler"]


class DirectiveStore:
    """Global/agent directives and knowledge-base items, on NEDB.

    Layout (hash docs via NedbRedisAdapter):
      "aias:directives:global"        fields d:{n} -> JSON directive
      "aias:directives:agent:{id}"    fields d:{n} -> JSON directive
      "aias:kb:global"                fields kb:{n} -> JSON item
      "aias:kb:agent:{id}"            fields kb:{n} -> JSON item
    A "count" field in each hash is the append counter.
    """

    def __init__(self, adapter):
        self.r = adapter

    # -- directives -------------------------------------------------------

    @staticmethod
    def _directives_key(scope: str, agent_id: Optional[str]) -> str:
        if scope == "global":
            return "aias:directives:global"
        if not agent_id:
            raise ValueError("agent scope requires agent_id")
        return f"aias:directives:agent:{agent_id}"

    def add_directive(
        self,
        scope: str,
        content: str,
        directive_type: str = "rule",
        agent_id: Optional[str] = None,
        active: bool = True,
    ) -> int:
        """Append a directive; returns its index within its scope list."""
        key = self._directives_key(scope, agent_id)
        index = self.r.hincrby(key, "count", 1) - 1
        self.r.hset(
            key,
            f"d:{index}",
            json.dumps(
                {
                    "scope": scope,
                    "agent_id": agent_id,
                    "directive_type": directive_type,
                    "content": content,
                    "active": active,
                }
            ),
        )
        return index

    def list_directives(
        self,
        scope: str,
        agent_id: Optional[str] = None,
        active_only: bool = True,
    ) -> List[dict]:
        key = self._directives_key(scope, agent_id)
        raw = self.r.hgetall(key)
        items = []
        for field, value in raw.items():
            if field.startswith("d:"):
                try:
                    items.append((int(field[2:]), json.loads(value)))
                except (ValueError, json.JSONDecodeError):
                    continue
        items.sort(key=lambda pair: pair[0])
        directives = [item for _, item in items]
        if active_only:
            directives = [d for d in directives if d.get("active", True)]
        return directives

    # -- knowledge base ---------------------------------------------------

    @staticmethod
    def _kb_key(agent_id: Optional[str]) -> str:
        return "aias:kb:global" if agent_id is None else f"aias:kb:agent:{agent_id}"

    def add_kb(self, agent_id: Optional[str], content: str, title: Optional[str] = None) -> int:
        """Append a knowledge item (agent_id=None -> global KB)."""
        key = self._kb_key(agent_id)
        index = self.r.hincrby(key, "count", 1) - 1
        self.r.hset(key, f"kb:{index}", json.dumps({"title": title, "content": content}))
        return index

    def list_kb(self, agent_id: Optional[str] = None) -> List[dict]:
        key = self._kb_key(agent_id)
        raw = self.r.hgetall(key)
        items = []
        for field, value in raw.items():
            if field.startswith("kb:"):
                try:
                    items.append((int(field[3:]), json.loads(value)))
                except (ValueError, json.JSONDecodeError):
                    continue
        items.sort(key=lambda pair: pair[0])
        return [item for _, item in items]


class PromptAssembler:
    """Builds the final message list for one turn."""

    def __init__(
        self,
        store: DirectiveStore,
        discussion: DiscussionLog,
        memory: Optional[ConversationMemory] = None,
    ):
        self.store = store
        self.discussion = discussion
        self.memory = memory

    # -- system prompt ----------------------------------------------------

    def build_system_prompt(
        self,
        agent_id: Optional[str] = None,
        client_system_prompt: Optional[str] = None,
        facts_block: Optional[str] = None,
        inherit_global_directives: bool = True,
    ) -> str:
        """Layered system prompt, foundation first:

        global directives -> agent directives -> knowledge base ->
        memory facts block (under the visibility guard) -> client prompt.
        """
        parts: List[str] = []

        if inherit_global_directives:
            for d in self.store.list_directives("global"):
                parts.append(f"[{d['directive_type'].upper()}]: {d['content']}")

        if agent_id:
            for d in self.store.list_directives("agent", agent_id):
                parts.append(f"[{d['directive_type'].upper()}]: {d['content']}")

        kb_parts: List[str] = []
        for kb in self.store.list_kb(None):
            kb_parts.append(self._format_kb(kb))
        if agent_id:
            for kb in self.store.list_kb(agent_id):
                kb_parts.append(self._format_kb(kb))
        if kb_parts:
            parts.append("\n## Knowledge Base:\n" + "\n\n".join(kb_parts))

        prompt = "\n\n".join(parts)

        if facts_block:
            guard = MEMORY_VISIBILITY_GUARD.strip()
            prompt = (prompt + "\n\n" if prompt else "") + guard + "\n\n" + facts_block

        if client_system_prompt:
            prompt = (prompt + "\n\n" if prompt else "") + client_system_prompt

        return prompt

    @staticmethod
    def _format_kb(kb: dict) -> str:
        title = kb.get("title")
        return f"### {title}\n{kb['content']}" if title else kb["content"]

    # -- full turn assembly -----------------------------------------------

    @staticmethod
    def resolve_history_limit(
        message_limit: Optional[int],
        org_default: Optional[int],
        buffer_limit: int,
    ) -> int:
        """The AiAS chain: workspace limit (1–50) -> org default (1–50)
        -> max(5, short_term_buffer // 200)."""
        if message_limit and 1 <= message_limit <= 50:
            return message_limit
        if org_default and 1 <= org_default <= 50:
            return org_default
        return max(5, buffer_limit // 200)

    async def assemble_with_info(
        self,
        agent_id: Optional[str] = None,
        workspace_id: str = "default",
        user_message: str = "",
        memory: Optional[ConversationMemory] = None,
        user_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        message_limit: Optional[int] = None,
        org_default: Optional[int] = None,
        provider: str = "openai",
        client_system_prompt: Optional[str] = None,
        memory_enabled: bool = True,
    ) -> Dict:
        """Assemble one turn; returns messages plus a layer breakdown."""
        memory = memory or self.memory
        conversation_id = conversation_id or workspace_id

        session_id: Optional[str] = None
        facts = []
        buffer_limit = 4000
        if memory is not None:
            scope, _identity = memory.resolve_memory_scope(
                user_id=user_id, lead_id=None, conversation_id=conversation_id
            )
            session_id = memory.get_session_id(
                workspace_id=workspace_id,
                user_id=user_id,
                conversation_id=conversation_id,
                lead_id=None,
                scope=scope,
            )
            if memory_enabled:
                facts = await memory.get_session_facts(session_id)
                await memory.update_session_metadata(
                    session_id=session_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    provider=provider,
                )
            budget = memory.calculate_token_budget(provider, memory_enabled)
            buffer_limit = budget.get("short_term_buffer", 4000)

        history_limit = self.resolve_history_limit(message_limit, org_default, buffer_limit)
        history = self.discussion.get_messages(workspace_id, limit=history_limit)

        facts_block = memory.format_session_memory(facts) if (memory and facts) else None
        system_prompt = self.build_system_prompt(
            agent_id=agent_id,
            client_system_prompt=client_system_prompt,
            facts_block=facts_block,
        )

        messages: List[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        layers = {
            "global_directives": len(self.store.list_directives("global")),
            "agent_directives": len(self.store.list_directives("agent", agent_id)) if agent_id else 0,
            "kb_items": len(self.store.list_kb(None)) + (len(self.store.list_kb(agent_id)) if agent_id else 0),
            "facts": len(facts),
            "history_messages": len(history),
            "client_system_prompt": bool(client_system_prompt),
        }
        return {
            "messages": messages,
            "session_id": session_id,
            "history_limit": history_limit,
            "layers": layers,
        }

    async def assemble(self, **kwargs) -> List[dict]:
        """Assemble one turn; returns just the messages list:
        [system, *history, user]."""
        result = await self.assemble_with_info(**kwargs)
        return result["messages"]
