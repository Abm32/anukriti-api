"""/runs router — the P1 core endpoints.

    POST /runs           Execute a single PGx run.
    GET  /runs           List recent runs (compact summaries).
    GET  /runs/{id}      Fetch a stored run for permalinks / replay.
    POST /runs/compare   Side-by-side comparison of 2-5 stored runs.

The implementation is intentionally narrow: every route handles exactly
one shape and either returns the platform's native output or maps a
controlled error class to a typed HTTPException.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.adapters import (
    FrontendRunRequest,
    WORKFLOW_RSIDS,
    WORKFLOW_TO_SCOPE,
    get_runtime,
    to_swarm_context,
)
from app.persistence import get_run_store, new_run_id, utcnow_iso

router = APIRouter(prefix="/runs", tags=["runs"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class SnpInput(BaseModel):
    id: str = Field(..., description="rsID (e.g. rs4244285)")
    genotype: str = Field(..., description="Nucleotide pair, e.g. 'AA' or 'CT'")


class RunRequestBody(BaseModel):
    workflow: str = Field(..., description="One of clopidogrel|warfarin|simvastatin")
    population: str = Field(..., description="3-letter SuperPopulation code")
    snps: list[SnpInput] = Field(..., description="Resolved snps; never raw VCF")
    cohort_size: int = Field(1, ge=1, le=10_000)


class CompareRequestBody(BaseModel):
    run_ids: list[str] = Field(..., min_length=2, max_length=5)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_workflow(workflow: str) -> None:
    if workflow not in WORKFLOW_TO_SCOPE:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_workflow",
                "workflow": workflow,
                "supported": sorted(WORKFLOW_TO_SCOPE),
            },
        )


def _validate_required_snps(workflow: str, snps: list[SnpInput]) -> None:
    """Reject the request if any required rsID for the workflow is missing."""
    required = set(WORKFLOW_RSIDS[workflow]["required"])
    seen = {s.id for s in snps}
    missing = sorted(required - seen)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "missing_required_snps",
                "workflow": workflow,
                "missing": missing,
                "supplied": sorted(seen),
            },
        )


# ---------------------------------------------------------------------------
# POST /runs
# ---------------------------------------------------------------------------


@router.post("")
def create_run(body: RunRequestBody) -> dict[str, Any]:
    """Execute the swarm runtime over the resolved diplotype + population.

    Returns the full report and event stream so the frontend can render
    every panel from a single response.
    """
    _validate_workflow(body.workflow)
    _validate_required_snps(body.workflow, body.snps)

    # Build the platform-native context (also resolves the diplotype).
    try:
        ctx, calling = to_swarm_context(
            FrontendRunRequest(
                workflow=body.workflow,
                population=body.population,
                snps=[s.model_dump() for s in body.snps],
                cohort_size=body.cohort_size,
            )
        )
    except ValueError as exc:
        # Population enum violation, etc.
        raise HTTPException(status_code=400, detail={"code": "bad_scope", "detail": str(exc)})

    # Run the swarm. Determinism + safety (R1..R12, V1..V10, U1..U9) live here.
    runtime = get_runtime()
    report = runtime.run(ctx)
    events = list(runtime.event_stream.events)
    # Drain the event stream so the next run's events don't include this one.
    runtime.event_stream.events.clear()  # type: ignore[attr-defined]

    # Build the audit envelope the GovernanceStrip + ReproducibilityButton consume.
    suff = report.evidence_sufficiency or {}
    audit = {
        "report_id": report.report_id,
        "correlation_id": report.correlation_id,
        "decision": suff.get("sufficiency_decision"),
        "verdict": suff.get("verdict"),
        "uncertainty": suff.get("uncertainty_score"),
        "deterministic_rules": list(report.deterministic_rules),
        "rule_version": _rule_version(),
        "generated_at": report.generated_at.isoformat(),
    }

    run_id = new_run_id()
    document = {
        "workflow": body.workflow,
        "population": body.population,
        "cohort_size": body.cohort_size,
        "report": report.to_dict(),
        "events": [e.to_dict() for e in events],
        "calling": calling,
        "audit": audit,
        "created_at": utcnow_iso(),
    }
    get_run_store().save(run_id, document)

    return {
        "run_id": run_id,
        "audit": audit,
        "calling": calling,
        "report": document["report"],
        "events": document["events"],
        "event_count": len(events),
    }


# ---------------------------------------------------------------------------
# GET /runs
# ---------------------------------------------------------------------------


@router.get("")
def list_runs(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    items = get_run_store().recent(limit=limit)
    return {"runs": items, "count": len(items)}


# ---------------------------------------------------------------------------
# GET /runs/{run_id}
# ---------------------------------------------------------------------------


@router.get("/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    doc = get_run_store().get(run_id)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "run_not_found", "run_id": run_id},
        )
    # Strip Mongo's _id key if present; expose run_id explicitly.
    return {
        "run_id": doc.get("_id", run_id),
        "audit": doc.get("audit", {}),
        "calling": doc.get("calling", {}),
        "report": doc.get("report", {}),
        "events": doc.get("events", []),
        "event_count": len(doc.get("events", [])),
    }


# ---------------------------------------------------------------------------
# POST /runs/compare
# ---------------------------------------------------------------------------


@router.post("/compare")
def compare_runs(body: CompareRequestBody) -> dict[str, Any]:
    store = get_run_store()
    runs: list[dict[str, Any]] = []
    missing: list[str] = []
    for rid in body.run_ids:
        doc = store.get(rid)
        if doc is None:
            missing.append(rid)
        else:
            runs.append(
                {
                    "run_id": doc.get("_id", rid),
                    "audit": doc.get("audit", {}),
                    "report": doc.get("report", {}),
                }
            )
    if missing:
        raise HTTPException(
            status_code=404,
            detail={"code": "runs_not_found", "missing": missing},
        )
    return {
        "runs": runs,
        "diff": _compare_diff(runs),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule_version() -> str:
    """Surface the pinned pgx-core version in audit envelopes."""
    try:
        from anukriti_pgx_core import __version__ as v  # type: ignore[attr-defined]
        return f"anukriti-pgx-core=={v}"
    except Exception:
        return "anukriti-pgx-core==unknown"


def _compare_diff(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Lightweight diff for the compare view — surface decision + verdict per run."""
    rows: list[dict[str, Any]] = []
    for r in runs:
        report = r.get("report") or {}
        suff = report.get("evidence_sufficiency") or {}
        rows.append(
            {
                "run_id": r["run_id"],
                "drug": report.get("drug"),
                "gene": report.get("gene"),
                "population": report.get("population"),
                "genotype": report.get("genotype"),
                "decision": suff.get("sufficiency_decision"),
                "verdict": suff.get("verdict"),
                "uncertainty": suff.get("uncertainty_score"),
                "deterministic_rules": list(report.get("deterministic_rules") or []),
            }
        )
    return {"rows": rows}
