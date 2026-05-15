"""/cohort router — deterministic synthetic-cohort generation.

Wraps `core/simulation/` types and `demos/cohort_workflows` (which
ships per-workflow frequency tables for all 3 frontend workflows).

Stage-1 guarantee (per `core/simulation/__init__.py`): only public +
aggregate data is used. Sources per workflow are listed in
`cohort_workflows.WORKFLOW_SOURCES`.

Determinism:
    A request with the same (workflow, population, n, seed) always
    produces the same cohort. Default seed is 42.
"""
from __future__ import annotations

import random
from typing import Any, cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# swarm — sibling repo (resolved via PYTHONPATH)
from core.models.population import SuperPopulation  # type: ignore[import-not-found]
from core.simulation import (  # type: ignore[import-not-found]
    CohortSamplingMethod,
    DrugSafetyOutcome,
    SimulationRun,
    SimulationScope,
    SyntheticPatient,
    VirtualPopulation,
)
from demos.cohort_demo import _canonical_diplotype  # type: ignore[import-not-found]
from demos.cohort_workflows import WORKFLOW_TABLES  # type: ignore[import-not-found]

from app.adapters import WORKFLOW_TO_SCOPE


router = APIRouter(prefix="/cohort", tags=["cohort"])


class CohortGenerateBody(BaseModel):
    workflow: str = Field(..., description="clopidogrel|warfarin|simvastatin")
    population: str = Field(..., description="3-letter SuperPopulation code")
    n: int = Field(100, ge=1, le=10_000, description="Cohort size")
    seed: int = Field(42, description="RNG seed; same seed produces same cohort")


@router.post("/generate")
def generate_cohort(body: CohortGenerateBody) -> dict[str, Any]:
    if body.workflow not in WORKFLOW_TO_SCOPE:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_workflow",
                "workflow": body.workflow,
                "supported": sorted(WORKFLOW_TO_SCOPE),
            },
        )
    if body.workflow not in WORKFLOW_TABLES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "frequencies_unavailable",
                "workflow": body.workflow,
                "supported": sorted(WORKFLOW_TABLES),
            },
        )

    try:
        super_pop = SuperPopulation(body.population.strip().upper())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_population",
                "population": body.population,
                "supported": [p.value for p in SuperPopulation],
            },
        )

    table = WORKFLOW_TABLES[body.workflow]
    gene = cast(str, table["gene"])
    drug = cast(str, table["drug"])
    all_freqs = cast(dict[SuperPopulation, dict[str, float]], table["freqs"])
    diplotype_to_phenotype = cast(dict[str, str], table["diplotype_to_phenotype"])
    phenotype_to_outcome = cast(dict[str, DrugSafetyOutcome], table["phenotype_to_outcome"])
    source = cast(str, table["source"])

    if super_pop not in all_freqs:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "frequencies_unavailable_for_population",
                "workflow": body.workflow,
                "population": super_pop.value,
                "supported": sorted(p.value for p in all_freqs),
            },
        )

    freqs = all_freqs[super_pop]
    rng = random.Random(body.seed)

    virtual_pop = VirtualPopulation(
        super_population=super_pop,
        gene=gene,
        allele_frequencies=freqs,
        source=source,
    )

    alleles = list(freqs.keys())
    weights = list(freqs.values())
    patients: list[SyntheticPatient] = []
    counts: dict[str, int] = {o.value: 0 for o in DrugSafetyOutcome}

    for i in range(body.n):
        a1 = rng.choices(alleles, weights=weights, k=1)[0]
        a2 = rng.choices(alleles, weights=weights, k=1)[0]
        diplotype = _canonical_diplotype(a1, a2)
        patient = SyntheticPatient(
            patient_id=f"{super_pop.value}-{i:04d}",
            super_population=super_pop,
            gene=gene,
            diplotype=diplotype,
            sampling_method=CohortSamplingMethod.HARDY_WEINBERG,
        )
        patients.append(patient)

        phenotype = diplotype_to_phenotype.get(diplotype)
        outcome = (
            phenotype_to_outcome[phenotype]
            if phenotype is not None
            else DrugSafetyOutcome.REFUSED
        )
        counts[outcome.value] += 1

    sim_run = SimulationRun(
        run_id=f"cohort_{body.workflow}_{super_pop.value}_n{body.n}_s{body.seed}",
        scope=SimulationScope.COHORT_EVIDENCE_REASONING,
        super_population=super_pop,
        gene=gene,
        drug=drug,
        cohort_size=body.n,
        sampling_method=CohortSamplingMethod.HARDY_WEINBERG,
        outcome_distribution=counts,
        source_populations=(virtual_pop,),
    )

    return {
        "scope": sim_run.scope.value,
        "run_id": sim_run.run_id,
        "workflow": body.workflow,
        "drug": drug,
        "gene": gene,
        "population": super_pop.value,
        "cohort_size": body.n,
        "seed": body.seed,
        "sampling_method": sim_run.sampling_method.value,
        "source": virtual_pop.source,
        "allele_frequencies": dict(freqs),
        "outcome_distribution": dict(counts),
        "outcome_fractions": {
            o.value: round(sim_run.outcome_fraction(o), 4) for o in DrugSafetyOutcome
        },
        "patients": [
            {
                "patient_id": p.patient_id,
                "diplotype": p.diplotype,
                "phenotype": diplotype_to_phenotype.get(p.diplotype, "Unknown"),
                "outcome": (
                    phenotype_to_outcome[diplotype_to_phenotype[p.diplotype]].value
                    if p.diplotype in diplotype_to_phenotype
                    else DrugSafetyOutcome.REFUSED.value
                ),
            }
            for p in patients
        ],
        "generated_at": sim_run.created_at.isoformat(),
    }
