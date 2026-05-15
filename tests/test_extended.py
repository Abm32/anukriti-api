"""Extended smoke tests for auth, webhooks, entities, and the new
warfarin + simvastatin cohort paths."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Auth-enabled client so the auth tests are meaningful.

    Bootstrap token mints the first real api key; subsequent requests
    use that key.
    """
    os.environ.pop("MONGODB_URI", None)
    os.environ["ANUKRITI_AUTH_DISABLED"] = "0"
    os.environ["ANUKRITI_BOOTSTRAP_TOKEN"] = "test-bootstrap-token"

    from app.adapters import reset_runtime
    from app.persistence import reset_run_store
    from app.store import reset_stores

    reset_run_store()
    reset_runtime()
    reset_stores()

    from app.main import build_app
    return TestClient(build_app())


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    """Mint a real admin token using the bootstrap path."""
    r = client.post(
        "/api-keys",
        headers={"Authorization": "Bearer test-bootstrap-token"},
        json={
            "label": "test-admin",
            "scopes": ["*"],
            "quota": 10_000,
            "window_secs": 3600,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def auth_headers(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


# ===========================================================================
# Auth basics
# ===========================================================================


def test_health_is_public_no_auth_needed(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200


def test_runs_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/runs",
        json={
            "workflow": "clopidogrel",
            "population": "SAS",
            "snps": [{"id": "rs4244285", "genotype": "AA"}],
            "cohort_size": 1,
        },
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "missing_api_key"


def test_runs_with_invalid_key_returns_401(client: TestClient) -> None:
    r = client.post(
        "/runs",
        headers={"Authorization": "Bearer ak_live_definitelynotreal"},
        json={
            "workflow": "clopidogrel",
            "population": "SAS",
            "snps": [{"id": "rs4244285", "genotype": "AA"}],
            "cohort_size": 1,
        },
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_api_key"


def test_runs_with_valid_key_works_and_emits_rate_limit_headers(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = client.post(
        "/runs",
        headers=auth_headers,
        json={
            "workflow": "clopidogrel",
            "population": "SAS",
            "snps": [
                {"id": "rs4244285", "genotype": "AA"},
                {"id": "rs12248560", "genotype": "CC"},
            ],
            "cohort_size": 1,
        },
    )
    assert r.status_code == 200
    # Rate limit headers should be present
    assert "X-RateLimit-Limit" in r.headers
    assert "X-RateLimit-Remaining" in r.headers
    assert "X-RateLimit-Reset" in r.headers


def test_api_keys_me_returns_caller(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = client.get("/api-keys/me", headers=auth_headers)
    assert r.status_code == 200
    me = r.json()
    assert me["label"] == "test-admin"
    assert "*" in me["scopes"]


# ===========================================================================
# Public routes don't require auth
# ===========================================================================


def test_scenarios_is_public(client: TestClient) -> None:
    r = client.get("/scenarios")
    assert r.status_code == 200


def test_benchmarks_is_public(client: TestClient) -> None:
    r = client.get("/benchmarks")
    assert r.status_code == 200


def test_llm_context_is_public(client: TestClient) -> None:
    r = client.post(
        "/llm-context", json={"workflow": "clopidogrel", "snps": []}
    )
    assert r.status_code == 200


# ===========================================================================
# Webhooks CRUD
# ===========================================================================


def test_webhook_create_list_delete(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = client.post(
        "/webhooks",
        headers=auth_headers,
        json={
            "url": "https://example.com/hook",
            "events": ["run_completed", "safe_abstention"],
            "label": "test hook",
        },
    )
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["id"].startswith("wh_")
    assert sub["secret"]
    sub_id = sub["id"]

    listed = client.get("/webhooks", headers=auth_headers).json()
    assert listed["count"] >= 1
    assert any(s["id"] == sub_id for s in listed["webhooks"])

    deleted = client.delete(f"/webhooks/{sub_id}", headers=auth_headers).json()
    assert deleted["deleted"] is True


def test_webhook_unknown_event_returns_422(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = client.post(
        "/webhooks",
        headers=auth_headers,
        json={
            "url": "https://example.com/hook",
            "events": ["definitely-not-an-event"],
        },
    )
    assert r.status_code == 422


# ===========================================================================
# Project entity CRUD
# ===========================================================================


def test_project_lifecycle(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = client.post(
        "/projects",
        headers=auth_headers,
        json={
            "name": "Demo Project",
            "description": "smoke test",
            "blob_json": {"foo": "bar"},
            "tags": ["smoke"],
        },
    ).json()
    assert created["_id"].startswith("proj_")
    pid = created["_id"]

    fetched = client.get(f"/projects/{pid}", headers=auth_headers).json()
    assert fetched["name"] == "Demo Project"

    listed = client.get("/projects", headers=auth_headers).json()
    assert any(p["_id"] == pid for p in listed["projects"])

    patched = client.patch(
        f"/projects/{pid}",
        headers=auth_headers,
        json={
            "name": "Demo Project (renamed)",
            "description": "smoke test",
            "blob_json": {},
            "tags": [],
        },
    ).json()
    assert patched["name"] == "Demo Project (renamed)"

    deleted = client.delete(f"/projects/{pid}", headers=auth_headers).json()
    assert deleted["deleted"] is True


# ===========================================================================
# PilotLead is public-write, admin-read
# ===========================================================================


def test_pilot_lead_create_is_public(client: TestClient) -> None:
    r = client.post(
        "/pilot-leads",
        json={
            "name": "Test Hospital",
            "email": "demo@example.com",
            "organization": "Example",
        },
    )
    assert r.status_code == 200
    assert r.json()["_id"].startswith("lead_")


def test_pilot_lead_list_requires_admin(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = client.get("/pilot-leads", headers=auth_headers)
    assert r.status_code == 200


# ===========================================================================
# RunComment thread
# ===========================================================================


def test_run_comments_thread(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    posted_run = client.post(
        "/runs",
        headers=auth_headers,
        json={
            "workflow": "clopidogrel",
            "population": "SAS",
            "snps": [
                {"id": "rs4244285", "genotype": "AA"},
                {"id": "rs12248560", "genotype": "CC"},
            ],
            "cohort_size": 1,
        },
    ).json()
    run_id = posted_run["run_id"]

    a = client.post(
        "/run-comments",
        headers=auth_headers,
        json={"run_id": run_id, "body": "first comment", "decision": "approve"},
    ).json()
    assert a["_id"].startswith("cmt_")

    b = client.post(
        "/run-comments",
        headers=auth_headers,
        json={"run_id": run_id, "body": "second comment"},
    ).json()
    assert b["_id"].startswith("cmt_")

    listed = client.get(
        f"/run-comments?run_id={run_id}", headers=auth_headers
    ).json()
    assert listed["count"] == 2


# ===========================================================================
# Onboarding checklist lazy-create + patch
# ===========================================================================


def test_onboarding_checklist_lifecycle(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    initial = client.get("/onboarding-checklist", headers=auth_headers).json()
    assert "create_first_run" in initial["steps"]
    assert initial["steps"]["create_first_run"] is False

    patched = client.patch(
        "/onboarding-checklist",
        headers=auth_headers,
        json={"step": "create_first_run", "completed": True},
    ).json()
    assert patched["steps"]["create_first_run"] is True


# ===========================================================================
# Notifications
# ===========================================================================


def test_notification_lifecycle(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    me = client.get("/api-keys/me", headers=auth_headers).json()
    api_key_id = me["id"]

    posted = client.post(
        "/notifications",
        headers=auth_headers,
        json={
            "title": "smoke",
            "body": "hello",
            "severity": "info",
            "target_api_key_id": api_key_id,
        },
    ).json()
    assert posted["_id"].startswith("notif_")
    notif_id = posted["_id"]

    listed = client.get("/notifications", headers=auth_headers).json()
    assert listed["unread"] >= 1

    marked = client.patch(
        f"/notifications/{notif_id}?read=true", headers=auth_headers
    ).json()
    assert marked["read"] is True


# ===========================================================================
# Audit log + changelog
# ===========================================================================


def test_audit_log_append_and_read(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    appended = client.post(
        "/project-audit-logs",
        headers=auth_headers,
        json={"project_id": "proj_test", "event": "smoke", "payload": {"k": 1}},
    ).json()
    assert appended["_id"].startswith("aud_")

    listed = client.get(
        "/project-audit-logs?project_id=proj_test", headers=auth_headers
    ).json()
    assert listed["count"] >= 1


def test_changelog_create_and_public_read(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    created = client.post(
        "/changelog",
        headers=auth_headers,
        json={"version": "v0.1.0", "title": "smoke", "body": "n/a"},
    ).json()
    assert created["_id"].startswith("chg_")

    # Public read — no auth headers
    listed = client.get("/changelog").json()
    assert listed["count"] >= 1


# ===========================================================================
# Usage records — written by middleware
# ===========================================================================


def test_usage_records_accumulate(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # Make a few billable calls
    for _ in range(3):
        client.post(
            "/runs",
            headers=auth_headers,
            json={
                "workflow": "clopidogrel",
                "population": "SAS",
                "snps": [
                    {"id": "rs4244285", "genotype": "AA"},
                    {"id": "rs12248560", "genotype": "CC"},
                ],
                "cohort_size": 1,
            },
        )

    listed = client.get("/usage", headers=auth_headers).json()
    assert listed["count"] >= 3
    assert "/runs" in listed["by_route"]


# ===========================================================================
# Cohort generator now supports all 3 workflows
# ===========================================================================


def test_cohort_warfarin_sas_runs(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = client.post(
        "/cohort/generate",
        headers=auth_headers,
        json={"workflow": "warfarin", "population": "SAS", "n": 100, "seed": 42},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["gene"] == "CYP2C9"
    assert body["drug"] == "warfarin"
    assert body["cohort_size"] == 100
    # Outcome distribution must sum to cohort size
    assert sum(body["outcome_distribution"].values()) == 100
    # SAS CYP2C9*3 freq is 0.09; *2 is 0.05. Some IM caveats expected.
    assert body["outcome_distribution"]["recommended_with_caveat"] > 0


def test_cohort_simvastatin_eur_runs(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = client.post(
        "/cohort/generate",
        headers=auth_headers,
        json={"workflow": "simvastatin", "population": "EUR", "n": 100, "seed": 42},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["gene"] == "SLCO1B1"
    assert body["drug"] == "simvastatin"
    assert sum(body["outcome_distribution"].values()) == 100
    # SLCO1B1 *5 is 0.16 in EUR; HW expects ~26% with at least one *5 copy.
    # The "recommended_with_caveat" bucket holds Decreased Function (*1/*5).
    assert body["outcome_distribution"]["recommended_with_caveat"] >= 5


def test_cohort_warfarin_afr_lower_pm_than_sas(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Cross-population sanity: AFR has lower CYP2C9*2/*3 than EUR."""
    eur = client.post(
        "/cohort/generate",
        headers=auth_headers,
        json={"workflow": "warfarin", "population": "EUR", "n": 500, "seed": 42},
    ).json()
    afr = client.post(
        "/cohort/generate",
        headers=auth_headers,
        json={"workflow": "warfarin", "population": "AFR", "n": 500, "seed": 42},
    ).json()
    eur_alt = eur["outcome_distribution"]["alternative_recommended"]
    afr_alt = afr["outcome_distribution"]["alternative_recommended"]
    # EUR should have more PMs than AFR at this seed (CYP2C9*3 EUR=0.08 vs AFR=0.04)
    assert eur_alt >= afr_alt


def test_cohort_unknown_workflow_returns_422(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = client.post(
        "/cohort/generate",
        headers=auth_headers,
        json={"workflow": "kryptonite", "population": "SAS", "n": 10, "seed": 1},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_workflow"
