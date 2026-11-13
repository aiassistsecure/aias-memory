"""
AiAS Memory — the AiAS conversation-memory stack, distilled for
open source and running on NEDB.

Three memory types, one storage engine:
- Conversational fact memory  (conversation_memory.ConversationMemory)
- Discussion window           (discussion.DiscussionLog)
- Broadcast directives + KB   (assembly.DirectiveStore / PromptAssembler)

Copyright (c) 2026 INTERCHAINED LLC. MIT License.
Distilled from AiAS (aiassistsecure) march_2026, commit 58a1664 — see
NOTICE.md for provenance.
"""

from .nedb_store import NedbRedisAdapter, load_nedb, open_adapter
from .discussion import DiscussionLog
from .assembly import DirectiveStore, PromptAssembler, MEMORY_VISIBILITY_GUARD

__version__ = "0.1.0"

__all__ = [
    "NedbRedisAdapter",
    "load_nedb",
    "open_adapter",
    "DiscussionLog",
    "DirectiveStore",
    "PromptAssembler",
    "MEMORY_VISIBILITY_GUARD",
    "__version__",
]
