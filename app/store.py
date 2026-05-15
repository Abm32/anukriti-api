"""Generic entity store used by every P2 entity router.

A thin wrapper that picks Mongo when MONGODB_URI is set, in-memory
otherwise. Same protocol as `RunStore` in `app.persistence`, but
collection-scoped — one `EntityStore("projects")`, one
`EntityStore("api_keys")`, etc.

This is *not* the run store. The run store has its own LRU semantics
and lives in `app.persistence`. The entity store is plain CRUD over
arbitrary documents.
"""
from __future__ import annotations

import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Iterable


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EntityStore:
    """One collection in the entity store. Backend chosen by env var."""

    def __init__(self, collection: str) -> None:
        self._name = collection
        uri = os.environ.get("MONGODB_URI", "").strip()
        if uri:
            from pymongo import MongoClient  # type: ignore[import-not-found]
            self._mode = "mongo"
            self._col = MongoClient(uri)[os.environ.get("MONGODB_DB", "anukriti")][collection]
            self._mem: dict[str, dict[str, Any]] = {}
            self._lock = threading.Lock()
        else:
            self._mode = "memory"
            self._mem = {}
            self._lock = threading.Lock()
            self._col = None

    # --- write ---

    def create(self, doc: dict[str, Any], *, id_prefix: str = "ent") -> dict[str, Any]:
        """Insert with a generated id; populate created_at + updated_at."""
        new = {
            "_id": doc.get("_id") or new_id(id_prefix),
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
            **{k: v for k, v in doc.items() if k != "_id"},
        }
        if self._mode == "mongo":
            self._col.insert_one(new)
        else:
            with self._lock:
                self._mem[new["_id"]] = new
        return new

    def update(self, entity_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        """Patch fields; bumps updated_at. Returns the new document or None."""
        patch = {**{k: v for k, v in patch.items() if k != "_id"}, "updated_at": utcnow_iso()}
        if self._mode == "mongo":
            doc = self._col.find_one_and_update(
                {"_id": entity_id},
                {"$set": patch},
                return_document=True,
            )
            return doc
        with self._lock:
            existing = self._mem.get(entity_id)
            if existing is None:
                return None
            existing.update(patch)
            return existing

    def delete(self, entity_id: str) -> bool:
        if self._mode == "mongo":
            return bool(self._col.delete_one({"_id": entity_id}).deleted_count)
        with self._lock:
            return self._mem.pop(entity_id, None) is not None

    # --- read ---

    def get(self, entity_id: str) -> dict[str, Any] | None:
        if self._mode == "mongo":
            return self._col.find_one({"_id": entity_id})
        with self._lock:
            return self._mem.get(entity_id)

    def list(
        self,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        sort: tuple[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        filters = filters or {}
        if self._mode == "mongo":
            cursor = self._col.find(filters)
            if sort:
                cursor = cursor.sort(*sort)
            return list(cursor.limit(int(limit)))
        with self._lock:
            rows = [d for d in self._mem.values() if _matches(d, filters)]
        if sort:
            key, direction = sort
            rows.sort(key=lambda d: d.get(key, ""), reverse=(direction < 0))
        return rows[: int(limit)]

    def count(self, filters: dict[str, Any] | None = None) -> int:
        filters = filters or {}
        if self._mode == "mongo":
            return self._col.count_documents(filters)
        with self._lock:
            return sum(1 for d in self._mem.values() if _matches(d, filters))

    # --- bulk helpers ---

    def find_one(self, filters: dict[str, Any]) -> dict[str, Any] | None:
        if self._mode == "mongo":
            return self._col.find_one(filters)
        with self._lock:
            for d in self._mem.values():
                if _matches(d, filters):
                    return d
            return None

    def increment(self, entity_id: str, field: str, by: int = 1) -> dict[str, Any] | None:
        if self._mode == "mongo":
            return self._col.find_one_and_update(
                {"_id": entity_id},
                {"$inc": {field: by}, "$set": {"updated_at": utcnow_iso()}},
                return_document=True,
            )
        with self._lock:
            doc = self._mem.get(entity_id)
            if doc is None:
                return None
            doc[field] = int(doc.get(field, 0)) + int(by)
            doc["updated_at"] = utcnow_iso()
            return doc

    @property
    def mode(self) -> str:
        return self._mode


def _matches(doc: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Tiny equality matcher for the in-memory backend."""
    for k, v in filters.items():
        if doc.get(k) != v:
            return False
    return True


# ---------------------------------------------------------------------------
# Singleton registry per collection name
# ---------------------------------------------------------------------------

_singletons: dict[str, EntityStore] = {}
_singletons_lock = threading.Lock()


def get_store(collection: str) -> EntityStore:
    if collection not in _singletons:
        with _singletons_lock:
            if collection not in _singletons:
                _singletons[collection] = EntityStore(collection)
    return _singletons[collection]


def reset_stores() -> None:
    """Test helper — drop all cached EntityStore singletons."""
    global _singletons
    with _singletons_lock:
        _singletons = {}


__all__ = ["EntityStore", "get_store", "reset_stores", "new_id", "utcnow_iso"]
