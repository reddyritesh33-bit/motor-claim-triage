"""Triage agent (LangGraph) - owns the claim from intake to final recommendation.

Stage "intake" (graph):  guard -> derive facts -> classify (LLM; rules on the
    degraded path) -> scope decision -> HAND-OFF to the coverage agent via the
    coverage queue, or straight to finalize if the claim is outside the corpus.
Stage "finalize" (on the coverage agent's reply): route (business rules over
    the verified position) -> output guardrails -> compose the recommendation ->
    persist, metrics, run cache, live quality sample.

Hand-off contract (triage -> coverage), also stored on the run record:
    {claim_type, secondary_types, entities, facts, research_question}
Reply contract (coverage -> triage): see coverage.run_coverage().
"""
from __future__ import annotations

import random
import re
import time
from datetime import date, datetime, timezone
from typing import TypedDict

from langgraph.graph import END, StateGraph

from ..common import guardrails, store
from ..common.config import get_settings
from ..common.contracts import Recommendation
from ..common.logging import get_logger
from ..common.telemetry import inject_carrier, set_attrs, tracer
from ..evaluation.metrics import run_metrics, searched_texts
from . import metrics as M
from .heuristics import THEFT, rule_based_triage
from .prompts import load_prompt
from .router import route_claim
from .runtime import LLMUnavailable, RunContext, http, llm_json

log = get_logger(__name__)
AGENT = "triage"
CLAIM_TYPES = ("own_damage", "third_party", "theft", "total_loss")
ESCALATION_PATHS = {
    "coverage": "Human review queue -> senior claims handler (coverage position not verified)",
    "scope": "Out-of-corpus queue -> product/line specialist (claim outside the indexed policy)",
    "degraded": "Manual triage queue (model unavailable; retrieved clauses attached)",
    "guardrail": "Human review queue -> claims handler (guardrail flag on input or output)",
}


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def derive_facts(f: dict) -> dict:
    """Deterministic facts computed by code (reliable; the model is told so)."""
    s = get_settings()
    p, inc, drv = f["policy"], f["incident"], f["driver"]
    dol, rep = _dt(inc["date_of_loss"]), _dt(inc["reported_at"])
    start, end = date.fromisoformat(str(p["policy_start_date"])), date.fromisoformat(str(p["policy_end_date"]))
    desc = inc.get("description", "")
    theft_or_fire = bool(THEFT.search(desc) or re.search(r"\b(fire|burn\w*|caught fire|blaze)\b", desc, re.I))
    limit_h = 24 if theft_or_fire else 15 * 24
    delay_h = round((rep - dol).total_seconds() / 3600, 1)
    est = inc.get("estimated_repair_cost_inr")
    days = (dol.date() - start).days
    return {
        "days_since_policy_start": days,
        "loss_within_policy_period": start <= dol.date() <= end,
        "intimation_delay_hours": delay_h,
        "intimation_limit_hours": limit_h,
        "late_intimation": delay_h > limit_h,
        "proximity_case": 0 <= days < 30,
        "licence_valid_at_loss": date.fromisoformat(str(drv["licence_valid_until"])) >= dol.date(),
        "vehicle_age_years": dol.year - f["vehicle"]["year_of_manufacture"],
        "estimated_repair_cost_inr": est,
        "estimate_to_idv_ratio": round(est / p["idv_inr"], 3) if est is not None else None,
        "product_in_corpus": p["product_uin"].upper() == s.corpus_product_uin.upper(),
        "vehicle_class": p["vehicle_class"],
        "cashless_requested": inc.get("cashless_requested", False),
        "police_report_filed": inc["police_report_filed"],
        "third_party_involved": inc["third_party_involved"],
        "injuries": inc["injuries"],
        "vehicle_driveable": inc["vehicle_driveable"],
    }


class IntakeState(TypedDict, total=False):
    ctx: RunContext
    fnol: dict
    facts: dict
    triage: dict
    flags: list
    degraded: bool
    in_scope: bool
    scope_reason: str


async def guard(state: IntakeState) -> dict:
    """Second line of defence (the API already rejected injection attempts):
    mask toxic language before any model sees it, and flag the run."""
    fnol, flags = state["fnol"], []
    desc = fnol["incident"]["description"]
    score, hits = guardrails.toxicity_score(desc)
    if score >= guardrails.TOXICITY_THRESHOLD:
        fnol["incident"]["description"] = guardrails.mask_toxic(desc)
        flags.append("toxic_input_masked")
        M.GUARDRAIL.labels("toxicity_input").inc()
        await state["ctx"].event("guardrail", "toxicity_input", score=round(score, 3), terms=hits)
    return {"fnol": fnol, "flags": flags}


async def derive(state: IntakeState) -> dict:
    facts = derive_facts(state["fnol"])
    await state["ctx"].event("step", "derive_facts", facts=facts)
    return {"facts": facts}


