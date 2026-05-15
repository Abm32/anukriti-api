"""Nine P2 entity routers as a single module.

Each entity gets a small CRUD surface tailored to its frontend usage.
None of these touch the swarm reasoning core — they're just persistence
endpoints the Base44 frontend reads/writes today via base44 entities,
moved into our backend for tenant isolation + auth + audit.

Entities + their semantics:

    Project              user-saved run blobs ("Save to cloud")
    PilotLead            sign-up form submissions on /pilot
    RunComment           comments on /runs/{id} (decision threads)
    ApiKey               handled by app/routers/api_keys.py instead
    UsageRecord          read-only listing — written by auth middleware
    OnboardingChecklist  single doc per api_key, lazy-created
    Notification         per-api_key inbox; mark read/unread
    ProjectAuditLog      per-project event log
    ChangelogEntry       admin-write, public-read

NOTE: ApiKey isn't here — it's in app/routers/api_keys.py. The other 8
share this module.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.store import get_store, utcnow_iso

router = APIRouter(tags=["entities"])


# ===========================================================================
# Project — saved run blobs
# ===========================================================================


class ProjectBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    blob_json: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


@router.post("/projects")
def create_project(
    body: ProjectBody,
    caller: dict = Depends(require_api_key("projects.write")),
) -> dict[str, Any]:
    return get_store("projects").create(
        {**body.model_dump(), "owner_api_key_id": caller.get("_id", "")},
        id_prefix="proj",
    )


@router.get("/projects")
def list_projects(
    caller: dict = Depends(require_api_key("projects.read")),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    rows = get_store("projects").list(
        filters={"owner_api_key_id": caller.get("_id", "")},
        limit=limit,
        sort=("created_at", -1),
    )
    return {"count": len(rows), "projects": rows}


@router.get("/projects/{project_id}")
def get_project(
    project_id: str,
    _caller: dict = Depends(require_api_key("projects.read")),
) -> dict[str, Any]:
    p = get_store("projects").get(project_id)
    if p is None:
        raise HTTPException(404, detail={"code": "project_not_found", "id": project_id})
    return p


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str,
    body: ProjectBody,
    _caller: dict = Depends(require_api_key("projects.write")),
) -> dict[str, Any]:
    updated = get_store("projects").update(project_id, body.model_dump(exclude_unset=True))
    if updated is None:
        raise HTTPException(404, detail={"code": "project_not_found", "id": project_id})
    return updated


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: str,
    _caller: dict = Depends(require_api_key("projects.write")),
) -> dict[str, Any]:
    if not get_store("projects").delete(project_id):
        raise HTTPException(404, detail={"code": "project_not_found", "id": project_id})
    return {"id": project_id, "deleted": True}


# ===========================================================================
# PilotLead — sign-up funnel
# ===========================================================================


class PilotLeadBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=3, max_length=200)
    organization: str = Field("", max_length=200)
    role: str = Field("", max_length=120)
    use_case: str = Field("", max_length=2000)
    source: str = Field("/pilot/new", max_length=120)


@router.post("/pilot-leads")
def create_lead(body: PilotLeadBody) -> dict[str, Any]:
    """Public — no auth required (form submission from /pilot)."""
    return get_store("pilot_leads").create(
        {**body.model_dump(), "status": "new"},
        id_prefix="lead",
    )


@router.get("/pilot-leads")
def list_leads(
    _admin: dict = Depends(require_api_key("admin.leads")),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    rows = get_store("pilot_leads").list(limit=limit, sort=("created_at", -1))
    return {"count": len(rows), "leads": rows}


# ===========================================================================
# RunComment — discussion threads on /runs/{id}
# ===========================================================================


class CommentBody(BaseModel):
    run_id: str
    body: str = Field(..., min_length=1, max_length=10_000)
    decision: str = Field("", max_length=120)


@router.post("/run-comments")
def create_comment(
    body: CommentBody,
    caller: dict = Depends(require_api_key("comments.write")),
) -> dict[str, Any]:
    return get_store("run_comments").create(
        {**body.model_dump(), "author_api_key_id": caller.get("_id", "")},
        id_prefix="cmt",
    )


@router.get("/run-comments")
def list_comments(
    run_id: str = Query(...),
    _caller: dict = Depends(require_api_key("comments.read")),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    rows = get_store("run_comments").list(
        filters={"run_id": run_id}, limit=limit, sort=("created_at", 1),
    )
    return {"count": len(rows), "run_id": run_id, "comments": rows}


@router.delete("/run-comments/{comment_id}")
def delete_comment(
    comment_id: str,
    _caller: dict = Depends(require_api_key("comments.write")),
) -> dict[str, Any]:
    if not get_store("run_comments").delete(comment_id):
        raise HTTPException(404, detail={"code": "comment_not_found", "id": comment_id})
    return {"id": comment_id, "deleted": True}


# ===========================================================================
# UsageRecord — read-only (writes happen in middleware)
# ===========================================================================


@router.get("/usage")
def list_usage(
    caller: dict = Depends(require_api_key()),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    rows = get_store("usage_records").list(
        filters={"api_key_id": caller.get("_id", "")},
        limit=limit,
        sort=("ts", -1),
    )
    # Aggregations the dashboard expects
    by_route: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for r in rows:
        by_route[r.get("route", "?")] = by_route.get(r.get("route", "?"), 0) + 1
        sc = str(r.get("status", "?"))
        by_status[sc] = by_status.get(sc, 0) + 1
    return {
        "count": len(rows),
        "records": rows,
        "by_route": by_route,
        "by_status": by_status,
    }


# ===========================================================================
# OnboardingChecklist — single doc per api_key
# ===========================================================================


class ChecklistPatch(BaseModel):
    step: str = Field(..., min_length=1, max_length=120)
    completed: bool = True


@router.get("/onboarding-checklist")
def get_checklist(
    caller: dict = Depends(require_api_key()),
) -> dict[str, Any]:
    store = get_store("onboarding_checklists")
    doc = store.find_one({"api_key_id": caller.get("_id", "")})
    if doc is None:
        # Lazy-create
        doc = store.create(
            {
                "api_key_id": caller.get("_id", ""),
                "steps": {
                    "create_first_run": False,
                    "save_to_project": False,
                    "create_api_key": False,
                    "register_webhook": False,
                    "view_audit_ledger": False,
                    "share_run_link": False,
                },
            },
            id_prefix="chk",
        )
    return doc


@router.patch("/onboarding-checklist")
def patch_checklist(
    patch: ChecklistPatch,
    caller: dict = Depends(require_api_key()),
) -> dict[str, Any]:
    store = get_store("onboarding_checklists")
    doc = store.find_one({"api_key_id": caller.get("_id", "")})
    if doc is None:
        doc = store.create(
            {"api_key_id": caller.get("_id", ""), "steps": {}},
            id_prefix="chk",
        )
    steps = dict(doc.get("steps") or {})
    steps[patch.step] = bool(patch.completed)
    return store.update(doc["_id"], {"steps": steps}) or doc


# ===========================================================================
# Notification — inbox
# ===========================================================================


class NotificationBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field("", max_length=4000)
    severity: str = Field("info", pattern="^(info|warning|error)$")
    target_api_key_id: str = Field(..., min_length=1)


@router.post("/notifications")
def create_notification(
    body: NotificationBody,
    _admin: dict = Depends(require_api_key("admin.notifications")),
) -> dict[str, Any]:
    return get_store("notifications").create(
        {**body.model_dump(), "read": False},
        id_prefix="notif",
    )


@router.get("/notifications")
def list_notifications(
    caller: dict = Depends(require_api_key()),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    rows = get_store("notifications").list(
        filters={"target_api_key_id": caller.get("_id", "")},
        limit=limit,
        sort=("created_at", -1),
    )
    return {
        "count": len(rows),
        "unread": sum(1 for r in rows if not r.get("read")),
        "notifications": rows,
    }


@router.patch("/notifications/{notif_id}")
def mark_notification(
    notif_id: str,
    read: bool = Query(True),
    caller: dict = Depends(require_api_key()),
) -> dict[str, Any]:
    store = get_store("notifications")
    doc = store.get(notif_id)
    if doc is None:
        raise HTTPException(404, detail={"code": "notification_not_found", "id": notif_id})
    if doc.get("target_api_key_id") != caller.get("_id"):
        raise HTTPException(403, detail={"code": "not_yours"})
    return store.update(notif_id, {"read": bool(read)}) or doc


# ===========================================================================
# ProjectAuditLog — append-only event log
# ===========================================================================


class AuditEntryBody(BaseModel):
    project_id: str = Field("", max_length=80)
    event: str = Field(..., min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/project-audit-logs")
def append_audit(
    body: AuditEntryBody,
    caller: dict = Depends(require_api_key()),
) -> dict[str, Any]:
    return get_store("project_audit_logs").create(
        {**body.model_dump(), "actor_api_key_id": caller.get("_id", ""), "ts": utcnow_iso()},
        id_prefix="aud",
    )


@router.get("/project-audit-logs")
def list_audit(
    project_id: str = Query(""),
    caller: dict = Depends(require_api_key()),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    filt: dict[str, Any] = {"actor_api_key_id": caller.get("_id", "")}
    if project_id:
        filt["project_id"] = project_id
    rows = get_store("project_audit_logs").list(
        filters=filt, limit=limit, sort=("ts", -1),
    )
    return {"count": len(rows), "entries": rows}


# ===========================================================================
# ChangelogEntry — public read, admin write
# ===========================================================================


class ChangelogBody(BaseModel):
    version: str = Field(..., min_length=1, max_length=40)
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field("", max_length=20_000)
    published_at: str = Field("", max_length=40)


@router.post("/changelog")
def create_changelog(
    body: ChangelogBody,
    _admin: dict = Depends(require_api_key("admin.changelog")),
) -> dict[str, Any]:
    return get_store("changelog_entries").create(
        {**body.model_dump(), "published_at": body.published_at or utcnow_iso()},
        id_prefix="chg",
    )


@router.get("/changelog")
def list_changelog(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Public — no auth required (read-only marketing surface)."""
    rows = get_store("changelog_entries").list(limit=limit, sort=("published_at", -1))
    return {"count": len(rows), "entries": rows}
