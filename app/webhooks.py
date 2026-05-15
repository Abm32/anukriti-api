"""Webhook subscriptions + HMAC-SHA256 signed dispatch.

Two halves:

1. WebhookStore — CRUD over webhook subscriptions (an EntityStore
   wrapper with shape {url, secret, events[], active, label}).
2. WebhookDispatcher — fire-and-forget HTTP POST with HMAC signature.

Events that fire:
    run_completed     POST /runs (200, decision != safe-abstention)
    safe_abstention   POST /runs returned with allows_synthesis=False

Delivery:
    POST <url>
    Headers:
      X-Anukriti-Event:      <event_name>
      X-Anukriti-Delivery:   <uuid>
      X-Anukriti-Signature:  sha256=<hex_hmac>
      X-Anukriti-Timestamp:  <iso>
      Content-Type:          application/json
    Body: canonical JSON of the payload.

Receivers verify by recomputing
    HMAC_SHA256(secret, "<X-Anukriti-Timestamp>.<raw_body>")
and constant-time-comparing with the signature header.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import urllib.error
import urllib.request

from app.store import EntityStore, get_store, new_id, utcnow_iso


VALID_EVENTS = {"run_completed", "safe_abstention", "export_signed"}


def _canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=timestamp.encode("utf-8") + b"." + body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# Store helpers (operate on the shared EntityStore("webhooks"))
# ---------------------------------------------------------------------------


def _store() -> EntityStore:
    return get_store("webhooks")


def create_subscription(
    *,
    url: str,
    events: list[str],
    label: str = "",
    secret: str | None = None,
    api_key_id: str = "",
) -> dict[str, Any]:
    bad = [e for e in events if e not in VALID_EVENTS]
    if bad:
        raise ValueError(f"unknown event(s) {bad!r}; allowed = {sorted(VALID_EVENTS)}")
    if not url.startswith(("http://", "https://")):
        raise ValueError("webhook url must be http(s)")
    secret = secret or secrets.token_hex(24)
    return _store().create(
        {
            "url": url,
            "events": events,
            "label": label,
            "secret": secret,
            "active": True,
            "api_key_id": api_key_id,
        },
        id_prefix="wh",
    )


def list_subscriptions(*, api_key_id: str = "") -> list[dict[str, Any]]:
    filt: dict[str, Any] = {}
    if api_key_id:
        filt["api_key_id"] = api_key_id
    return _store().list(filters=filt, limit=200, sort=("created_at", -1))


def delete_subscription(sub_id: str) -> bool:
    return _store().delete(sub_id)


def get_subscription(sub_id: str) -> dict[str, Any] | None:
    return _store().get(sub_id)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="webhook")
_delivery_lock = threading.Lock()


def _deliver(subscription: dict[str, Any], event: str, payload: dict[str, Any]) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    body = _canonical_json(payload)
    signature = _sign(subscription["secret"], timestamp, body)
    delivery_id = new_id("dlv")

    headers = {
        "Content-Type": "application/json",
        "X-Anukriti-Event": event,
        "X-Anukriti-Delivery": delivery_id,
        "X-Anukriti-Signature": signature,
        "X-Anukriti-Timestamp": timestamp,
        "User-Agent": "anukriti-api/0.1.0 (+webhooks)",
    }

    req = urllib.request.Request(
        subscription["url"],
        data=body,
        method="POST",
        headers=headers,
    )
    record = {
        "delivery_id": delivery_id,
        "subscription_id": subscription["_id"],
        "event": event,
        "url": subscription["url"],
        "status_code": 0,
        "ts": timestamp,
    }
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - explicit user-supplied URL
            record["status_code"] = resp.status
    except urllib.error.HTTPError as e:
        record["status_code"] = e.code
        record["error"] = str(e)
    except Exception as e:
        record["status_code"] = -1
        record["error"] = repr(e)

    # Append to delivery log (best-effort)
    try:
        get_store("webhook_deliveries").create(record, id_prefix="dlv")
    except Exception:
        pass
    return record


def dispatch(event: str, payload: dict[str, Any]) -> int:
    """Fire `event` to every active subscription that listens for it.

    Returns the number of subscriptions queued for delivery.
    """
    if event not in VALID_EVENTS:
        return 0
    queued = 0
    for sub in _store().list(filters={"active": True}, limit=500):
        if event in (sub.get("events") or []):
            _executor.submit(_deliver, sub, event, payload)
            queued += 1
    return queued


__all__ = [
    "VALID_EVENTS",
    "create_subscription",
    "list_subscriptions",
    "delete_subscription",
    "get_subscription",
    "dispatch",
]
