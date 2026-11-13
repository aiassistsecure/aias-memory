# The Lore of AiAS Memory

## Where this came from

AiAS (AiAssist Secure) is the agent platform built by INTERCHAINED LLC.
It ran real agents for real customers, and like every agent platform it
ran into the same wall: the model only knows what fits in the prompt,
and the prompt starts empty every single time.

The big labs answered that wall with vector databases, embedding
pipelines, and cloud memory services. We answered it differently,
because our agents ran on our own metal, next to our own database,
and we wanted memory we could hold in our hands.

This package is that answer, taken out of the AiAS codebase (March 2026
snapshot) and given away. The core file, `conversation_memory.py`, is
byte-for-byte the code that ran in production. We did not rewrite it
for the release. We lifted it, gave it a new home on NEDB, and wrote
the adapters around it.

## The three memories

Production taught us that "memory" is not one thing. It is three, and
mixing them up is how agents get weird.

**Facts.** People tell an agent things that stay true: preferences,
constraints, decisions, names, numbers. After each turn, AiAS ran an
extractor over the conversation and pulled those facts out in a strict
format, `CATEGORY|CONFIDENCE|FACT`. Anything under 0.75 confidence was
thrown away. Anything that looked like personal data it should not keep
was thrown away. Duplicates were caught by content hash. When a session
filled up, the least valuable facts were evicted first. What survived
got injected into later prompts as quiet background, under a guard that
tells the model: use this, do not recite it. An agent that announces
everything it remembers is not smart. It is just loud.

**Discussion.** The recent conversation itself, verbatim, in a window
whose size is a policy, not an accident: a workspace limit if one is
set, an organization default under that, and a token budget under
everything. When the window outgrows its budget, the old turns are
compressed into a summary instead of being dropped on the floor.

**Broadcast.** Standing instructions and reference material that every
conversation inherits, layered on purpose: global directives first,
then the agent's own directives, then the knowledge base, then the
remembered facts, and the caller's system prompt on top. Whoever runs
the deployment can steer every agent at once, and the layering order
is the constitution that decides whose word wins.

## Why NEDB

Memory you cannot inspect is memory you cannot trust. NEDB is an
embedded database with a journal: every write is an entry in an
append-only record you can read back, audit, and roll back. There is
no server to run, no embedding model to babysit, no cloud account
holding your users' facts. Your agent's memory is a directory on disk.
You can open it. You can back it up. You can watch it grow.

The bridge between the two worlds is small on purpose. The AiAS core
was written against a Redis-shaped client, about ten commands. This
package implements those ten commands over NEDB documents, and the
core never noticed the swap. That is the whole trick, and it is why
the core could ship verbatim.

## Why it is free

Because memory should be table stakes, not a moat. The interesting
problems in agents are not "can it remember my name." They are what
an agent does once remembering is boring. We would rather the whole
ecosystem stand on this floor and compete above it.

So it is MIT. Take it, fork it, ship it in products. The provenance
stays on it, INTERCHAINED LLC, AiAS, NEDB, because where code comes
from matters. But the code is yours now too.

## Why OpenClaw

OpenClaw is where the builders are. Its plugin SDK has a concept most
platforms never bothered with: a context engine, a single replaceable
piece that owns how a prompt gets assembled. That is exactly the shape
of this stack, so the adapter is thin: a small TypeScript plugin that
calls the memory service, and the service does the real work on NEDB.
If you run OpenClaw, you can have production-grade memory in an
afternoon, on your own disk, under your own control.

Welcome aboard. Build something that remembers.

## The third seat

No workshop story here is complete without the trio. In the
Interchained workshop, work happens at a table with three seats.

The first seat is the Architect, Mark, who decides what gets built
and what it means. The third seat is the builder, Muse, who writes
the code and keeps the journal. And the second seat belongs to the
Oracle, GPT, the outside mind called in to point at what the other
two are too close to see.

The Oracle does not write the code and does not make the final call.
It rules on the hard questions: a format, a trade-off, a design that
has to hold for years. Some of the deepest decisions in NEDB and in
this memory stack carry those rulings in their bones. The Oracle
points. The Architect decides. The builder builds.

That is why the sign-off on everything we ship is 3>1. Three seats,
one table, and the work that comes off it is greater than any one
mind at it alone. It is also, not coincidentally, a fine description
of memory itself: no single turn of a conversation means much. The
accumulation is the intelligence.


Signed,

**Mark A Evans Jr.**, INTERCHAINED LLC
**Muse**

**3>1**
