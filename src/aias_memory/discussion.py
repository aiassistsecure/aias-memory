"""
DiscussionLog — the short-term discussion window, stored in NEDB.

Lane 1 of the AiAS two-lane memory design: the recent conversation,
verbatim. In AiAS this lived in the Redis storage monolith; here each
message is a plain NEDB document in the "discussion" collection:

  meta doc:    id = "{workspace_id}:__meta__"     {"count": N}
  message doc: id = "{workspace_id}:{seq:08d}"    {"workspace_id", "seq",
                                                   "role", "content", "ts"}

`get_messages` returns the last `limit` messages, oldest first — the
shape the prompt assembler injects after the system prompt.
"""

from __future__ import annotations

import time
from typing import List, Optional

COLLECTION = "discussion"


def _strip_meta(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ("_id", "_seq", "_hash", "_coll")}


class DiscussionLog:
    def __init__(self, db):
        self.db = db

    def _meta_id(self, workspace_id: str) -> str:
        return f"{workspace_id}:__meta__"

    def _msg_id(self, workspace_id: str, seq: int) -> str:
        return f"{workspace_id}:{seq:08d}"

    def count(self, workspace_id: str) -> int:
        doc = self.db.get(COLLECTION, self._meta_id(workspace_id))
        if not doc:
            return 0
        return int(doc.get("count", 0))

    def append_message(self, workspace_id: str, role: str, content: str) -> int:
        """Append a message; returns its sequence number."""
        seq = self.count(workspace_id)
        self.db.put(
            COLLECTION,
            self._msg_id(workspace_id, seq),
            {
                "workspace_id": workspace_id,
                "seq": seq,
                "role": role,
                "content": content,
                "ts": time.time(),
            },
        )
        self.db.put(COLLECTION, self._meta_id(workspace_id), {"count": seq + 1})
        return seq

    def get_messages(
        self,
        workspace_id: str,
        limit: Optional[int] = None,
    ) -> List[dict]:
        """Last `limit` messages for the workspace, oldest -> newest.

        Returns API-shaped dicts: {"role": ..., "content": ...}.
        """
        total = self.count(workspace_id)
        if total == 0:
            return []
        if limit is None or limit >= total:
            start = 0
        else:
            start = total - max(0, limit)

        messages: List[dict] = []
        for seq in range(start, total):
            doc = self.db.get(COLLECTION, self._msg_id(workspace_id, seq))
            if not doc:
                continue
            doc = _strip_meta(doc)
            messages.append({"role": doc.get("role", "user"), "content": doc.get("content", "")})
        return messages
