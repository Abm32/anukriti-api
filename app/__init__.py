"""anukriti-api — unified backend for the Anukriti platform.

This package fuses three repos behind one FastAPI surface tuned to the
Base44 frontend's contract:

    anukriti-pgx-core  (PyPI)        deterministic phenotype + gene callers
    anukriti-swarm     (sibling dir) reasoning runtime + KG + sufficiency
    anukriti           (sibling dir) (currently unused; reserved for VCF
                                      ingestion when raw VCFs are accepted)

Layout:

    app/main.py            FastAPI app + CORS + router wiring + health
    app/adapters.py        workflow + snps[]  ->  (drug, gene, population, diplotype)
    app/persistence.py     RunStore: in-memory; optional Mongo backend
    app/routers/runs.py    POST /runs, GET /runs, GET /runs/{id}, POST /runs/compare

Scope firewall (deliberately narrow):

    * No auth/quotas/webhooks in this scaffold (those are P3 backlog).
    * No raw VCF ingestion — accepts pre-resolved snps[] only (P4 PHI rule).
    * No multi-tenant session storage (run_id permalinks instead).
"""
__version__ = "0.1.0"
