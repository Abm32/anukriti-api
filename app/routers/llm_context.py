"""/llm-context router — backend replacement for the Base44 llmContext function.

The frontend currently calls `base44.functions.invoke('llmContext', { workflow, snps })`
and gets back a structured payload of variant metadata + grounding instructions
that it then feeds to InvokeLLM for narrative synthesis.

This endpoint produces the same shape, sourced from anukriti-pgx-core
(deterministic, CPIC-pinned) instead of a hand-curated Base44 table.

Output shape (identical to what `LlmExplainer.jsx` already consumes):

    {
      "workflow": "clopidogrel",
      "drug": "clopidogrel",
      "gene": "CYP2C19",
      "rule_version": "anukriti-pgx-core==0.2.1",
      "variants": [
        { "rsid": "rs4244285", "ref": "G", "alt": "A",
          "gene": "CYP2C19", "star_allele": "*2",
          "function": "loss-of-function", "role": "required" },
        ...
      ],
      "phenotypes": [
        { "name": "Poor Metabolizer", "activity_score": 0.0,
          "recommendation": "alternative_recommended" },
        ...
      ],
      "evidence_sources": [
        "CPIC:CYP2C19:clopidogrel:2022 (PMID:35034351, NBK84114)",
        "PharmGKB:PA166169660",
      ],
      "grounding_instructions": [
        "Use only the variant + phenotype data provided in this context.",
        "Cite the rule_version on every clinical recommendation.",
        "Refuse to synthesize if requested variant is not in the variants list.",
        ...
      ]
    }
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.adapters import (
    RSID_REF_ALT,
    WORKFLOW_RSIDS,
    WORKFLOW_TO_SCOPE,
)


router = APIRouter(prefix="/llm-context", tags=["llm"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SnpRef(BaseModel):
    id: str


class LlmContextBody(BaseModel):
    workflow: str = Field(..., description="clopidogrel|warfarin|simvastatin")
    snps: list[SnpRef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Static metadata per workflow (CPIC-aligned; deterministic)
# ---------------------------------------------------------------------------

# rsid -> star allele + function annotation. Pulled from CPIC tables;
# expand as the workflow surface grows.
RSID_ANNOTATION: dict[str, dict[str, str]] = {
    # CYP2C19
    "rs4244285":  {"star_allele": "*2",  "function": "loss-of-function"},
    "rs4986893":  {"star_allele": "*3",  "function": "loss-of-function"},
    "rs12248560": {"star_allele": "*17", "function": "increased-function"},
    "rs17884712": {"star_allele": "*9",  "function": "decreased-function"},
    # CYP2C9
    "rs1799853":  {"star_allele": "*2",  "function": "decreased-function"},
    "rs1057910":  {"star_allele": "*3",  "function": "decreased-function"},
    "rs28371686": {"star_allele": "*5",  "function": "decreased-function"},
    "rs9332131":  {"star_allele": "*6",  "function": "no-function"},
    # VKORC1
    "rs9923231":  {"star_allele": "-1639G>A", "function": "regulatory-low-dose"},
    # CYP4F2
    "rs2108622":  {"star_allele": "*3",  "function": "modifier"},
    # SLCO1B1
    "rs4149056":  {"star_allele": "*5",  "function": "decreased-function"},
    "rs56101265": {"star_allele": "*15", "function": "decreased-function"},
}


WORKFLOW_PHENOTYPES: dict[str, list[dict[str, Any]]] = {
    "clopidogrel": [
        {"name": "Ultrarapid Metabolizer",  "activity_score": 3.0, "recommendation": "recommended_as_is"},
        {"name": "Rapid Metabolizer",       "activity_score": 2.5, "recommendation": "recommended_as_is"},
        {"name": "Normal Metabolizer",      "activity_score": 2.0, "recommendation": "recommended_as_is"},
        {"name": "Intermediate Metabolizer","activity_score": 1.0, "recommendation": "recommended_with_caveat"},
        {"name": "Poor Metabolizer",        "activity_score": 0.0, "recommendation": "alternative_recommended"},
    ],
    "warfarin": [
        {"name": "Standard Sensitivity",    "activity_score": 2.0, "recommendation": "recommended_as_is"},
        {"name": "Reduced Sensitivity",     "activity_score": 1.5, "recommendation": "recommended_with_caveat"},
        {"name": "Moderately Reduced",      "activity_score": 1.0, "recommendation": "recommended_with_caveat"},
        {"name": "Significantly Reduced",   "activity_score": 0.5, "recommendation": "alternative_recommended"},
    ],
    "simvastatin": [
        {"name": "Standard Statin Dose",        "activity_score": 2.0, "recommendation": "recommended_as_is"},
        {"name": "Moderate Myopathy Risk",      "activity_score": 1.0, "recommendation": "recommended_with_caveat"},
        {"name": "High Myopathy Risk",          "activity_score": 0.5, "recommendation": "alternative_recommended"},
        {"name": "Very High Myopathy Risk",     "activity_score": 0.0, "recommendation": "alternative_recommended"},
    ],
}


WORKFLOW_EVIDENCE: dict[str, list[str]] = {
    "clopidogrel": [
        "CPIC:CYP2C19:clopidogrel:2022 (PMID:35034351, NBK84114)",
        "PharmGKB:PA166169660",
    ],
    "warfarin": [
        "CPIC:CYP2C9+VKORC1:warfarin:2017 (PMID:28198005)",
        "PharmGKB:PA166104937",
    ],
    "simvastatin": [
        "CPIC:SLCO1B1:simvastatin:2022 (PMID:35152405)",
        "PharmGKB:PA166104881",
    ],
}


GROUNDING_INSTRUCTIONS = [
    "Use only the variant + phenotype data provided in this context.",
    "Cite the rule_version exactly as supplied on every clinical recommendation.",
    "Refuse to synthesize if a requested variant is not in the variants list.",
    "Never invent dosages; if CPIC says 'no recommendation', surface that verbatim.",
    "Surface the population dimension in the narrative when it is provided.",
    "Honest refusals must name the rule id (R1..R12, V1..V10, U1..U9) when available.",
]


def _rule_version() -> str:
    try:
        from anukriti_pgx_core import __version__ as v
        return f"anukriti-pgx-core=={v}"
    except Exception:
        return "anukriti-pgx-core==unknown"


@router.post("")
def llm_context(body: LlmContextBody) -> dict[str, Any]:
    if body.workflow not in WORKFLOW_TO_SCOPE:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_workflow",
                "workflow": body.workflow,
                "supported": sorted(WORKFLOW_TO_SCOPE),
            },
        )

    drug, gene = WORKFLOW_TO_SCOPE[body.workflow]
    required_set = set(WORKFLOW_RSIDS[body.workflow]["required"])
    optional_set = set(WORKFLOW_RSIDS[body.workflow]["optional"])
    relevant_rsids = required_set | optional_set

    # Expose every variant the workflow cares about, regardless of which
    # ones the frontend sent — the LLM context is meant to be exhaustive
    # for the workflow.
    variants: list[dict[str, Any]] = []
    for rsid in sorted(relevant_rsids):
        if rsid not in RSID_REF_ALT:
            continue
        ref_gene, ref, alt = RSID_REF_ALT[rsid]
        ann = RSID_ANNOTATION.get(rsid, {})
        variants.append(
            {
                "rsid": rsid,
                "ref": ref,
                "alt": alt,
                "gene": ref_gene,
                "star_allele": ann.get("star_allele", "unknown"),
                "function": ann.get("function", "unknown"),
                "role": "required" if rsid in required_set else "optional",
            }
        )

    return {
        "workflow": body.workflow,
        "drug": drug,
        "gene": gene,
        "rule_version": _rule_version(),
        "variants": variants,
        "phenotypes": WORKFLOW_PHENOTYPES.get(body.workflow, []),
        "evidence_sources": WORKFLOW_EVIDENCE.get(body.workflow, []),
        "grounding_instructions": GROUNDING_INSTRUCTIONS,
    }
