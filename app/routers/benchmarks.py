"""/benchmarks + /scenarios routers.

Both surface read-only data straight out of `anukriti-swarm` — no
state mutation, no LLM, no per-request cost.

GET /benchmarks
    The 12 canonical benchmark scenarios from `benchmarks/scenarios.py`
    (CYP2C19 + CYP2D6 + HLA-B across populations). Used by the
    frontend's /benchmarks page so it shows real demo data instead
    of an empty state.

GET /scenarios
    The 3 flagship scenarios the swarm ships in `/api/scenarios`:
    clopidogrel + CYP2C19 + SAS, carbamazepine + HLA-B + EAS,
    codeine + CYP2D6 + AFR. Used to populate the frontend's
    scenario picker.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

# swarm — sibling repo
from benchmarks.scenarios import ALL_SCENARIOS, BenchmarkScenario  # type: ignore[import-not-found]


router = APIRouter(tags=["benchmarks"])


def _scenario_to_dict(s: BenchmarkScenario) -> dict[str, Any]:
    return {
        "scenario_id":       s.scenario_id,
        "gene":              s.gene,
        "drug":              s.drug,
        "population":        s.population,
        "diplotype":         f"{s.allele1}/{s.allele2}",
        "expected_phenotype":s.expected_phenotype,
        "expected_risk":     s.expected_risk,
        "expected_verdict":  s.expected_verdict,
        "expected_frequency":s.expected_frequency,
        "expected_rarity":   s.expected_rarity,
        "description":       s.description,
    }


@router.get("/benchmarks")
def list_benchmarks() -> dict[str, Any]:
    """Return all 12 pinned benchmark scenarios across the 3 flagship genes."""
    rows = [_scenario_to_dict(s) for s in ALL_SCENARIOS]
    by_gene: dict[str, int] = {}
    by_population: dict[str, int] = {}
    for s in ALL_SCENARIOS:
        by_gene[s.gene] = by_gene.get(s.gene, 0) + 1
        by_population[s.population] = by_population.get(s.population, 0) + 1
    return {
        "count": len(rows),
        "scenarios": rows,
        "by_gene": by_gene,
        "by_population": by_population,
    }


@router.get("/scenarios")
def list_canonical_scenarios() -> dict[str, Any]:
    """Three swarm-flagship scenarios for the frontend picker."""
    return {
        "scenarios": [
            {
                "id": "cyp2c19_clopidogrel_sas",
                "title": "Clopidogrel + CYP2C19 + South Asian",
                "subtitle": "36% SAS carry CYP2C19*2 (loss-of-function)",
                "drug": "clopidogrel",
                "gene": "CYP2C19",
                "population": "SAS",
                "genotype": "*2/*2",
            },
            {
                "id": "hlab_cbz_eas",
                "title": "Carbamazepine + HLA-B*15:02 + East Asian",
                "subtitle": "HLA-B*15:02 carriers contraindicated for CBZ",
                "drug": "carbamazepine",
                "gene": "HLA-B",
                "population": "EAS",
                "genotype": "*15:02/positive",
            },
            {
                "id": "cyp2d6_codeine_afr",
                "title": "Codeine + CYP2D6 + African ancestry",
                "subtitle": "CYP2D6*4 PM in AFR; seed lacks AFR-specific evidence",
                "drug": "codeine",
                "gene": "CYP2D6",
                "population": "AFR",
                "genotype": "*4/*4",
            },
        ],
        "count": 3,
    }
