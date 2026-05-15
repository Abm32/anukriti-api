"""anukriti-api FastAPI entry point.

Run with:

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Required PYTHONPATH (so `from core.runtime import ...` resolves to the
anukriti-swarm sibling repo without an editable install):

    export PYTHONPATH="$PYTHONPATH:../anukriti-swarm"
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.persistence import get_run_store
from app.routers import benchmarks as benchmarks_router
from app.routers import cohort as cohort_router
from app.routers import exports as exports_router
from app.routers import llm_context as llm_context_router
from app.routers import runs as runs_router


def _cors_origins() -> list[str]:
    raw = os.environ.get("CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    # Sensible defaults: Base44 dev + local Vite + localhost frontends.
    return [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]


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
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(runs_router.router)
    app.include_router(cohort_router.router)
    app.include_router(exports_router.router)
    app.include_router(llm_context_router.router)
    app.include_router(benchmarks_router.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, Any]:
        store = get_run_store()
        return {
            "status": "ok",
            "service": "anukriti-api",
            "version": __version__,
            "store": store.__class__.__name__,
            "store_size": store.count(),
        }

    @app.get("/", tags=["meta"])
    def root() -> dict[str, Any]:
        return {
            "service": "anukriti-api",
            "version": __version__,
            "endpoints": {
                "health":          "/health",
                "runs.create":     "POST /runs",
                "runs.list":       "GET  /runs",
                "runs.get":        "GET  /runs/{run_id}",
                "runs.compare":    "POST /runs/compare",
                "cohort.generate": "POST /cohort/generate",
                "exports.create":  "POST /exports",
                "llm_context":     "POST /llm-context",
                "benchmarks":      "GET  /benchmarks",
                "scenarios":       "GET  /scenarios",
                "openapi":         "/openapi.json",
                "docs":            "/docs",
            },
        }

    return app


app = build_app()
