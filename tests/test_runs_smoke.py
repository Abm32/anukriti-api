"""End-to-end smoke test for the /runs endpoint.

Exercises:
    POST /runs   - resolves snps -> diplotype -> swarm context -> report
    GET /runs/{id}  - reproduces the same report via permalink
    GET /runs       - lists the recent runs
    POST /runs/compare - diffs two runs side-by-side

Runs against the in-memory RunStore (no Mongo required).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Ensure no MONGODB_URI is set so we use the in-memory store.
    os.environ.pop("MONGODB_URI", None)
    # Reset the singletons so tests are isolated from any prior state.
    from app.persistence import reset_run_store
    from app.adapters import reset_runtime

    reset_run_store()
    reset_runtime()

    from app.main import build_app

    return TestClient(build_app())


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_returns_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "anukriti-api"


# ---------------------------------------------------------------------------
# /runs — clopidogrel happy path
# ---------------------------------------------------------------------------


CLOPIDOGREL_SAS_PM = {
    "workflow": "clopidogrel",
    "population": "SAS",
    "snps": [
        {"id": "rs4244285",  "genotype": "AA"},   # *2/*2 homozygous
        {"id": "rs12248560", "genotype": "CC"},   # *17 absent
    ],
    "cohort_size": 1,
}


def test_post_runs_clopidogrel_sas_returns_full_report(client: TestClient) -> None:
    r = client.post("/runs", json=CLOPIDOGREL_SAS_PM)
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level shape
    assert body["run_id"].startswith("run_")
    assert "audit" in body and "report" in body and "events" in body
    assert body["event_count"] == len(body["events"]) >= 10

    # Audit envelope (the GovernanceStrip + ReproducibilityButton consume this)
    audit = body["audit"]
    assert audit["correlation_id"].startswith("unified_")
    assert "rule_version" in audit and audit["rule_version"].startswith("anukriti-pgx-core==")
    assert isinstance(audit["deterministic_rules"], list)

    # Report shape (UnifiedExecutionReport.to_dict)
    report = body["report"]
    assert report["drug"] == "clopidogrel"
    assert report["gene"] == "CYP2C19"
    assert report["population"] == "SAS"
    # Diplotype was derived from the snps via pgx-core's CYP2C19Caller.
    assert "/" in report["genotype"]
    assert "evidence_sufficiency" in report
    assert "final_recommendation" in report

    # Calling details (per-gene transparency)
    assert "CYP2C19" in body["calling"]["details"]


# ---------------------------------------------------------------------------
# /runs/{run_id} — permalink fetch
# ---------------------------------------------------------------------------


def test_get_run_returns_same_report(client: TestClient) -> None:
    posted = client.post("/runs", json=CLOPIDOGREL_SAS_PM).json()
    run_id = posted["run_id"]

    fetched = client.get(f"/runs/{run_id}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["run_id"] == run_id
    # The cached report should be byte-identical to the posted one.
    assert body["report"]["report_id"] == posted["report"]["report_id"]
    assert body["report"]["correlation_id"] == posted["report"]["correlation_id"]


def test_get_run_unknown_id_returns_404(client: TestClient) -> None:
    r = client.get("/runs/run_does_not_exist")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "run_not_found"


# ---------------------------------------------------------------------------
# /runs — list
# ---------------------------------------------------------------------------


def test_list_runs_returns_recent_summaries(client: TestClient) -> None:
    # Make sure at least one run exists.
    client.post("/runs", json=CLOPIDOGREL_SAS_PM)

    r = client.get("/runs?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    sample = body["runs"][0]
    assert sample["workflow"] == "clopidogrel"
    assert sample["population"] == "SAS"
    assert "rule_version" in sample


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_post_runs_unknown_workflow_returns_422(client: TestClient) -> None:
    r = client.post(
        "/runs",
        json={
            "workflow": "imaginarydrug",
            "population": "SAS",
            "snps": [{"id": "rs4244285", "genotype": "AA"}],
            "cohort_size": 1,
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_workflow"


def test_post_runs_missing_required_snp_returns_422(client: TestClient) -> None:
    r = client.post(
        "/runs",
        json={
            "workflow": "clopidogrel",
            "population": "SAS",
            "snps": [{"id": "rs12248560", "genotype": "CT"}],  # rs4244285 missing
            "cohort_size": 1,
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "missing_required_snps"
    assert "rs4244285" in r.json()["detail"]["missing"]


def test_post_runs_bad_population_returns_400(client: TestClient) -> None:
    r = client.post(
        "/runs",
        json={
            "workflow": "clopidogrel",
            "population": "MARS",
            "snps": [{"id": "rs4244285", "genotype": "AA"}],
            "cohort_size": 1,
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_scope"


# ---------------------------------------------------------------------------
# /runs/compare
# ---------------------------------------------------------------------------


def test_post_runs_compare_diffs_two_runs(client: TestClient) -> None:
    a = client.post("/runs", json={**CLOPIDOGREL_SAS_PM, "population": "SAS"}).json()
    b = client.post("/runs", json={**CLOPIDOGREL_SAS_PM, "population": "EUR"}).json()

    r = client.post("/runs/compare", json={"run_ids": [a["run_id"], b["run_id"]]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["runs"]) == 2
    assert len(body["diff"]["rows"]) == 2
    pops = {row["population"] for row in body["diff"]["rows"]}
    assert pops == {"SAS", "EUR"}
