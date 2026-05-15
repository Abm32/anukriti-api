"""/api-keys router — create / list / revoke API keys.

The plaintext token is returned EXACTLY ONCE on POST /api-keys (creation).
After that only the hash is stored. Lost tokens require revoke + new key.

Auth model:
    Admin actions (create / list / revoke) require an API key with
    scope `admin.api_keys` OR the bootstrap token (env var).
    GET /api-keys/me returns the caller's own key record (any scope).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import generate_token, hash_token, require_api_key
from app.store import get_store, utcnow_iso

router = APIRouter(prefix="/api-keys", tags=["admin"])


class CreateKeyBody(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["runs.write", "runs.read"])
    quota: int | None = Field(None, ge=1, description="Requests per window; null = unlimited")
    window_secs: int = Field(3600, ge=60, le=86_400)


@router.post("")
def create_key(
    body: CreateKeyBody,
    _admin: dict = Depends(require_api_key("admin.api_keys")),
) -> dict[str, Any]:
    plaintext = generate_token()
    record = get_store("api_keys").create(
        {
            "label": body.label,
            "scopes": body.scopes,
            "quota": body.quota,
            "window_secs": body.window_secs,
            "key_hash": hash_token(plaintext),
            "key_prefix": plaintext[:11],  # 'ak_live_' + 3 chars for display
            "revoked": False,
            "window_start": utcnow_iso(),
            "window_count": 0,
        },
        id_prefix="ak",
    )
    return {
        "id": record["_id"],
        "label": record["label"],
        "scopes": record["scopes"],
        "quota": record.get("quota"),
        "window_secs": record.get("window_secs"),
        "key_prefix": record["key_prefix"],
        "token": plaintext,  # ONE-TIME shown
        "warning": "Save this token now. It cannot be recovered later.",
    }


@router.get("")
def list_keys(
    _admin: dict = Depends(require_api_key("admin.api_keys")),
) -> dict[str, Any]:
    rows = get_store("api_keys").list(limit=500, sort=("created_at", -1))
    return {
        "count": len(rows),
        "keys": [_redact(k) for k in rows],
    }


@router.delete("/{key_id}")
def revoke_key(
    key_id: str,
    _admin: dict = Depends(require_api_key("admin.api_keys")),
) -> dict[str, Any]:
    store = get_store("api_keys")
    if store.get(key_id) is None:
        raise HTTPException(404, detail={"code": "key_not_found", "id": key_id})
    store.update(key_id, {"revoked": True})
    return {"id": key_id, "revoked": True}


@router.get("/me")
def whoami(
    caller: dict = Depends(require_api_key()),
) -> dict[str, Any]:
    return _redact(caller)


def _redact(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("_id"),
        "label": record.get("label"),
        "scopes": record.get("scopes", []),
        "quota": record.get("quota"),
        "window_secs": record.get("window_secs"),
        "window_count": record.get("window_count", 0),
        "key_prefix": record.get("key_prefix"),
        "revoked": record.get("revoked", False),
        "created_at": record.get("created_at"),
    }
