"""/cohort router — deterministic synthetic-cohort generation.

Wraps the swarm's `core/simulation/` types and the `demos/cohort_demo`
sampler (Hardy-Weinberg over real CPIC + 1000G allele frequencies) so
the frontend's `lib/thousandGenomes.js` can be replaced by a single
backend call.

Stage-1 guarantee (per `core/simulation/__init__.py`):
    Only public + aggregate data is used (CPIC tables, 1000 Genomes
    super-population frequencies, IndiGen, GenomeAsia Pilot). No
    controlled-access data ever touches this endpoint.

Determinism:
    A request with the same (workflow, population, n, seed) always
    produces the same cohort. The default seed is 42 (matches
    `demos/cohort_demo.RNG_SEED`).

Today the swarm only ships frequency data for CYP2C19. Other workflows
return a 422 with a clear "frequencies_unavailable" code so the
frontend can render the right empty-state.
"""
from __future__ import annotations

import random
from typing import Any

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
from demos.cohort_demo import (  # type: ignore[import-not-found]
    DIPLOTYPE_TO_PHENOTYPE,
    PHENOTYPE_TO_OUTCOME,
    POPULATION_FREQUENCIES,
    POPULATION_SOURCES,
    _canonical_diplotype,
)

from app.adapters import WORKFLOW_TO_SCOPE


router = APIRouter(prefix="/cohort", tags=["cohort"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CohortGenerateBody(BaseModel):
    workflow: str = Field(..., description="clopidogrel|warfarin|simvastatin")
    population: str = Field(..., description="3-letter SuperPopulation code")
    n: int = Field(100, ge=1, le=10_000, description="Cohort size")
    seed: int = Field(42, description="RNG seed; same seed produces same cohort")


# Workflows for which we have frequency tables seeded today.
SUPPORTED_COHORT_WORKFLOWS: set[str] = {"clopidogrel"}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/generate")
def generate_cohort(body: CohortGenerateBody) -> dict[str, Any]:
    """Deterministically sample a synthetic cohort with outcome distribution."""

    if body.workflow not in WORKFLOW_TO_SCOPE:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_workflow",
                "workflow": body.workflow,
                "supported": sorted(WORKFLOW_TO_SCOPE),
            },
        )

    if body.workflow not in SUPPORTED_COHORT_WORKFLOWS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "frequencies_unavailable",
                "workflow": body.workflow,
                "detail": (
                    f"Cohort frequencies are only seeded for "
                    f"{sorted(SUPPORTED_COHORT_WORKFLOWS)} today; "
                    "warfarin and simvastatin require KG seed expansion."
                ),
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

    if super_pop not in POPULATION_FREQUENCIES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "frequencies_unavailable_for_population",
                "population": super_pop.value,
                "supported": sorted(p.value for p in POPULATION_FREQUENCIES),
            },
        )

    drug, gene = WORKFLOW_TO_SCOPE[body.workflow]
    rng = random.Random(body.seed)

    # Build the VirtualPopulation record (validates + carries provenance).
    freqs = POPULATION_FREQUENCIES[super_pop]
    source = POPULATION_SOURCES[super_pop]
    virtual_pop = VirtualPopulation(
        super_population=super_pop,
        gene=gene,
        allele_frequencies=freqs,
        source=source,
    )

    # Sample n synthetic patients (Hardy-Weinberg, two independent allele draws).
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

        phenotype = DIPLOTYPE_TO_PHENOTYPE.get(diplotype)
        outcome = (
            PHENOTYPE_TO_OUTCOME[phenotype]
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
                "phenotype": DIPLOTYPE_TO_PHENOTYPE.get(p.diplotype, "Unknown"),
                "outcome": (
                    PHENOTYPE_TO_OUTCOME[DIPLOTYPE_TO_PHENOTYPE[p.diplotype]].value
                    if p.diplotype in DIPLOTYPE_TO_PHENOTYPE
                    else DrugSafetyOutcome.REFUSED.value
                ),
            }
            for p in patients
        ],
        "generated_at": sim_run.created_at.isoformat(),
    }
