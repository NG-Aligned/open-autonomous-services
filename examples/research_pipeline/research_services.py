"""Three small, independent services forming a research pipeline.

These are deliberately rule-based stubs (no LLM calls): the framework is what is
being demonstrated. Any of these functions could be swapped for an LLM-backed
implementation without touching the other services, because they only
communicate through typed claims.
"""
from __future__ import annotations

import math

DESIGN_WEIGHT = {"rct": 0.9, "cohort": 0.6, "case-control": 0.5, "case-report": 0.2}


def literature_monitor(ctx):
    """Emit the next batch of configured papers as `paper` claims; remember how far we got."""
    papers = ctx.config["papers"]
    batch = ctx.config["batch_size"]
    cursor = ctx.state.get("cursor", 0)
    new = papers[cursor:cursor + batch]
    for paper in new:
        ctx.write("paper", paper, confidence=1.0)
    ctx.state["cursor"] = cursor + len(new)
    ctx.log(f"emitted {len(new)} papers (cursor now {ctx.state['cursor']})")


def evidence_evaluator(ctx):
    """Score each not-yet-assessed paper by study design and sample size."""
    assessed = set(ctx.state.get("assessed", []))
    for claim in ctx.read("paper"):
        if claim.id in assessed:
            continue
        p = claim.content
        base = DESIGN_WEIGHT.get(p.get("design"), 0.3)
        size_factor = min(1.0, math.log10(max(p.get("n", 1), 1)) / 3)
        confidence = round(base * (0.5 + 0.5 * size_factor), 3)
        limitations = []
        if p.get("n", 0) < 100:
            limitations.append("small sample")
        if p.get("design") not in ("rct", "cohort"):
            limitations.append("weak design for causal claims")
        ctx.write("evidence.assessment",
                  {"paper_id": p["id"], "design": p.get("design"), "n": p.get("n"),
                   "limitations": limitations},
                  confidence=confidence, inputs=[claim.id])
        assessed.add(claim.id)
    ctx.state["assessed"] = sorted(assessed)


def reporting_service(ctx):
    """Maintain one current `report` claim; each new report supersedes the previous one."""
    assessments = ctx.read("evidence.assessment")
    if not assessments:
        return
    ids = sorted(a.id for a in assessments)
    if ctx.state.get("reported") == ids:
        return
    ranked = sorted(({"paper_id": a.content["paper_id"], "confidence": a.confidence} for a in assessments),
                    key=lambda r: (-r["confidence"], r["paper_id"]))
    report_id = ctx.write(
        "report",
        {"n_assessed": len(ranked), "ranked": ranked},
        confidence=min(a.confidence for a in assessments),  # weakest-link confidence
        inputs=ids,
        supersedes=ctx.state.get("last_report_id"),
    )
    ctx.state["reported"] = ids
    ctx.state["last_report_id"] = report_id