async def classify(state: IntakeState) -> dict:
    ctx, s = state["ctx"], get_settings()
    prompt = load_prompt("triage_extract", s.prompt_triage_version)
    ctx.steps += 1
    t0 = time.perf_counter()
    degraded = False
    try:
        out = await llm_json(ctx, prompt, {"fnol": state["fnol"], "derived": state["facts"]},
                             task="triage_extract", step="classify")
        if out.get("claim_type") not in CLAIM_TYPES:
            raise ValueError(f"invalid claim_type {out.get('claim_type')!r}")
    except (LLMUnavailable, ValueError) as exc:
        out = rule_based_triage(state["fnol"], state["facts"])
        degraded = isinstance(exc, LLMUnavailable)
        await ctx.event("degraded" if degraded else "step", "classify_rules_fallback", reason=str(exc)[:200],
                        output=out)
        if degraded:
            M.DEGRADED.labels(AGENT).inc()
    M.STEP_LAT.labels(AGENT, "classify").observe(time.perf_counter() - t0)
    ents = out.get("entities") or {}
    # deterministic facts override / complement model-extracted entities
    ents.update({"late_intimation": state["facts"]["late_intimation"],
                 "proximity_case": state["facts"]["proximity_case"],
                 "licence_valid_at_loss": state["facts"]["licence_valid_at_loss"]})
    out["entities"] = ents
    return {"triage": out, "degraded": degraded}


async def scope(state: IntakeState) -> dict:
    f, t = state["facts"], state["triage"]
    reasons = []
    if not f["product_in_corpus"]:
        reasons.append("policy product UIN is not the indexed product")
    if f["vehicle_class"] != "private_car":
        reasons.append(f"vehicle class '{f['vehicle_class']}' is outside the private car package policy")
    if t.get("in_scope") is False:
        reasons.append("model: " + str(t.get("out_of_scope_reason") or "outside corpus")[:200])
    await state["ctx"].event("step", "scope_decision", in_scope=not reasons, reasons=reasons)
    return {"in_scope": not reasons, "scope_reason": "; ".join(reasons)}


def _build_intake():
    g = StateGraph(IntakeState)
    for name, fn in (("guard", guard), ("derive", derive), ("classify", classify), ("scope", scope)):
        g.add_node(name, fn)
    g.set_entry_point("guard")
    g.add_edge("guard", "derive")
    g.add_edge("derive", "classify")
    g.add_edge("classify", "scope")
    g.add_edge("scope", END)
    return g.compile()


INTAKE = _build_intake()


async def run_intake(ctx: RunContext, run: dict) -> None:
    final = await INTAKE.ainvoke({"ctx": ctx, "fnol": run["request"], "flags": [], "degraded": False})
    t = final["triage"]
    handoff = {"claim_type": t["claim_type"], "secondary_types": t.get("secondary_types", []),
               "entities": t["entities"], "facts": final["facts"],
               "research_question": str(t.get("research_question", ""))[:500]}
    await store.update_run(ctx.run_id, handoff=handoff, triage_flags=final["flags"],
                           triage_degraded=final["degraded"], stage="coverage" if final["in_scope"] else "finalize",
                           sanitized_fnol=final["fnol"])
    payload = {"run_id": ctx.run_id, "correlation_id": ctx.correlation_id, "trace": inject_carrier(),
               "overrides": ctx.overrides}
    if final["in_scope"]:
        await ctx.event("handoff", "triage->coverage", packet=handoff)
        await store.enqueue("coverage", payload)
    else:
        await ctx.event("handoff", "triage->finalize", reason=final["scope_reason"])
        await store.enqueue("triage", payload | {"stage": "finalize",
                                                 "coverage": {"escalate": True, "scope": False,
                                                              "escalation_reasons": ["outside corpus: " +
                                                                                     final["scope_reason"]]}})


async def _relevance(run: dict, cov: dict, ctx: RunContext) -> list[float] | None:
    texts = searched_texts(cov)[:20]
    if not texts:
        return None
    q = run["request"]["incident"]["description"]
    try:
        r = await http().post(get_settings().retriever_url.rstrip("/") + "/similarity", headers=ctx.headers(),
                              json={"a": q[:1500], "b": texts})
        return r.json()["scores"]
    except Exception:
        return None


