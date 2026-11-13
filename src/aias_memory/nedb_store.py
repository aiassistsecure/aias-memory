"""
NedbRedisAdapter — the Redis surface of ConversationMemory, over NEDB.

`conversation_memory.py` (the AiAS fact-memory core, included verbatim)
was written against a Redis client. It uses exactly nine primitives:
hgetall, hget, hset, hincrby, hdel, expire, get, set, delete, scan_iter.
This adapter implements that surface over NEDB documents so the original
code runs unchanged on NEDB — no Redis server required.

Document shape (one NEDB doc per Redis key):
  collection = the key's first ':' segment (or "_unrouted")
  doc id     = the full key
  hash       -> {"key": ..., "rtype": "hash",   "fields": {field: str, ...}}
  string     -> {"key": ..., "rtype": "string", "value": str}

All values are coerced to str on write, matching redis-py with
decode_responses=True: everything in and out is a string.

TTL: `expire` records the deadline on the doc ("_ttl_s" / "_expires_at")
but does not enforce it — NEDB is an append-only journal, so expiry is a
sweep concern, not a read-path concern. This matches the distillation's
goal (durable memory) and is a deliberate, documented deviation from
Redis semantics.

Requires the pure-Python NEDB engine (package `nedb`, class `NEDB`).
Point NEDB_PYTHON_PATH at the NEDB checkout's `python/` directory.
"""

from __future__ import annotations

import fnmatch
import os
import sys
import time
from typing import Dict, Iterator, Optional

__all__ = ["NedbRedisAdapter", "load_nedb", "open_adapter"]


def load_nedb():
    """Import the NEDB engine, honouring NEDB_PYTHON_PATH."""
    path = os.environ.get(
        "NEDB_PYTHON_PATH",
        os.path.expanduser("~/workspace/nedb-portal-load/python"),
    )
    if path and path not in sys.path:
        sys.path.insert(0, path)
    from nedb import NEDB  # noqa: WPS433 (intentional late import)

    return NEDB


def open_adapter(db_path: str) -> "NedbRedisAdapter":
    """Open (or create) a NEDB database at db_path and wrap it."""
    NEDB = load_nedb()
    return NedbRedisAdapter(NEDB(db_path))


def _coll_of(key: str) -> str:
    return key.split(":", 1)[0] if ":" in key else "_unrouted"


class NedbRedisAdapter:
    """A redis-py-shaped client backed by NEDB documents."""

    def __init__(self, db):
        self.db = db

    # -- internal doc plumbing -------------------------------------------

    def _load(self, key: str) -> Optional[dict]:
        doc = self.db.get(_coll_of(key), key)
        if not doc:
            return None
        # Strip NEDB engine metadata (_id/_seq/_hash/_coll and our own
        # underscore-prefixed bookkeeping stays available to this class
        # via the raw doc, but the payload view drops engine fields).
        return {k: v for k, v in doc.items() if k not in ("_id", "_seq", "_hash", "_coll")}

    def _save(self, key: str, doc: dict) -> None:
        doc = dict(doc)
        doc["key"] = key
        self.db.put(_coll_of(key), key, doc)

    def _load_hash(self, key: str) -> Optional[dict]:
        doc = self._load(key)
        if doc and doc.get("rtype") == "hash":
            return doc
        return None

    # -- hash operations --------------------------------------------------

    def hgetall(self, key: str) -> Dict[str, str]:
        doc = self._load_hash(key)
        if not doc:
            return {}
        return dict(doc.get("fields") or {})

    def hget(self, key: str, field: str) -> Optional[str]:
        doc = self._load_hash(key)
        if not doc:
            return None
        return (doc.get("fields") or {}).get(field)

    def hset(self, key: str, *args, mapping: Optional[dict] = None, **kwargs) -> int:
        updates: Dict[str, str] = {}
        if mapping:
            updates.update(mapping)
        if kwargs:
            updates.update(kwargs)
        if args:
            if len(args) % 2 != 0:
                raise ValueError("hset: field/value args must come in pairs")
            for i in range(0, len(args), 2):
                updates[args[i]] = args[i + 1]
        if not updates:
            return 0

        doc = self._load_hash(key) or {"rtype": "hash", "fields": {}}
        fields = dict(doc.get("fields") or {})
        added = 0
        for field, value in updates.items():
            if field not in fields:
                added += 1
            fields[str(field)] = str(value)
        doc["fields"] = fields
        self._save(key, doc)
        return added

    def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        doc = self._load_hash(key) or {"rtype": "hash", "fields": {}}
        fields = dict(doc.get("fields") or {})
        current = int(fields.get(field, "0") or 0)
        new_value = current + int(amount)
        fields[field] = str(new_value)
        doc["fields"] = fields
        self._save(key, doc)
        return new_value

    def hdel(self, key: str, *fields: str) -> int:
        doc = self._load_hash(key)
        if not doc:
            return 0
        stored = dict(doc.get("fields") or {})
        removed = 0
        for field in fields:
            if field in stored:
                del stored[field]
                removed += 1
        if removed:
            doc["fields"] = stored
            self._save(key, doc)
        return removed

    # -- string operations ------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        doc = self._load(key)
        if doc and doc.get("rtype") == "string":
            return doc.get("value")
        return None

    def set(self, key: str, value) -> bool:
        self._save(key, {"rtype": "string", "value": str(value)})
        return True

    # -- generic key operations -------------------------------------------

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._load(key) is not None:
                self.db.delete(_coll_of(key), key)
                removed += 1
        return removed

    def expire(self, key: str, ttl_s: int) -> bool:
        """Record a TTL deadline on the doc (not enforced on read)."""
        doc = self._load(key)
        if doc is None:
            return False
        doc["_ttl_s"] = int(ttl_s)
        doc["_expires_at"] = time.time() + int(ttl_s)
        self._save(key, doc)
        return True

    def scan_iter(self, match: str = "*", count: Optional[int] = None) -> Iterator[str]:
        """Yield keys matching a Redis-style glob pattern (fnmatch).

        `count` is accepted for redis-py signature parity and ignored;
        enumeration walks the pattern's collection (or all collections
        when the pattern's first segment is itself a glob).
        """
        first_segment = match.split(":", 1)[0]
        if any(ch in first_segment for ch in "*?["):
            collections = self.db.collections()
        else:
            collections = [first_segment]

        for coll in collections:
            try:
                rows = self.db.q(coll).run()
            except Exception:
                continue
            for row in rows:
                key = row.get("_id") or row.get("key")
                if key and fnmatch.fnmatchcase(key, match):
                    yield key
