# anukriti-api

Unified FastAPI backend for the Anukriti platform.

This is the single HTTP surface a Base44 (or any other) frontend points
at. It fuses three repos behind one URL:

| Layer | Source | Role |
|---|---|---|
| Deterministic biomedical truth | `anukriti-pgx-core==0.2.1` (PyPI) | snps[] → star-allele diplotype + phenotype |
| Reasoning runtime              | `anukriti-swarm` (sibling repo)     | KG + evidence sufficiency + verification + narrative |
| HTTP adapter                   | `anukriti-api` (this repo)          | Base44 wire format ↔ platform native |

```
   Base44 frontend
        │
        ▼
   POST /runs   { workflow, population, snps[], cohort_size }
        │
        ▼
   anukriti-api / app / adapters.py
        │  workflow + snps[] → (drug, gene, diplotype) via pgx-core callers
        ▼
   SwarmRuntime.run(UnifiedExecutionContext)
        │  5-stage lifecycle (orchestrate → retrieve → graph → sufficiency → synthesise)
        ▼
   UnifiedExecutionReport  +  RuntimeEvents
        │
        ▼
   anukriti-api  → JSON { run_id, audit, calling, report, events }
```

## Endpoints

| Method | Path | Status | Notes |
|---|---|---|---|
| GET  | `/health`        | ✅ shipped | liveness + store info |
| POST | `/runs`          | ✅ shipped | full lifecycle, returns report + events + audit |
| GET  | `/runs`          | ✅ shipped | recent runs (compact summaries) |
| GET  | `/runs/{run_id}` | ✅ shipped | permalink fetch |
| POST | `/runs/compare`  | ✅ shipped | side-by-side diff of 2-5 runs |
| POST | `/exports`       | ⏳ backlog | server-signed audit bundles |
| POST | `/cohort/generate` | ⏳ backlog | wraps `core/simulation/` cohort_demo |
| GET  | `/benchmarks`    | ⏳ backlog | wraps swarm `benchmarks/` package |

## Setup

You need both `anukriti-api` and `anukriti-swarm` checked out in the same
parent directory.

```bash
cd /home/abhimanyu/Desktop/SynthaTrial-repo/anukriti-api
python -m venv venv
source venv/bin/activate
pip install -e .[dev]

# Make the swarm runtime importable. Two options:
#   (a) add the swarm repo to PYTHONPATH each shell session
#   (b) `pip install -e ../anukriti-swarm` (preferred for dev)
export PYTHONPATH="$(pwd)/../anukriti-swarm:$PYTHONPATH"

# Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Verify:

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"anukriti-api","version":"0.1.0",
#  "store":"InMemoryRunStore","store_size":0}
```

## Run the smoke test

```bash
cd anukriti-api
source venv/bin/activate
export PYTHONPATH="$(pwd)/../anukriti-swarm:$PYTHONPATH"
pytest -q
```

## Example: end-to-end /runs call

```bash
curl -s -X POST http://localhost:8000/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "workflow": "clopidogrel",
    "population": "SAS",
    "snps": [
      {"id": "rs4244285",  "genotype": "AA"},
      {"id": "rs12248560", "genotype": "CC"}
    ],
    "cohort_size": 1
  }' | jq '{run_id, audit, calling: .calling.details.CYP2C19,
            decision: .report.evidence_sufficiency.sufficiency_decision,
            recommendation: .report.final_recommendation.text}'
```

Expected (the diplotype + phenotype come from pgx-core deterministically):

```json
{
  "run_id": "run_a3f9d7c1e8b40192",
  "audit": {
    "report_id": "1f8a2b3c4d5e6f70",
    "correlation_id": "unified_3c2a1b9d",
    "decision": "SUFFICIENT",
    "rule_version": "anukriti-pgx-core==0.2.1",
    "deterministic_rules": ["R12", "V10", "U9"],
    ...
  },
  "calling": {
    "diplotype": "*2/*2",
    "phenotype": "Poor Metabolizer",
    "activity_score": 0.0
  },
  "decision": "SUFFICIENT",
  "recommendation": "Patient is CYP2C19 *2/*2 (poor metabolizer) ..."
}
```

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `MONGODB_URI`   | unset (uses in-memory) | switch to MongoRunStore for persistence |
| `CORS_ORIGINS`  | localhost:3000, 5173    | comma-separated allowlist for frontend origins |
| `PYTHONPATH`    | —                      | must include the path to `anukriti-swarm` |

## Layout

```
anukriti-api/
├── pyproject.toml
├── requirements.txt
├── README.md
├── app/
│   ├── __init__.py
│   ├── main.py            FastAPI factory + CORS + health
│   ├── adapters.py        workflow + snps[] -> swarm context
│   ├── persistence.py     RunStore (in-memory + Mongo)
│   └── routers/
│       └── runs.py        /runs surface
└── tests/
    └── test_runs_smoke.py
```

## What's intentionally NOT here

These are real frontend needs but they belong outside the deterministic
reasoning core. Track them as separate work:

- Authentication / API keys → middleware over FastAPI
- Quotas / rate limiting → `slowapi` or `fastapi-limiter`
- Webhooks → `WebhookDispatcher` + Mongo collection
- 9 P2 entities (Project, RunComment, etc.) → standard CRUD; one router each

Each is a small, self-contained additional router. The reasoning core
(adapters → swarm runtime → store) plus the P1 business endpoints
(runs / cohort / exports / llm-context / benchmarks) are stable;
everything else stacks on top.

## Note on starlette pin

`fastapi==0.111.0` calls `on_startup` / `on_shutdown` on `starlette.Router`,
which were removed in `starlette>=0.38`. We pin `starlette>=0.37.2,<0.38`
in both `requirements.txt` and `pyproject.toml`. If you `pip install
fastapi==0.111.0` without the pin you get starlette 1.0+ and the app
crashes at import time.

The same fix should be applied to `anukriti-swarm/requirements.txt`.
