"""anukriti-api FastAPI entry point.

Run with:

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Required PYTHONPATH (so `from core.runtime import ...` resolves to the
anukriti-swarm sibling repo without an editable install):

    export PYTHONPATH="$PYTHONPATH:../anukriti-swarm"

Auth:
    All endpoints except the public set in app/auth.py PUBLIC_ROUTES
    require `Authorization: Bearer ak_live_<...>` (or the bootstrap
    token in env var ANUKRITI_BOOTSTRAP_TOKEN to mint the first key).

    For local dev with no auth pressure:
        export ANUKRITI_AUTH_DISABLED=1
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.auth import APIKeyMiddleware
from app.persistence import get_run_store
from app.routers import api_keys as api_keys_router
from app.routers import benchmarks as benchmarks_router
from app.routers import cohort as cohort_router
from app.routers import entities as entities_router
from app.routers import exports as exports_router
from app.routers import llm_context as llm_context_router
from app.routers import runs as runs_router
from app.routers import webhooks_router


def _cors_origins() -> list[str]:
    raw = os.environ.get("CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]


def _auth_enabled() -> bool:
    return os.environ.get("ANUKRITI_AUTH_DISABLED", "").strip().lower() not in {
        "1", "true", "yes",
    }


def build_app() -> FastAPI:
    app = FastAPI(
        title="anukriti-api",
        version=__version__,
        description=(
            "Unified backend for the Anukriti platform — pgx-core + swarm + "
            "Base44 frontend."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "Retry-After",
        ],
    )

    app.add_middleware(APIKeyMiddleware, enabled=_auth_enabled())

    # Reasoning core
    app.include_router(runs_router.router)
    app.include_router(cohort_router.router)
    app.include_router(exports_router.router)
    app.include_router(llm_context_router.router)
    app.include_router(benchmarks_router.router)

    # Admin + persistence
    app.include_router(api_keys_router.router)
    app.include_router(webhooks_router.router)
    app.include_router(entities_router.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, Any]:
        store = get_run_store()
        return {
            "status": "ok",
            "service": "anukriti-api",
            "version": __version__,
            "store": store.__class__.__name__,
            "store_size": store.count(),
            "auth_enabled": _auth_enabled(),
        }

    @app.get("/", tags=["meta"])
    def root() -> dict[str, Any]:
        return {
            "service": "anukriti-api",
            "version": __version__,
            "endpoints": {
                "health":             "/health",
                "runs.create":        "POST /runs",
                "runs.list":          "GET  /runs",
                "runs.get":           "GET  /runs/{run_id}",
                "runs.compare":       "POST /runs/compare",
                "cohort.generate":    "POST /cohort/generate",
                "exports.create":     "POST /exports",
                "llm_context":        "POST /llm-context",
                "benchmarks":         "GET  /benchmarks",
                "scenarios":          "GET  /scenarios",
                "api_keys.create":    "POST /api-keys",
                "api_keys.list":      "GET  /api-keys",
                "api_keys.me":        "GET  /api-keys/me",
                "api_keys.revoke":    "DELETE /api-keys/{id}",
                "webhooks.create":    "POST /webhooks",
                "webhooks.list":      "GET  /webhooks",
                "webhooks.delete":    "DELETE /webhooks/{id}",
                "projects":           "/projects (POST/GET/PATCH/DELETE)",
                "pilot_leads":        "/pilot-leads (POST/GET)",
                "run_comments":       "/run-comments (POST/GET/DELETE)",
                "usage":              "GET  /usage",
                "onboarding":         "/onboarding-checklist (GET/PATCH)",
                "notifications":      "/notifications (POST/GET/PATCH)",
                "audit_log":          "/project-audit-logs (POST/GET)",
                "changelog":          "/changelog (POST/GET)",
                "openapi":            "/openapi.json",
                "docs":               "/docs",
            },
        }

    return app


app = build_app()
