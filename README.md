# AiAS Memory, powered by NEDB

*New here? Read [LORE.md](LORE.md) for the story of where this came from and why it is free.*

**The conversation-memory stack from AiAS (AiAssist Secure), distilled
into a standalone open-source package that runs on NEDB.**

Your agent forgets. This fixes that, with three distinct memory
types, one storage engine, and no embeddings, no vector database, and
no cloud memory service:

1. **Conversational fact memory.** After each turn, an extractor model
   pulls durable facts out of the conversation, preferences,
   constraints, decisions, context, in a strict
   `CATEGORY|CONFIDENCE|FACT` format. Facts are confidence-filtered
   (≥ 0.75), PII-filtered, content-hash deduplicated, and evicted
   under a per-session cap. On later turns they're injected into the
   system prompt as background context, under a visibility guard that
   keeps the model from reciting them like a robot.
2. **Discussion window.** The most recent turns of the conversation,
   verbatim. The window size resolves like the original: workspace
   message limit (1–50) → organization default → a token-budget-derived
   `max(5, buffer // 200)`. When the buffer outgrows its budget, older
   turns are compressed into a summary instead of being dropped.
3. **Broadcast directives + knowledge base.** Standing instructions
   and reference material, layered into every prompt in a deliberate
   order: global directives (broadcast) → agent directives →
   knowledge base → memory facts → the client's own system prompt.

Everything, facts, discussion, directives, KB, is stored in
**NEDB**, an embedded, journal-backed database. Your agent's memory
is a directory on disk you can inspect, back up, and time-travel.

```text
                        ┌─────────────────────────────────────────┐
   directives ──┐       │  PromptAssembler                        │
   knowledge ───┤       │                                         │
   facts ───────┼──────►│  system = directives + KB + facts       │
   discussion ──┘       │  messages = [system, *window, user]     │
                        └──────────────────┬──────────────────────┘
                                           │
              ┌────────────────────────────┼───────────────────┐
              │                            │                   │
     ConversationMemory             DiscussionLog        DirectiveStore
     (verbatim from AiAS)           (message log)        (directives/KB)
              │                            │                   │
              └────────────────────────────┼───────────────────┘
                                  NedbRedisAdapter
                              (9 Redis primitives)
                                           │
                                         NEDB
                              (embedded, journalled)
```

## Quickstart

```bash
git clone <this repo> && cd aias-memory
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# The NEDB engine is a separate project; point at its python/ dir:
export NEDB_PYTHON_PATH=/path/to/nedb/python
```

**See it work**, a scripted end-to-end demo with progress output.
Extraction runs the real pipeline; only the extraction *model call* is
a labeled stub, so it works with no API key:

```bash
venv/bin/python demo.py     # ends with "DEMO VERDICT: PASS"
venv/bin/python tests/smoke.py
```

**Run the service** (the NEDB connector), one command:

```bash
./run.sh                    # http://127.0.0.1:8000
# or: PYTHONPATH=src venv/bin/python -m aias_memory.server
```

Give it a model (any OpenAI-compatible endpoint) to enable live fact
extraction and completion proxying:

```bash
export AIAS_MEMORY_UPSTREAM=https://api.openai.com/v1
export AIAS_MEMORY_API_KEY=sk-...
./run.sh
```

**Use it as a library:**

```python
import asyncio
from aias_memory import open_adapter, DirectiveStore, PromptAssembler, DiscussionLog
from aias_memory.conversation_memory import ConversationMemory

adapter = open_adapter("./my-memory-data")          # opens NEDB
memory = ConversationMemory(adapter)                # the AiAS core, verbatim
discussion = DiscussionLog(adapter.db)
store = DirectiveStore(adapter)
store.add_directive("global", "Be concise.", directive_type="style")

assembler = PromptAssembler(store, discussion, memory)
messages = asyncio.run(assembler.assemble(
    workspace_id="my-app", user_id="u1",
    user_message="What did we decide about storage?"))
# -> [system (directives + facts), *recent discussion, user]
```

## The service: HTTP contract

The FastAPI service (`src/aias_memory/server.py`) is the integration
surface. Two calls are **the adapter contract**, the OpenClaw plugin
in `openclaw/` is built on exactly these:

### `POST /v1/memory/assemble`

Assemble one turn's messages without calling a model. Pure read,
nothing is stored.

Request:

```json
{
  "workspace_id": "openclaw",
  "agent_id": "my-agent",
  "user": "session-or-user-id",
  "user_message": "What did we pick for storage?",
  "message_limit": 20,
  "system_prompt": "Optional client system prompt, layered on top"
}
```

`agent_id`, `user`, `message_limit`, and `system_prompt` are optional.
`message_limit` feeds the resolution chain (workspace limit → org
default → derived).

