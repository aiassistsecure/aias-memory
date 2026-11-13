#!/usr/bin/env python3
"""
Smoke tests for AiAS Memory — plain script, no pytest.

Run:  python3 tests/smoke.py     (exit 0 = pass, 1 = fail)

Covers: NedbRedisAdapter roundtrips (hash ops, hincrby, string ops,
scan_iter), DiscussionLog window behaviour, and PromptAssembler
layering order + history-limit chain.
"""

import asyncio
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
sys.path.insert(
    0,
    os.environ.get(
        "NEDB_PYTHON_PATH",
        os.path.expanduser("~/workspace/nedb-portal-load/python"),
    ),
)

from nedb import NEDB  # noqa: E402

from aias_memory.assembly import DirectiveStore, PromptAssembler  # noqa: E402
from aias_memory.conversation_memory import ConversationMemory, SessionFact  # noqa: E402
from aias_memory.discussion import DiscussionLog  # noqa: E402
from aias_memory.nedb_store import NedbRedisAdapter  # noqa: E402
from datetime import datetime  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if (detail and not cond) else ""))
    if not cond:
        FAILURES.append(name)


def test_adapter(db):
    print("adapter roundtrips")
    r = NedbRedisAdapter(db)
    key = "aai:session:ws:u1:facts"

    check("hgetall missing -> {}", r.hgetall(key) == {})
    r.hset(key, "fact:0", '{"content":"x"}')
    r.hset(key, mapping={"fact_count": "1", "last_updated": "now"})
    all_fields = r.hgetall(key)
    check("hset positional + mapping", all_fields.get("fact:0") == '{"content":"x"}' and all_fields.get("fact_count") == "1")
    check("all values are str", all(isinstance(v, str) for v in all_fields.values()))
    check("hget", r.hget(key, "fact_count") == "1")
    check("hincrby starts at amount", r.hincrby(key, "counter", 5) == 5)
    check("hincrby accumulates", r.hincrby(key, "counter", 1) == 6)
    check("hincrby stored as str", r.hget(key, "counter") == "6")
    check("hdel", r.hdel(key, "fact:0") == 1 and r.hget(key, "fact:0") is None)

    r.set("aai:config:extraction_disabled", "1")
    check("set/get", r.get("aai:config:extraction_disabled") == "1")
    check("get on hash key -> None", r.get(key) is None)
    check("expire existing -> True", r.expire(key, 3600) is True)
    check("expire missing -> False", r.expire("aai:nope:missing", 10) is False)

    r.hset("aai:session:ws:u2:facts", "fact:0", "{}")
    r.hset("aai:session:other:u3:facts", "fact:0", "{}")
    found = set(r.scan_iter(match="aai:session:ws:*:facts"))
    check("scan_iter fnmatch", found == {"aai:session:ws:u1:facts", "aai:session:ws:u2:facts"}, f"got {found}")
    check("delete", r.delete("aai:config:extraction_disabled") == 1 and r.get("aai:config:extraction_disabled") is None)


def test_discussion(db):
    print("discussion window")
    log = DiscussionLog(db)
    for i in range(5):
        log.append_message("ws1", "user" if i % 2 == 0 else "assistant", f"msg-{i}")
    all_msgs = log.get_messages("ws1", limit=50)
    check("limit > total returns all", len(all_msgs) == 5)
    check("oldest -> newest order", [m["content"] for m in all_msgs] == [f"msg-{i}" for i in range(5)])
    window = log.get_messages("ws1", limit=3)
    check("window keeps the LAST 3", [m["content"] for m in window] == ["msg-2", "msg-3", "msg-4"])
    check("roles preserved", window[0]["role"] == "user" and window[1]["role"] == "assistant")
    check("unknown workspace -> []", log.get_messages("nope", limit=5) == [])


async def test_assembly(db):
    print("assembly layering")
    adapter = NedbRedisAdapter(db)
    store = DirectiveStore(adapter)
    discussion = DiscussionLog(db)
    memory = ConversationMemory(adapter)
    assembler = PromptAssembler(store, discussion, memory)

    store.add_directive("global", "GLOBAL-RULE", directive_type="policy")
    store.add_directive("agent", "AGENT-RULE", directive_type="role", agent_id="a1")
    store.add_kb("a1", "KB-CONTENT", title="KB-TITLE")
    store.add_kb(None, "GLOBAL-KB", title="GLOBAL-KB-TITLE")

    prompt = assembler.build_system_prompt(agent_id="a1", client_system_prompt="CLIENT-PROMPT")
    order = [prompt.find(s) for s in ("GLOBAL-RULE", "AGENT-RULE", "KB-CONTENT", "CLIENT-PROMPT")]
    check("layer order global < agent < kb < client", all(i >= 0 for i in order) and order == sorted(order), f"order={order}")
    check("global KB included", "GLOBAL-KB" in prompt)

    session_id = memory.get_session_id("wsA", "user1", "wsA", None,
                                       memory.resolve_memory_scope("user1", None, "wsA")[0])
    await memory.add_session_facts(session_id, [SessionFact(
        content="User likes tea", category="preference", confidence=0.9,
        turn_number=1, created_at=datetime.utcnow())])

    for i in range(5):
        discussion.append_message("wsA", "user", f"hist-{i}")

    result = await assembler.assemble_with_info(
        agent_id="a1", workspace_id="wsA", user_message="hello",
        user_id="user1", message_limit=2)
    msgs = result["messages"]
    check("assemble shape [system, *history, user]",
          msgs[0]["role"] == "system" and msgs[-1] == {"role": "user", "content": "hello"})
    check("message_limit honoured", result["history_limit"] == 2 and result["layers"]["history_messages"] == 2)
    check("facts block in system prompt", "[Session Context]" in msgs[0]["content"] and "User likes tea" in msgs[0]["content"])
    check("history window is the most recent", msgs[1]["content"] == "hist-3" and msgs[2]["content"] == "hist-4")

    check("history limit falls back to org default",
          PromptAssembler.resolve_history_limit(None, 7, 4000) == 7)
    check("history limit derives from buffer",
          PromptAssembler.resolve_history_limit(None, None, 4000) == 20)
    check("history limit rejects out-of-range workspace value",
          PromptAssembler.resolve_history_limit(99, None, 4000) == 20)


def main():
    tmp = tempfile.mkdtemp(prefix="aias-memory-smoke-")
    try:
        db = NEDB(os.path.join(tmp, "db"))
        test_adapter(db)
        test_discussion(db)
        asyncio.run(test_assembly(db))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILURES:
        print(f"\nSMOKE: FAIL — {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("\nSMOKE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
