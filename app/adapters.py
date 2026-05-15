"""Adapter layer — translates Base44 frontend shape into platform-native shape.

Frontend sends:

    {
      "workflow": "clopidogrel" | "warfarin" | "simvastatin",
      "population": "AFR" | "AMR" | "EAS" | "EUR" | "SAS",
      "snps": [{"id": "rs4244285", "genotype": "AA"}, ...],
      "cohort_size": 1
    }

Platform expects:

    UnifiedExecutionContext.new(drug=..., gene=..., population=..., genotype=...)

This module is the ONLY place that knows about Base44's wire format. Every
downstream module talks the platform's native (drug, gene, population,
genotype) tuple.

The translation steps:

    1. workflow            -> (drug, gene) via WORKFLOW_TO_SCOPE
    2. snps[] + workflow   -> diplotype (e.g. "*1/*17") via call_diplotype()
                              using anukriti-pgx-core caller classes
    3. (drug, gene,        -> UnifiedExecutionContext via to_swarm_context()
        population,
        diplotype)

The runtime is reused across requests (warm KG / indexer / retrievers).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

# pgx-core — deterministic biomedical truth (PyPI library)
from anukriti_pgx_core import (
    CYP2C9Caller,
    CYP2C19Caller,
    SLCO1B1Caller,
    VCFVariant,
    VKORC1Caller,
)

# swarm — reasoning runtime (imported as sibling repo via PYTHONPATH)
from core.runtime import (  # type: ignore[import-not-found]
    InMemoryEventStream,
    SwarmRuntime,
    UnifiedExecutionContext,
)


# ---------------------------------------------------------------------------
# Workflow → scope tables
# ---------------------------------------------------------------------------

# Three workflows the frontend supports. The "primary gene" is what gets
# passed to the swarm runtime; warfarin's composite (CYP2C9 + VKORC1) is
# handled inside call_diplotype() by running both callers and composing.
WORKFLOW_TO_SCOPE: dict[str, tuple[str, str]] = {
    "clopidogrel": ("clopidogrel", "CYP2C19"),
    "warfarin":    ("warfarin",    "CYP2C9"),   # primary; VKORC1 in genotype
    "simvastatin": ("simvastatin", "SLCO1B1"),
}

# Required + optional rsIDs per workflow (mirrors lib/pgxRules.js v1.4.0 in
# the frontend). Used to validate incoming snps[] before calling.
WORKFLOW_RSIDS: dict[str, dict[str, list[str]]] = {
    "clopidogrel": {
        "required": ["rs4244285"],
        "optional": ["rs4986893", "rs12248560", "rs17884712"],
    },
    "warfarin": {
        # rs1799853 = CYP2C9*2; rs1057910 = CYP2C9*3; rs9923231 = VKORC1
        "required": ["rs1799853", "rs1057910", "rs9923231"],
        "optional": ["rs2108622", "rs28371686", "rs9332131"],
    },
    "simvastatin": {
        "required": ["rs4149056"],
        "optional": ["rs56101265"],
    },
}

# rsID → (gene, ref, alt) reference table. Needed because pgx-core's
# VCFVariant constructor wants ref/alt/genotype, but the frontend only
# sends rsID + genotype string like 'AA'. Source: dbSNP + CPIC tables.
RSID_REF_ALT: dict[str, tuple[str, str, str]] = {
    # CYP2C19
    "rs4244285":   ("CYP2C19", "G", "A"),  # *2
    "rs4986893":   ("CYP2C19", "G", "A"),  # *3
    "rs12248560":  ("CYP2C19", "C", "T"),  # *17
    "rs17884712":  ("CYP2C19", "G", "A"),  # *9
    # CYP2C9
    "rs1799853":   ("CYP2C9",  "C", "T"),  # *2
    "rs1057910":   ("CYP2C9",  "A", "C"),  # *3
    # VKORC1
    "rs9923231":   ("VKORC1",  "G", "A"),  # -1639G>A
    # CYP4F2 (warfarin optional)
    "rs2108622":   ("CYP4F2",  "C", "T"),  # *3
    "rs28371686":  ("CYP2C9",  "C", "G"),  # *5
    "rs9332131":   ("CYP2C9",  "AGAAATGGAAGGAGAATAATTACAA", "A"),  # *6 deletion
    # SLCO1B1
    "rs4149056":   ("SLCO1B1", "T", "C"),  # *5
    "rs56101265":  ("SLCO1B1", "A", "G"),  # *15-related
}


# ---------------------------------------------------------------------------
# Frontend request shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrontendRunRequest:
    """The shape the frontend POSTs."""

    workflow: str
    population: str
    snps: list[dict[str, str]]
    cohort_size: int = 1


# ---------------------------------------------------------------------------
# Translators
# ---------------------------------------------------------------------------


def _genotype_to_vcf_genotype(genotype_str: str, ref: str, alt: str) -> str:
    """Translate frontend 'AA' / 'AT' style to pgx-core's '0/0' / '0/1' style.

    The frontend sends nucleotide strings ('CT', 'AA'), but pgx-core's
    VCFVariant expects VCF-style genotype calls ('0/0', '0/1', '1/1').
    """
    g = (genotype_str or "").strip().upper()
    if len(g) != 2:
        # Unknown / malformed — treat as no-call (will yield reference)
        return "0/0"
    a1, a2 = g[0], g[1]

    def code(n: str) -> str:
        if n == ref:
            return "0"
        if n == alt:
            return "1"
        # Any other character -> treat as no-call ref to keep call deterministic
        return "0"

    return f"{code(a1)}/{code(a2)}"


def _build_variants_for_gene(
    snps: list[dict[str, str]],
    target_gene: str,
) -> dict[str, VCFVariant]:
    """Filter incoming snps[] to those that belong to target_gene, build VCFVariants."""
    variants: dict[str, VCFVariant] = {}
    for snp in snps:
        rsid = (snp.get("id") or snp.get("rsid") or "").strip()
        if not rsid or rsid not in RSID_REF_ALT:
            continue
        gene, ref, alt = RSID_REF_ALT[rsid]
        if gene != target_gene:
            continue
        gt = _genotype_to_vcf_genotype(snp.get("genotype", ""), ref, alt)
        variants[rsid] = VCFVariant(ref=ref, alt=alt, genotype=gt)
    return variants


def call_diplotype(workflow: str, snps: list[dict[str, str]]) -> dict[str, Any]:
    """Resolve frontend snps[] into a star-allele diplotype string.

    Returns a dict with:
        diplotype:  the star-allele string ('*1/*17') used as the swarm
                    runtime's `genotype` input.
        details:    per-gene call breakdown for transparency / debugging.
    """
    if workflow == "clopidogrel":
        variants = _build_variants_for_gene(snps, "CYP2C19")
        result = CYP2C19Caller().call(variants)
        return {
            "diplotype": result.diplotype,
            "details": {
                "CYP2C19": {
                    "diplotype": result.diplotype,
                    "phenotype": result.phenotype.phenotype,
                    "activity_score": result.phenotype.activity_score,
                },
            },
        }

    if workflow == "simvastatin":
        variants = _build_variants_for_gene(snps, "SLCO1B1")
        result = SLCO1B1Caller().call(variants)
        return {
            "diplotype": result.diplotype,
            "details": {
                "SLCO1B1": {
                    "diplotype": result.diplotype,
                    "phenotype": result.phenotype.phenotype,
                },
            },
        }

    if workflow == "warfarin":
        # Composite: CYP2C9 + VKORC1. The swarm runs against CYP2C9 as the
        # primary gene; the VKORC1 call is exposed in `details` so the
        # frontend can render the composite recommendation.
        cyp2c9_vars = _build_variants_for_gene(snps, "CYP2C9")
        vkorc1_vars = _build_variants_for_gene(snps, "VKORC1")
        cyp2c9 = CYP2C9Caller().call(cyp2c9_vars)
        vkorc1 = VKORC1Caller().call(vkorc1_vars)
        return {
            "diplotype": cyp2c9.diplotype,
            "details": {
                "CYP2C9": {
                    "diplotype": cyp2c9.diplotype,
                    "phenotype": cyp2c9.phenotype.phenotype,
                },
                "VKORC1": {
                    "diplotype": vkorc1.diplotype,
                    "phenotype": vkorc1.phenotype.phenotype,
                },
            },
        }

    raise ValueError(f"unknown workflow: {workflow!r}")


def to_swarm_context(req: FrontendRunRequest) -> tuple[UnifiedExecutionContext, dict[str, Any]]:
    """Build a SwarmRuntime context from a frontend request.

    Returns (context, calling_details) so the route handler can attach
    per-gene calling info to its response.
    """
    if req.workflow not in WORKFLOW_TO_SCOPE:
        raise ValueError(
            f"unknown workflow {req.workflow!r}; expected one of "
            f"{sorted(WORKFLOW_TO_SCOPE)}"
        )

    drug, gene = WORKFLOW_TO_SCOPE[req.workflow]
    calling = call_diplotype(req.workflow, req.snps)

    ctx = UnifiedExecutionContext.new(
        drug=drug,
        gene=gene,
        population=req.population,
        genotype=calling["diplotype"],
    )
    return ctx, calling


# ---------------------------------------------------------------------------
# Singleton runtime (warm KG / indexer / retrievers)
# ---------------------------------------------------------------------------

_runtime_lock = threading.Lock()
_runtime_instance: SwarmRuntime | None = None


def get_runtime() -> SwarmRuntime:
    """Return a process-wide SwarmRuntime; lazily warmed on first call."""
    global _runtime_instance
    if _runtime_instance is None:
        with _runtime_lock:
            if _runtime_instance is None:
                _runtime_instance = SwarmRuntime(event_stream=InMemoryEventStream())
    return _runtime_instance


def reset_runtime() -> None:
    """Test helper — drop the cached runtime so the next call rebuilds it."""
    global _runtime_instance
    with _runtime_lock:
        _runtime_instance = None
