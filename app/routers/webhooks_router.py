"""/webhooks router — register / list / delete webhook subscriptions."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from app import webhooks as wh
from app.auth import require_api_key

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class CreateWebhookBody(BaseModel):
    url: HttpUrl
    events: list[str] = Field(..., min_length=1)
    label: str = Field("", max_length=120)


@router.post("")
def create_webhook(
    body: CreateWebhookBody,
    caller: dict = Depends(require_api_key("webhooks.write")),
) -> dict[str, Any]:
    try:
        sub = wh.create_subscription(
            url=str(body.url),
            events=body.events,
            label=body.label,
            api_key_id=caller.get("_id", ""),
        )
    except ValueError as e:
        raise HTTPException(422, detail={"code": "bad_subscription", "detail": str(e)})
    # Return secret ONCE on creation so subscriber can verify signatures.
    return {
        "id": sub["_id"],
        "url": sub["url"],
        "events": sub["events"],
        "label": sub.get("label", ""),
        "secret": sub["secret"],  # one-time
        "warning": "Save this secret now to verify HMAC signatures. It cannot be recovered later.",
    }


@router.get("")
def list_webhooks(
    caller: dict = Depends(require_api_key("webhooks.read")),
) -> dict[str, Any]:
    rows = wh.list_subscriptions(api_key_id=caller.get("_id", ""))
    return {
        "count": len(rows),
        "webhooks": [_redact(s) for s in rows],
    }


@router.delete("/{sub_id}")
def delete_webhook(
    sub_id: str,
    _caller: dict = Depends(require_api_key("webhooks.write")),
) -> dict[str, Any]:
    if not wh.delete_subscription(sub_id):
        raise HTTPException(404, detail={"code": "subscription_not_found", "id": sub_id})
    return {"id": sub_id, "deleted": True}


def _redact(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": s.get("_id"),
        "url": s.get("url"),
        "events": s.get("events", []),
        "label": s.get("label", ""),
        "active": s.get("active", True),
        "created_at": s.get("created_at"),
    }