Response:

```json
{
  "messages": [
    {"role": "system", "content": "[POLICY]: ...\n\n## Knowledge Base:\n...\n\nMEMORY VISIBILITY RULES: ...\n\n[Session Context]\n- Will use NEDB instead of Postgres for storage"},
    {"role": "user", "content": "We decided to use NEDB instead of Postgres."},
    {"role": "assistant", "content": "Locked in."},
    {"role": "user", "content": "What did we pick for storage?"}
  ],
  "session_id": "openclaw:session-or-user-id",
  "history_limit": 20,
  "layers": {
    "global_directives": 2, "agent_directives": 1, "kb_items": 3,
    "facts": 4, "history_messages": 2, "client_system_prompt": false
  }
}
```

### `POST /v1/memory/extract`

Report a finished turn. The turn is appended to the discussion log,
then fact extraction runs over it (when a provider is configured).

Request:

```json
{
  "session_id": "openclaw:session-or-user-id",
  "workspace_id": "openclaw",
  "user_msg": "We decided to use NEDB instead of Postgres.",
  "ai_response": "Locked in, NEDB for storage.",
  "provider": "openai"
}
```

Response (provider configured): `{"extracted": 1, "fact_count": 5}`

Response (no `AIAS_MEMORY_API_KEY` set):
`{"extracted": 0, "reason": "no provider configured"}`, the turn is
still recorded in the discussion log either way.

### Supporting endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-shaped completions with assembly in front of your upstream model. With no upstream configured, returns a demo completion carrying the full assembled prompt in an `aias_memory` field (`"demo_mode": true`). |
| `GET /v1/memory/facts?session_id=…` | Read a session's stored facts. |
| `GET /v1/memory/stats?session_id=…` or `?workspace_id=…` | Fact counts, categories, activity. |
| `GET /health` | Liveness + configuration summary. |

Service environment: `AIAS_MEMORY_NEDB_PATH` (default
`./aias-memory-data`), `AIAS_MEMORY_UPSTREAM`, `AIAS_MEMORY_API_KEY`,
`AIAS_MEMORY_HOST`, `AIAS_MEMORY_PORT`, `NEDB_PYTHON_PATH`.

## OpenClaw compatibility

**AiAS Memory for OpenClaw, powered by NEDB** lives in
[`openclaw/`](openclaw/): a self-contained, drop-in context-engine
plugin (manifest + config schema + `index.ts`, no build step). It
registers a context engine that delegates `assemble`/`afterTurn` to
this service over the HTTP contract above. Install walkthrough, written
for OpenClaw users, is in [`openclaw/README.md`](openclaw/README.md).

## Design notes

- **The fact-memory core is verbatim AiAS.** `conversation_memory.py`
  is byte-identical to the production file, scopes (user / workspace /
  conversation / lead), circuit breaker, kill switch, GDPR
  export/purge, lead→user migration and all. Provenance in
  [`NOTICE.md`](NOTICE.md).
- **Storage seam.** The core was written against Redis; it uses
  exactly nine primitives. `nedb_store.py` implements them over NEDB
  documents (collection = key's first segment, doc id = full key).
  One deliberate deviation: `expire` records the TTL deadline on the
  doc but does not enforce it on read, NEDB is a journal, and durable
  memory expiring silently is a bug, not a feature.
- **Safety is the design.** Facts enter memory only through the
  pipeline: kill-switch → extraction timeout → confidence ≥ 0.75 →
  PII pattern filter → dedupe → capped eviction. The kill switch lives
  at `aai:config:extraction_disabled` and can be toggled at runtime
  via `ConversationMemory.set_extraction_enabled`.

## License

MIT, Copyright (c) 2026 **INTERCHAINED LLC**. Distilled from AiAS
(aiassistsecure), march_2026 snapshot `58a1664`. It's ours, and now
it's yours too. NEDB is a separate project by the same authors and is
not vendored here.

---

## The Interchained properties

AiAS Memory is one piece of a larger workshop. Everything below is built by INTERCHAINED LLC:

- **AiAS and PIN**, the agent platform and p2p inference network this memory was distilled from: https://aiassistsecure.com
- **NEDB**, the embedded journal database underneath it all: https://github.com/aiassistsecure/nedb
- **Interchained (ITC)**, the tri-PoW blockchain: https://github.com/interchained/interchained
- **Vision**, the ITC block explorer: https://vision.interchained.org
- **Models**, our published weights: https://huggingface.co/Interchained
- **GitHub**: https://github.com/aiassistsecure

Signed,

**Mark A Evans Jr.**, INTERCHAINED LLC
**Muse**

**3>1**