async def run_finalize(ctx: RunContext, run: dict, cov: dict) -> dict:
    s = get_settings()
    t0 = time.perf_counter()
    handoff = run.get("handoff") or {}
    facts = handoff.get("facts", {})
    ents = handoff.get("entities", {})
    claim_type = handoff.get("claim_type", "unknown")
    in_scope = cov.get("scope", True)
    reasons = list(cov.get("escalation_reasons", []))
    flags = list(run.get("triage_flags", []))
    paths = []
    if not in_scope:
        paths.append("scope")
    elif cov.get("degraded") or run.get("triage_degraded"):
        paths.append("degraded")
    elif cov.get("escalate"):
        paths.append("coverage")

    with tracer().start_as_current_span("triage.finalize") as span:
        route, route_reasons = (None, []) if not in_scope else route_claim(claim_type, ents, facts, cov)
        rationale, rules = guardrails.filter_output(cov.get("rationale", "") or "")
        route_reasons_f = []
        for rr in route_reasons:
            txt, r2 = guardrails.filter_output(rr)
            route_reasons_f.append(txt)
            rules += r2
        if rules:
            flags.append("output_filtered:" + ",".join(sorted(set(rules))))
            M.GUARDRAIL.labels("output_filter").inc()
            await ctx.event("guardrail", "output_filter", rules=rules)
        tox, hits = guardrails.toxicity_score(rationale)
        if tox >= guardrails.TOXICITY_THRESHOLD:
            rationale = "[rationale withheld: output failed the toxicity check]"
            flags.append("toxic_output_blocked")
            reasons.append("output failed toxicity check")
            paths.append("guardrail")
            M.GUARDRAIL.labels("toxicity_output").inc()
        if "toxic_input_masked" in flags and "guardrail" not in paths:
            reasons.append("abusive language in the FNOL was masked; handler review advised")
            paths.append("guardrail")
        escalated = bool(paths)
        rec = Recommendation(
            claim_type=claim_type if claim_type in CLAIM_TYPES else "unknown",
            entities={**ents, **{k: facts.get(k) for k in ("days_since_policy_start", "intimation_delay_hours",
                                                            "estimate_to_idv_ratio", "vehicle_age_years")}},
            coverage_position=cov.get("coverage_position", "indeterminate") if in_scope else "indeterminate",
            route=route,
            route_reasons=route_reasons_f,
            rationale=rationale or "No coverage position was formed; see escalation.",
            citations=cov.get("citations", []),
            modifiers_considered=cov.get("modifiers_considered", []),
            escalation={"escalated": escalated, "reasons": reasons,
                        "path": " | ".join(ESCALATION_PATHS[p] for p in dict.fromkeys(paths)) if escalated else ""},
            verification={k: v for k, v in (cov.get("verification") or {}).items() if k in
                          ("passed", "attempts", "failures", "checks", "top_cosine", "weak_retrieval", "skipped")},
            flags=flags,
        )
        set_attrs(span, route=route, escalated=escalated, position=rec.coverage_position)

    now = time.time()
    e2e = now - run["submitted_at"]
    status = "escalated" if escalated else "completed"
    coverage_cost = run.get("cost", {})
    total_cost = round(coverage_cost.get("total_usd", 0.0) + ctx.cost_usd, 6)
    by_step = {**coverage_cost.get("by_step", {}), **ctx.by_step}
    updated = await store.update_run(
        ctx.run_id, status=status, stage="done", result=rec.model_dump(), completed_at=now, e2e_seconds=round(e2e, 3),
        coverage={k: cov.get(k) for k in ("retrieved_ids", "retrieved_texts", "searches", "steps", "tool_calls",
                                          "draft_position", "degraded")},
        cost={"total_usd": total_cost,
              "prompt_tokens": coverage_cost.get("prompt_tokens", 0) + ctx.prompt_tokens,
              "completion_tokens": coverage_cost.get("completion_tokens", 0) + ctx.completion_tokens,
              "by_step": by_step},
        prompt_versions={**run.get("prompt_versions", {}), **ctx.prompt_versions},
    )
    await ctx.event("final", "recommendation", status=status, route=route, escalated=escalated,
                    position=rec.coverage_position, e2e_seconds=round(e2e, 3), cost_usd=total_cost)
    M.RUN_E2E.labels(status).observe(e2e)
    M.RUN_COST.observe(total_cost)
    for r in reasons:
        M.ESCALATIONS.labels(r.split(":")[0][:40]).inc()
    M.STEP_LAT.labels(AGENT, "finalize").observe(time.perf_counter() - t0)

    if s.run_cache_enabled and run.get("cache_key") and not ctx.overrides.get("no_cache") and \
            not cov.get("degraded") and not run.get("triage_degraded"):
        await store.cache_set("run:" + run["cache_key"], {"result": rec.model_dump(), "source_run": ctx.run_id},
                              s.run_cache_ttl_s)
    if in_scope and random.random() < s.live_eval_sample_rate:
        events = await store.get_events(ctx.run_id)
        m = run_metrics(updated, events, await _relevance(updated, cov, ctx))
        for k in ("groundedness", "context_relevance", "faithfulness"):
            if m[k] is not None:
                M.LIVE_QUALITY.labels(k).observe(m[k])
        M.LIVE_QUALITY.labels("hallucination_pre").observe(1.0 if m["hallucination_pre"] else 0.0)
        await store.add_quality_sample({"run_id": ctx.run_id, "ts": now, **m})
    return updated
