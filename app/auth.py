"""API-key auth + rate-limiting + usage metering.

Auth model:
    Bearer token. Plaintext token never persists — only a SHA256 hash.
    Token format: ak_live_<32 hex>  (created via POST /api-keys).
    Hash format: hex(sha256(token)).

Storage:
    EntityStore("api_keys") with shape:
      _id          ak_<id>
      key_hash     sha256 hex of plaintext
      label        human-readable
      scopes       list[str]
      quota        int (requests per window) | null = unlimited
      window_secs  int (rate window seconds, default 3600)
      window_start iso timestamp (set on first hit each window)
      window_count int counter
      created_at, updated_at

Auth dependency:
    Use `Depends(require_api_key())` on every protected route. Pass
    `optional=True` to allow anonymous calls (e.g. /scenarios for
    public picker).

Usage metering:
    Each authenticated billable request writes a UsageRecord with the
    api_key id, route, status, and timestamp. UsageRecord is one of
    the 9 P2 entities; see `app/routers/entities.py`.

Rate-limit response headers:
    X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset.
    Returned on every authenticated response. 429 + Retry-After when
    the quota is exceeded.

Public routes (auth-exempt):
    /health, /, /openapi.json, /docs, /redoc, /scenarios, /benchmarks,
    /llm-context (pure metadata; no compute).

Onboarding bootstrap:
    POST /api-keys is itself protected. To create the first key, set
    ANUKRITI_BOOTSTRAP_TOKEN env var; clients can call
    POST /api-keys with `Authorization: Bearer <bootstrap>` to mint
    the first real key. Then revoke the bootstrap token by clearing
    the env var.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.store import get_store, utcnow_iso


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PUBLIC_ROUTES = {
    "/health",
    "/",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/scenarios",
    "/benchmarks",
    "/llm-context",
}

# Method-aware public exemptions for endpoints where one verb is public
# (form submission / public read) but others require auth.
PUBLIC_METHOD_ROUTES: dict[str, set[str]] = {
    "/pilot-leads": {"POST"},   # /pilot form submissions
    "/changelog":   {"GET"},    # public-read changelog
}

# Routes that don't write a UsageRecord even when authed (read-only or meta).
NON_BILLABLE_ROUTES = {
    "/api-keys",
    "/api-keys/me",
    "/usage",
    "/changelog",
    "/notifications",
    "/onboarding-checklist",
}


def _is_public(path: str, method: str = "") -> bool:
    if path in PUBLIC_ROUTES:
        return True
    methods = PUBLIC_METHOD_ROUTES.get(path)
    if methods and method.upper() in methods:
        return True
    if path.startswith("/docs") or path.startswith("/redoc"):
        return True
    if path == "/openapi.json":
        return True
    return False


def _is_billable(path: str) -> bool:
    if path in NON_BILLABLE_ROUTES:
        return False
    for prefix in NON_BILLABLE_ROUTES:
        if path.startswith(prefix + "/"):
            return False
    return True


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return f"ak_live_{secrets.token_hex(16)}"


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_token() -> str:
    return os.environ.get("ANUKRITI_BOOTSTRAP_TOKEN", "").strip()


def _is_bootstrap_token(plaintext: str) -> bool:
    bs = _bootstrap_token()
    if not bs:
        return False
    return secrets.compare_digest(bs, plaintext)


# ---------------------------------------------------------------------------
# Lookup + rate-limit accounting
# ---------------------------------------------------------------------------


def _lookup_key_record(plaintext: str) -> dict[str, Any] | None:
    store = get_store("api_keys")
    return store.find_one({"key_hash": hash_token(plaintext), "revoked": False})


def _accounting_apply(record: dict[str, Any]) -> dict[str, int | str]:
    """Increment the record's counter; reset window if expired.

    Returns the rate-limit headers dict.
    """
    store = get_store("api_keys")
    quota = record.get("quota")
    window_secs = int(record.get("window_secs") or 3600)
    now = datetime.now(timezone.utc)
    window_start_iso = record.get("window_start")
    window_start = (
        datetime.fromisoformat(window_start_iso)
        if window_start_iso
        else now
    )

    if (now - window_start).total_seconds() > window_secs:
        # Reset window
        store.update(
            record["_id"],
            {"window_start": now.isoformat(), "window_count": 1},
        )
        used = 1
        window_start = now
    else:
        updated = store.increment(record["_id"], "window_count", 1)
        used = int((updated or {}).get("window_count", 0))

    reset_at = window_start + timedelta(seconds=window_secs)
    headers: dict[str, int | str] = {
        "X-RateLimit-Reset": int(reset_at.timestamp()),
    }
    if quota is None:
        headers["X-RateLimit-Limit"] = "unlimited"
        headers["X-RateLimit-Remaining"] = "unlimited"
    else:
        headers["X-RateLimit-Limit"] = int(quota)
        headers["X-RateLimit-Remaining"] = max(0, int(quota) - used)
    return headers


def _quota_exceeded(record: dict[str, Any]) -> bool:
    quota = record.get("quota")
    if quota is None:
        return False
    used = int(record.get("window_count") or 0)
    return used >= int(quota)


# ---------------------------------------------------------------------------
# Dependency for routes that need scope-tagged access
# ---------------------------------------------------------------------------


def require_api_key(*scopes: str, optional: bool = False):
    """FastAPI dependency factory.

    Usage:
        @router.post(..., dependencies=[Depends(require_api_key("runs.write"))])
    """

    async def _dep(request: Request) -> dict[str, Any] | None:
        # Honor the runtime auth-disabled toggle so dev / tests work.
        if os.environ.get("ANUKRITI_AUTH_DISABLED", "").strip().lower() in {
            "1", "true", "yes",
        }:
            return {"_id": "anonymous", "scopes": ["*"], "label": "auth-disabled"}

        auth = request.headers.get("authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()

        if not token:
            if optional:
                return None
            raise HTTPException(
                status_code=401,
                detail={"code": "missing_api_key"},
            )

        # Bootstrap path
        if _is_bootstrap_token(token):
            return {"_id": "bootstrap", "scopes": ["*"], "label": "bootstrap"}

        record = _lookup_key_record(token)
        if record is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_api_key"},
            )
        if scopes:
            granted = set(record.get("scopes") or [])
            if "*" not in granted and not granted.intersection(scopes):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "insufficient_scope",
                        "required": list(scopes),
                        "granted": sorted(granted),
                    },
                )
        return record

    return _dep


# ---------------------------------------------------------------------------
# Middleware: applies auth + rate-limit + usage to every request
# ---------------------------------------------------------------------------


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Single middleware that performs:
      • public-route exemption,
      • api-key resolution,
      • rate-limit accounting + headers,
      • usage record write on billable routes.

    Per-route scope checks are still done via Depends(require_api_key(...))
    so the OpenAPI schema reflects them. The middleware is the broad gate;
    the dependency is the per-route gate.
    """

    def __init__(self, app: ASGIApp, *, enabled: bool = True) -> None:
        super().__init__(app)
        self._enabled_at_init = enabled

    def _is_enabled(self) -> bool:
        # Re-read the env var per-request so tests / runtime toggling work.
        # The constructor flag is the default when the env var is absent.
        raw = os.environ.get("ANUKRITI_AUTH_DISABLED", "").strip().lower()
        if raw in {"1", "true", "yes"}:
            return False
        return self._enabled_at_init

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not self._is_enabled() or _is_public(path, request.method):
            return await call_next(request)

        # Auth-required path. Reject requests without a Bearer token here
        # so unauth requests get a clean 401 without depending on every
        # route declaring Depends(require_api_key).
        if request.method == "OPTIONS":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse(
                {"detail": {"code": "missing_api_key"}},
                status_code=401,
            )
        token = auth.split(" ", 1)[1].strip()

        record: dict[str, Any] | None = None
        if _is_bootstrap_token(token):
            record = {"_id": "bootstrap", "scopes": ["*"], "label": "bootstrap"}
        else:
            record = _lookup_key_record(token)

        if record is None:
            return JSONResponse(
                {"detail": {"code": "invalid_api_key"}},
                status_code=401,
            )

        # Quota check (skip for bootstrap)
        if record["_id"] != "bootstrap" and _quota_exceeded(record):
            return JSONResponse(
                {"detail": {"code": "quota_exceeded"}},
                status_code=429,
                headers={"Retry-After": str(record.get("window_secs") or 3600)},
            )

        # Accounting + headers
        rl_headers = (
            _accounting_apply(record) if record["_id"] != "bootstrap" else {}
        )
        request.state.api_key = record

        response = await call_next(request)
        for k, v in rl_headers.items():
            response.headers[k] = str(v)

        # UsageRecord write on billable routes (best-effort; never fail the request)
        if record["_id"] != "bootstrap" and _is_billable(path) and 200 <= response.status_code < 500:
            try:
                get_store("usage_records").create(
                    {
                        "api_key_id": record["_id"],
                        "route": path,
                        "method": request.method,
                        "status": response.status_code,
                        "ts": utcnow_iso(),
                    },
                    id_prefix="usage",
                )
            except Exception:
                pass

        return response


__all__ = [
    "PUBLIC_ROUTES",
    "NON_BILLABLE_ROUTES",
    "APIKeyMiddleware",
    "require_api_key",
    "hash_token",
    "generate_token",
]

