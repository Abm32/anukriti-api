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


# ---------------------------------------------------------------------------
# /benchmarks
# ---------------------------------------------------------------------------


def test_get_benchmarks_returns_pinned_scenarios(client: TestClient) -> None:
    r = client.get("/benchmarks")
    assert r.status_code == 200
    body = r.json()
    # Swarm ships 12 pinned scenarios across 3 genes.
    assert body["count"] >= 12
    assert "CYP2C19" in body["by_gene"]
    assert "CYP2D6" in body["by_gene"]
    assert "HLA-B" in body["by_gene"]
    sample = body["scenarios"][0]
    for k in ("scenario_id", "gene", "drug", "population", "diplotype",
              "expected_phenotype", "expected_verdict", "description"):
        assert k in sample


# ---------------------------------------------------------------------------
# /scenarios
# ---------------------------------------------------------------------------


def test_get_scenarios_returns_three_canonical(client: TestClient) -> None:
    r = client.get("/scenarios")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    ids = {s["id"] for s in body["scenarios"]}
    assert ids == {
        "cyp2c19_clopidogrel_sas",
        "hlab_cbz_eas",
        "cyp2d6_codeine_afr",
    }


# ---------------------------------------------------------------------------
# /llm-context
# ---------------------------------------------------------------------------


def test_post_llm_context_returns_grounded_payload(client: TestClient) -> None:
    r = client.post(
        "/llm-context",
        json={
            "workflow": "clopidogrel",
            "snps": [{"id": "rs4244285"}, {"id": "rs12248560"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["workflow"] == "clopidogrel"
    assert body["drug"] == "clopidogrel"
    assert body["gene"] == "CYP2C19"
    assert body["rule_version"].startswith("anukriti-pgx-core==")
    # All 4 CYP2C19 rsIDs we annotate should be present.
    rsids = {v["rsid"] for v in body["variants"]}
    assert {"rs4244285", "rs4986893", "rs12248560", "rs17884712"} <= rsids
    # Phenotypes exposed for the workflow.
    assert any(p["name"] == "Poor Metabolizer" for p in body["phenotypes"])
    # Grounding instructions are present.
    assert any("rule_version" in s for s in body["grounding_instructions"])


def test_post_llm_context_unknown_workflow_returns_422(client: TestClient) -> None:
    r = client.post("/llm-context", json={"workflow": "garbage", "snps": []})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_workflow"


# ---------------------------------------------------------------------------
# /cohort/generate
# ---------------------------------------------------------------------------


def test_post_cohort_generate_clopidogrel_sas_is_deterministic(
    client: TestClient,
) -> None:
    body = {"workflow": "clopidogrel", "population": "SAS", "n": 100, "seed": 42}
    a = client.post("/cohort/generate", json=body).json()
    b = client.post("/cohort/generate", json=body).json()

    # Determinism: identical seed -> identical outcome distribution + patients.
    assert a["outcome_distribution"] == b["outcome_distribution"]
    assert [p["diplotype"] for p in a["patients"]] == [
        p["diplotype"] for p in b["patients"]
    ]

    # Sanity: SAS at seed=42 with 100 patients should have non-trivial PM count
    # (CYP2C19*2 freq is 0.36 in SAS; HW expects ~13% PM = ~13 patients).
    assert a["cohort_size"] == 100
    assert sum(a["outcome_distribution"].values()) == 100
    alt = a["outcome_distribution"]["alternative_recommended"]
    assert 5 <= alt <= 25, f"PM count {alt} outside expected HW range for SAS"


def test_post_cohort_generate_unsupported_workflow_returns_422(
    client: TestClient,
) -> None:
    r = client.post(
        "/cohort/generate",
        json={"workflow": "warfarin", "population": "SAS", "n": 50, "seed": 1},
    )
    # warfarin is a known workflow but cohort frequencies aren't seeded yet.
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "frequencies_unavailable"


def test_post_cohort_generate_bad_population_returns_400(client: TestClient) -> None:
    r = client.post(
        "/cohort/generate",
        json={"workflow": "clopidogrel", "population": "MARS", "n": 10, "seed": 1},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_population"


# ---------------------------------------------------------------------------
# /exports
# ---------------------------------------------------------------------------


def test_post_exports_returns_signed_envelope(client: TestClient) -> None:
    posted = client.post("/runs", json=CLOPIDOGREL_SAS_PM).json()
    run_id = posted["run_id"]

    r = client.post("/exports", json={"run_id": run_id, "format": "reproducibility"})
    assert r.status_code == 200
    body = r.json()
    assert body["algorithm"] == "HMAC-SHA256"
    assert body["key_id"]
    assert len(body["signature"]) == 64  # SHA256 hex = 64 chars
    # Payload is what the signature was computed over.
    assert body["payload"]["run_id"] == run_id
    assert body["payload"]["format"] == "reproducibility"
    assert "audit" in body["payload"] and "report" in body["payload"]


def test_post_exports_unknown_run_returns_404(client: TestClient) -> None:
    r = client.post(
        "/exports",
        json={"run_id": "run_not_real", "format": "reproducibility"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "run_not_found"


def test_post_exports_unknown_format_returns_422(client: TestClient) -> None:
    posted = client.post("/runs", json=CLOPIDOGREL_SAS_PM).json()
    r = client.post(
        "/exports",
        json={"run_id": posted["run_id"], "format": "klingon-invoice"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_format"


# ---------------------------------------------------------------------------
# Determinism between two POST /runs calls with the same input
# ---------------------------------------------------------------------------


def test_post_runs_is_deterministic_per_input(client: TestClient) -> None:
    a = client.post("/runs", json=CLOPIDOGREL_SAS_PM).json()
    b = client.post("/runs", json=CLOPIDOGREL_SAS_PM).json()
    # Identical input -> identical decision/verdict/uncertainty.
    a_suff = a["report"]["evidence_sufficiency"]
    b_suff = b["report"]["evidence_sufficiency"]
    assert a_suff["sufficiency_decision"] == b_suff["sufficiency_decision"]
    assert a_suff["verdict"] == b_suff["verdict"]
    assert a_suff["uncertainty_score"] == b_suff["uncertainty_score"]
    # Calling result is byte-identical.
    assert a["calling"]["diplotype"] == b["calling"]["diplotype"]
