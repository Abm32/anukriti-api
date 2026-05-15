"""RunStore — persist runs so the frontend can have true permalinks.

Two backends:
    InMemoryRunStore   — default; survives until process restart.
    MongoRunStore      — opt-in via MONGODB_URI env var; survives restart,
                         supports multi-worker deployments.

Both implement the same RunStore protocol. The route handler picks one
at startup via `get_run_store()`.

Run document shape (Mongo / dict):

    {
      "_id": "run_a3f9d7c1e8b40192",
      "workflow": "clopidogrel",
      "population": "SAS",
      "cohort_size": 1,
      "report": { ... UnifiedExecutionReport.to_dict() ... },
      "events": [ ... RuntimeEvent.to_dict() in emission order ... ],
      "calling": { "CYP2C19": {"diplotype": "*1/*17", ...} },
      "audit": { "report_id": "...", "correlation_id": "...",
                 "deterministic_rules": [...], "rule_version": "...",
                 "generated_at": "..." },
      "created_at": "2026-05-15T03:12:45Z"
    }
"""
from __future__ import annotations

import os
import secrets
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Protocol


def new_run_id() -> str:
    """Generate a stable, URL-safe run identifier."""
    return f"run_{secrets.token_hex(8)}"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class RunStore(Protocol):
    def save(self, run_id: str, document: dict[str, Any]) -> None: ...
    def get(self, run_id: str) -> dict[str, Any] | None: ...
    def recent(self, limit: int = 20) -> list[dict[str, Any]]: ...
    def count(self) -> int: ...


# ---------------------------------------------------------------------------
# In-memory store (default — bounded LRU)
# ---------------------------------------------------------------------------


class InMemoryRunStore:
    """Bounded LRU store. Default capacity is 1024 runs."""

    def __init__(self, max_entries: int = 1024) -> None:
        self._max = max(1, int(max_entries))
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def save(self, run_id: str, document: dict[str, Any]) -> None:
        with self._lock:
            self._data[run_id] = document
            self._data.move_to_end(run_id)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._data.get(run_id)
            if doc is not None:
                self._data.move_to_end(run_id)
            return doc

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), self._max))
        with self._lock:
            # OrderedDict is MRU at the end; reverse to get most-recent-first.
            items = list(self._data.values())[-limit:][::-1]
        # Return compact summaries for list views (frontend GET /runs).
        return [_summarise(d) for d in items]

    def count(self) -> int:
        with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# Mongo store (opt-in)
# ---------------------------------------------------------------------------


class MongoRunStore:
    """Mongo-backed implementation. Requires `pymongo` and a `MONGODB_URI`."""

    def __init__(self, uri: str, db: str = "anukriti", collection: str = "runs") -> None:
        try:
            from pymongo import MongoClient  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "MongoRunStore requires `pymongo`; install with `pip install pymongo==4.17.0`"
            ) from exc
        self._client = MongoClient(uri)
        self._col = self._client[db][collection]
        # Index for /runs listing: most recent first.
        self._col.create_index([("created_at", -1)])

    def save(self, run_id: str, document: dict[str, Any]) -> None:
        document = {**document, "_id": run_id}
        # upsert keeps the API idempotent if the same run_id is replayed.
        self._col.replace_one({"_id": run_id}, document, upsert=True)

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self._col.find_one({"_id": run_id})

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        cursor = (
            self._col.find({}, projection=_SUMMARY_PROJECTION)
            .sort("created_at", -1)
            .limit(int(limit))
        )
        return [_summarise(d) for d in cursor]

    def count(self) -> int:
        return self._col.estimated_document_count()


_SUMMARY_PROJECTION = {
    "_id": 1,
    "workflow": 1,
    "population": 1,
    "cohort_size": 1,
    "audit": 1,
    "created_at": 1,
}


def _summarise(doc: dict[str, Any]) -> dict[str, Any]:
    """Compact projection for list endpoints — no full report bodies."""
    audit = doc.get("audit") or {}
    return {
        "run_id": doc.get("_id"),
        "workflow": doc.get("workflow"),
        "population": doc.get("population"),
        "cohort_size": doc.get("cohort_size"),
        "decision": audit.get("decision"),
        "rule_version": audit.get("rule_version"),
        "generated_at": audit.get("generated_at") or doc.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_singleton: RunStore | None = None
_singleton_lock = threading.Lock()


def get_run_store() -> RunStore:
    """Pick a backend based on MONGODB_URI; default to in-memory."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is not None:
            return _singleton
        uri = os.environ.get("MONGODB_URI", "").strip()
        if uri:
            _singleton = MongoRunStore(uri)
        else:
            _singleton = InMemoryRunStore()
    return _singleton


def reset_run_store() -> None:
    """Test helper — clear the cached singleton."""
    global _singleton
    with _singleton_lock:
        _singleton = None
