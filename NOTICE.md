# NOTICE — Provenance

**AiAS Memory** is an open-source distillation of the conversation-memory
stack from **AiAS (AiAssist Secure)**, the AI platform built by
**INTERCHAINED LLC**.

- Copyright (c) 2026 INTERCHAINED LLC
- License: MIT (see `LICENSE`)
- Source snapshot: AiAS `march_2026`, commit `58a1664`

## What was distilled

| Distilled file | Origin in AiAS |
|---|---|
| `src/aias_memory/conversation_memory.py` | `api/services/conversation_memory.py` — copied **verbatim** (byte-identical). The two-lane fact-memory core: extraction, confidence/PII filtering, dedupe, eviction, scopes, circuit breaker, GDPR export/purge. |
| `src/aias_memory/assembly.py` | Distilled from `api/services/ai_orchestrator.py` (session-facts load, history-limit resolution chain, buffer budgeting) and `api/routes/public_api.py` (`build_system_prompt` layering: global directives → agent directives → knowledge base → client prompt). |
| `src/aias_memory/nedb_store.py`, `discussion.py`, `server.py`, `openclaw/` | New code written for this distillation. |

## What was excluded, and why

- **The storage monolith** (`api/services/redis_storage.py`, ~10k lines) —
  it carries the entire AiAS SaaS data model (users, billing, licensing,
  resellers, PIN operations). The memory core only ever needed nine
  Redis-style primitives; those are re-implemented here over **NEDB**
  (`nedb_store.py`), which is the point of the distillation.
- **Billing / licensing / subscription / reseller code** — commercial SaaS
  machinery, unrelated to the memory architecture being shared.
- **Frozen duplicate files** — the AiAS tree carries in-place version
  snapshots (`*_v11`, `*_stable`, `*_broken`, …) that nothing imports.
  Publishing them would publish abandoned intermediate versions alongside
  the live code, so only the live import surface was considered.
- **Secrets and runtime data** — `.env`, Redis snapshots (`dump.rdb`,
  AOF dirs), databases, and built front-end bundles found in the source
  tree are never part of a source distillation.

## Storage

The original runs on Redis. This distillation runs on **NEDB**, the
embedded, journal-backed database from the same authors, via a small
adapter that implements exactly the Redis surface the memory core uses.
NEDB itself is a separate project and is **not** vendored here; point
`NEDB_PYTHON_PATH` at a NEDB checkout (see `README.md`).
