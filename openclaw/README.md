# AiAS Memory for OpenClaw, powered by NEDB

Give your OpenClaw agent a real memory:

- **Fact memory**, after each conversation turn, durable facts
  (preferences, decisions, constraints) are extracted automatically,
  filtered for confidence and PII, deduplicated, and quietly injected
  into future prompts as background context.
- **Discussion window**, the most recent turns of conversation ride
  along verbatim, so the agent picks up where you left off.
- **Broadcast directives + knowledge base**, standing instructions
  and reference material layered into every system prompt.

Everything is stored in **NEDB**, an embedded, journal-backed database
, no Redis, no cloud memory service, your data stays a directory on
your disk.

This folder is a **drop-in OpenClaw plugin**. The plugin itself is
thin: it delegates context assembly to the **AiAS Memory service**
(a small Python service from the same repo, think of it as the NEDB
connector). You run the service next to OpenClaw, drop this folder
into OpenClaw, and point one at the other.

```
OpenClaw ──(context engine calls)──► AiAS Memory service ──► NEDB
   ▲                                        │
   └──────── assembled messages ◄───────────┘
```

## Step 1, Start the AiAS Memory service

You need the `aias-memory` repo (this plugin lives in its `openclaw/`
folder) and a checkout of the NEDB engine's Python package.

```bash
git clone https://github.com/aiassistsecure/aias-memory.git && cd aias-memory
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Tell it where NEDB lives (path to NEDB's python/ directory):
export NEDB_PYTHON_PATH=/path/to/nedb/python

./run.sh        # serves on http://127.0.0.1:8000
```

Optional: give the service a model to extract facts with and to proxy
completions through, any OpenAI-compatible endpoint works:

```bash
export AIAS_MEMORY_UPSTREAM=https://api.openai.com/v1
export AIAS_MEMORY_API_KEY=sk-...
./run.sh
```

Without those two variables the service still assembles prompts and
stores discussion history; fact extraction simply reports
"no provider configured".

Sanity check:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","service":"aias-memory","storage":"nedb",...}
```

## Step 2, Install this plugin into OpenClaw

Copy this folder into your OpenClaw plugins directory (or reference
its path in your OpenClaw config's plugin load paths, per the OpenClaw
plugin docs):

```bash
cp -r openclaw/ ~/.openclaw/plugins/aias-memory
```

The folder is self-contained: `openclaw.plugin.json` (manifest +
config schema), `package.json`, and `index.ts`, no build step, no
npm dependencies.

## Step 3, Configure and select it

In your OpenClaw config, enable the plugin, point it at the service,
and select `aias-memory` as the context engine for the memory slot:

```json5
{
  plugins: {
    entries: {
      "aias-memory": {
        enabled: true,
        config: {
          serviceUrl: "http://127.0.0.1:8000", // default
          workspaceId: "openclaw",             // memory namespace
          // agentId: "my-agent",              // optional: per-agent
                                              // directives + KB
        },
      },
    },
  },
  // Select the engine (key names follow the OpenClaw plugin docs):
  memory: { engine: "aias-memory" },
}
```

Restart OpenClaw. On session start the plugin health-checks the
service and logs `aias-memory: connected to …`. If the service is
down, the plugin fails soft: OpenClaw keeps working, prompts just
pass through unassembled.

## How it maps to the service

| OpenClaw hook | Service call |
|---|---|
| `bootstrap` | `GET /health` |
| `assemble` (before each model call) | `POST /v1/memory/assemble`, returns the layered message list |
| `afterTurn` (after each reply) | `POST /v1/memory/extract`, records the turn, extracts facts |

The full HTTP contract is documented in the main repo `README.md`.

## Seeding directives and knowledge

Directives/KB live in NEDB alongside everything else. The quickest
way to seed them today is from Python, against the same database the
service uses:

```python
from aias_memory import open_adapter, DirectiveStore
store = DirectiveStore(open_adapter("./aias-memory-data"))
store.add_directive("global", "Always answer in English.", directive_type="style")
store.add_kb(None, "Support hours are 9–5 ET.", title="Support")  # global KB
```

## License

MIT, Copyright (c) 2026 INTERCHAINED LLC. Distilled from AiAS
(aiassistsecure); see `NOTICE.md` in the main repo for provenance.

---

## The Interchained properties

This plugin is one piece of a larger workshop. Everything below is built by INTERCHAINED LLC:

- **AiAS Memory**, the full package, the service, and the story: https://github.com/aiassistsecure/aias-memory
- **AiAS and PIN**, the agent platform and p2p inference network this memory was distilled from: https://aiassistsecure.com
- **NEDB**, the embedded journal database underneath it all: https://github.com/aiassistsecure/nedb
- **Interchained (ITC)**, the tri-PoW blockchain: https://github.com/interchained/interchained
- **Vision**, the ITC block explorer: https://vision.interchained.org
- **Models**, our published weights: https://huggingface.co/Interchained
- **GitHub**: https://github.com/aiassistsecure


## Security

Conversation turns and session identifiers are sent to the memory
service you configure, and the service's assembled response becomes
part of the prompt. Run the service where that is safe:

* The default is loopback, http://127.0.0.1:8000, on the same machine
  as the gateway. Traffic never leaves the box.
* The plugin enforces transport policy: plain HTTP is accepted only
  for loopback addresses (localhost, 127.0.0.0/8, ::1). Any remote
  serviceUrl must be HTTPS, or the plugin refuses to register.
* Service responses are validated before use: only well-formed
  messages with known roles and string content enter the prompt.

* Context boundary: the gateway's own system instructions are
  preserved verbatim and always come first. Service responses can
  never supply system-role messages. Facts and discussion arrive as
  labeled reference data. The service's directive block only earns a
  system seat if you explicitly set allowServiceDirectives, which you
  should do only for a memory service you operate yourself.

Signed,

**Mark A Evans Jr.**, INTERCHAINED LLC
**Muse**

**3>1**
