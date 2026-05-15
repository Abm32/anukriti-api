"""/exports router — server-signed audit bundles.

Produces a tamper-evident export of a cached run by HMAC-signing the
canonical JSON of the run's audit envelope + report. The signature
key is read from `EXPORT_SIGNING_KEY` env var; if unset, a per-process
random key is generated at startup so signing still works in dev (but
clients can't verify across restarts).

Format:
    {
      "run_id":     "run_...",
      "format":     "reproducibility" | "reviewer" | "partner",
      "signed_at":  "2026-05-15T...",
      "key_id":     "anukriti-api-v1" | "...env-set...",
      "algorithm":  "HMAC-SHA256",
      "payload":    { audit, report, calling, format, signed_at },
      "signature":  "<hex>"
    }

The frontend's ReproducibilityButton / ReviewerReport / PartnerPackButton
take this envelope and embed `signature` + `key_id` + `signed_at` into
the rendered PDF / JSON file. A verifier with the same shared secret
can recompute HMAC over `payload` and confirm tamper-evidence.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.persistence import get_run_store


router = APIRouter(prefix="/exports", tags=["exports"])


# ---------------------------------------------------------------------------
# Signing key — env var preferred; per-process random fallback
# ---------------------------------------------------------------------------


def _load_signing_key() -> tuple[bytes, str]:
    raw = os.environ.get("EXPORT_SIGNING_KEY", "").strip()
    if raw:
        return raw.encode("utf-8"), "env"
    # Dev fallback. Logged-once warning is intentionally light.
    return secrets.token_bytes(32), "ephemeral"


_SIGNING_KEY, _KEY_SOURCE = _load_signing_key()
_KEY_ID = os.environ.get("EXPORT_KEY_ID", "anukriti-api-v1")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


VALID_FORMATS = {"reproducibility", "reviewer", "partner"}


class ExportBody(BaseModel):
    run_id: str = Field(..., description="Cached run id from POST /runs")
    format: str = Field(
        "reproducibility",
        description="reproducibility | reviewer | partner",
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def _canonical_json(data: Any) -> bytes:
    """Stable JSON encoding for HMAC: sorted keys, no whitespace, UTC ISO."""
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


@router.post("")
def create_export(body: ExportBody) -> dict[str, Any]:
    if body.format not in VALID_FORMATS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_format",
                "format": body.format,
                "supported": sorted(VALID_FORMATS),
            },
        )

    doc = get_run_store().get(body.run_id)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "run_not_found", "run_id": body.run_id},
        )

    signed_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "run_id": body.run_id,
        "format": body.format,
        "signed_at": signed_at,
        "audit": doc.get("audit", {}),
        "calling": doc.get("calling", {}),
        "report": doc.get("report", {}),
    }

    digest = hmac.new(
        key=_SIGNING_KEY,
        msg=_canonical_json(payload),
        digestmod=hashlib.sha256,
    ).hexdigest()

    return {
        "key_id": _KEY_ID,
        "key_source": _KEY_SOURCE,
        "algorithm": "HMAC-SHA256",
        "signed_at": signed_at,
        "payload": payload,
        "signature": digest,
    }
