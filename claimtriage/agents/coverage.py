"""Coverage agent (LangGraph).

Responsibility: given the triage agent's hand-off packet, research the policy
and return a VERIFIED coverage position with clause-level citations - or an
escalation. Retrieval is a set of tools the model chooses to call; the loop is
bounded by steps, tool calls and wall-clock time.

    think --(tool action)--> act --> think ...
    think --(final)--> verify --(pass)--> END
                              --(fail, retries left)--> think (feedback + strategy switch)
                              --(fail, exhausted)--> escalate
    think --(LLM unavailable / circuit open)--> fallback (degraded) --> END
    think --(budget exceeded / unparseable)--> escalate --> END
"""
from __future__ import annotations

import re
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.telemetry import set_attrs, tracer
from . import metrics as M
from .prompts import load_prompt
from .protocol import COVERAGE_ACTIONS, DECISIVE_TERMS, POSITIONS
from .runtime import BudgetExceeded, LLMUnavailable, RetrieverClient, RunContext, llm_json
from .verifier import verify_draft

log = get_logger(__name__)
AGENT = "coverage"
STATE_TEXT_CHARS = 700
MODIFIER_PREVIEW_CHARS = 240
FALLBACK_COVER_QUERY = {
    "own_damage": "loss or damage to the vehicle insured by accidental external means",
    "theft": "loss or damage to the vehicle insured by burglary housebreaking or theft",
    "third_party": "liability to third parties death bodily injury damage to property",
    "total_loss": "constructive total loss aggregate cost of repair exceeds 75% of IDV",
}


class CovState(TypedDict, total=False):
    ctx: RunContext
    fnol: dict
    handoff: dict
    retrieved: dict
    searches: list
    modifier_calls: list
    history: list
    feedback: list
    pending: dict
    draft: dict
    verification: dict
    attempts: int
    parse_failures: int
    strategy: str
    outcome: str
    escalation_reasons: list
    degraded: bool


# ------------------------------------------------------------------ helpers
def _remember(state: CovState, chunks: list[dict], source: str) -> list[str]:
    ids = []
    for c in chunks:
        cid = c["clause_id"]
        prev = state["retrieved"].get(cid, {})
        state["retrieved"][cid] = {**prev, **c, "source": prev.get("source", source),
                                   "dense_score": max(prev.get("dense_score", 0.0), c.get("dense_score", 0.0))}
        ids.append(cid)
    return ids


def _state_chars(c: dict) -> int:
    """Token budget: general exclusions and search hits are shown at length;
    other modifier clauses (conditions, deductible) as previews - the agent can
    get_clause any of them in full."""
    if c.get("source") == "modifiers" and not c.get("section", "").startswith("Section V:"):
        return MODIFIER_PREVIEW_CHARS
    return STATE_TEXT_CHARS


def _fit_budget(items: list[dict], latest: set[str], budget: int) -> list[dict]:
    """Keep the STATE block under AGENT_STATE_CHAR_BUDGET characters, so a prompt never
    outgrows the provider's per-request limit (free tiers reject over-size requests).
    Shorten older, less important passages first; general exclusions, clauses read in
    full and the newest search's results are shortened last."""
    def size() -> int:
        return sum(len(x["text"]) for x in items)

    def protected(x: dict) -> bool:
        return x["clause_id"] in latest or (x.get("section") or "").startswith("Section V:")

    for limit, include_protected in ((450, False), (300, False), (180, False), (450, True), (300, True)):
        if size() <= budget:
            break
        for x in items:
            if len(x["text"]) > limit and (include_protected or not protected(x)):
                x["text"] = x["text"][:limit]
    return items


def _model_state(state: CovState) -> dict:
    ctx = state["ctx"]
    h = state["handoff"]
    latest = set(state["searches"][-1]["result_ids"]) if state["searches"] else set()
    retrieved = [{"clause_id": cid, "doc": c.get("doc_title"), "section": c.get("section"),
                  "subsection": c.get("subsection"), "refs": c.get("refs", []),
                  "text": c["text"] if c.get("full") else c["text"][:_state_chars(c)]}
                 for cid, c in state["retrieved"].items()]
    return {
        "claim": {"claim_type": h["claim_type"], "secondary_types": h.get("secondary_types", []),
                  "description": state["fnol"]["incident"]["description"],
                  "research_question": h["research_question"], "entities": h["entities"], "facts": h["facts"]},
        "retrieved": _fit_budget(retrieved, latest, get_settings().agent_state_char_budget),
        "history": state["history"][-6:],
        "searched_queries": [s["query"] for s in state["searches"]],
        "modifier_calls": state["modifier_calls"],
        "feedback": state["feedback"],
        "budget": {"steps_left": ctx.max_steps - ctx.steps, "tool_calls_left": ctx.max_tool_calls - ctx.tool_calls,
                   "seconds_left": round(ctx.time_left())},
        "fault": ctx.overrides.get("fault"),
    }


# ------------------------------------------------------------------ nodes
async def think(state: CovState) -> dict:
    res = await _think(state)
    res.setdefault("outcome", "")
    res["feedback"] = state["feedback"]
    return res


async def _think(state: CovState) -> dict:
    ctx = state["ctx"]
    s = get_settings()
    version = ctx.overrides.get("coverage_prompt") or s.prompt_coverage_version
    prompt = load_prompt("coverage_agent", version)
    t0 = time.perf_counter()
    try:
        ctx.check_budget()
        ctx.steps += 1
        out = await llm_json(ctx, prompt, _model_state(state), task="coverage_step", step=f"think")
    except BudgetExceeded as exc:
        return {"outcome": "escalate", "escalation_reasons": [f"loop bound reached: {exc}"]}
    except LLMUnavailable as exc:
        return {"outcome": "fallback", "escalation_reasons": [f"LLM unavailable: {exc}"]}
    except ValueError:
        n = state.get("parse_failures", 0) + 1
        state["feedback"].append("Your last reply was not a single complete JSON object. Reply with JSON only and "
                                 "keep it short: rationale under 100 words, at most 4 citations, quotes under 30 words.")
        if n >= 2:
            return {"parse_failures": n, "outcome": "escalate", "escalation_reasons": ["model output unparseable"]}
        return {"parse_failures": n, "pending": {}}
    finally:
        M.STEP_LAT.labels(AGENT, "think").observe(time.perf_counter() - t0)
    action = out.get("action")
    if action not in COVERAGE_ACTIONS:
        state["feedback"].append(f"Unknown action {action!r}; choose one of {list(COVERAGE_ACTIONS)}.")
        return {"pending": {}}
    return {"pending": {"action": action, "args": out.get("args") or {}, "thought": str(out.get("thought", ""))[:400]}}


async def act(state: CovState) -> dict:
    ctx = state["ctx"]
    p = state["pending"]
    action, args = p["action"], p["args"]
    try:
        ctx.check_budget(tool=True)
    except BudgetExceeded as exc:
        return {"outcome": "escalate", "escalation_reasons": [f"loop bound reached: {exc}"]}
    ctx.tool_calls += 1
    rc = RetrieverClient(ctx)
    t0 = time.perf_counter()
    summary: Any
    ok = True
    with tracer().start_as_current_span(f"{AGENT}.tool.{action}") as span:
        set_attrs(span, run_id=ctx.run_id, args=str(args)[:300])
        try:
            if action == "search_policy":
                query = str(args.get("query", "")).strip()[:500]
                section = args.get("section") or None
                if len(query) < 3:
                    raise ValueError("search_policy needs a query")
                res = await rc.search(query, k=5, section=section)
                ids = _remember(state, res["results"], "search")
                state["searches"].append({"query": query, "section": section, "result_ids": ids,
                                          "cache_hit": res.get("cache_hit"), "mode": res.get("mode")})
                summary = [{"clause_id": r["clause_id"], "section": r["section"], "subsection": r["subsection"],
                            "score": r["score"], "cosine": r["dense_score"]} for r in res["results"]]
                await ctx.event("tool", "search_policy", query=query, section=section, mode=res.get("mode"),
                                cache_hit=res.get("cache_hit"),
                                passages=[{"clause_id": r["clause_id"], "section": r["section"],
                                           "dense_score": r["dense_score"], "text": r["text"]} for r in res["results"]])
            elif action == "get_clause":
                cid = str(args.get("clause_id", ""))
                c = await rc.clause(cid)
                if "error" in c:
                    raise ValueError(c["error"])
                _remember(state, [c | {"full": True}], "get_clause")
                summary = {"clause_id": cid, "loaded": True}
                await ctx.event("tool", "get_clause", clause_id=cid, passages=[{"clause_id": cid, "text": c["text"]}])
            elif action == "find_modifiers":
                cid = str(args.get("clause_id", ""))
                res = await rc.modifiers(cid)
                if "error" in res:
                    raise ValueError(res["error"])
                _remember(state, res["modifiers"], "modifiers")
                state["modifier_calls"].append(cid)
                unresolved = [t for t in res["referenced_terms"] if t in DECISIVE_TERMS]
                summary = {"modifiers": [m["clause_id"] for m in res["modifiers"]],
                           "referenced_terms": res["referenced_terms"],
                           "note": (f"These provisions refer to {unresolved}, which are stated elsewhere."
                                    if unresolved else "")}
                await ctx.event("tool", "find_modifiers", clause_id=cid, referenced_terms=res["referenced_terms"],
                                passages=[{"clause_id": m["clause_id"], "section": m["section"], "text": m["text"]}
                                          for m in res["modifiers"]])
            elif action == "get_fnol_field":
                path = str(args.get("path", ""))
                val: Any = state["fnol"]
                for part in path.split("."):
                    val = val.get(part) if isinstance(val, dict) else None
                summary = {"path": path, "value": val}
                await ctx.event("tool", "get_fnol_field", path=path, value=val)
            else:
                raise ValueError(f"unsupported action {action}")
        except Exception as exc:  # tool errors are observations, not crashes
            ok = False
            summary = {"error": str(exc)[:300]}
            await ctx.event("tool", action, ok=False, error=str(exc)[:300], args=args)
    M.TOOL_CALLS.labels(AGENT, action, "ok" if ok else "error").inc()
    M.STEP_LAT.labels(AGENT, f"tool.{action}").observe(time.perf_counter() - t0)
    state["history"].append({"action": action, "args": args, "thought": p.get("thought", ""), "result": summary})
    return {"pending": {}, "retrieved": state["retrieved"], "searches": state["searches"],
            "history": state["history"], "modifier_calls": state["modifier_calls"]}


async def verify(state: CovState) -> dict:
    ctx = state["ctx"]
    s = get_settings()
    draft = state["pending"]["args"]
    t0 = time.perf_counter()
    ent = {**state["handoff"]["entities"], **state["handoff"]["facts"]}
    with tracer().start_as_current_span(f"{AGENT}.verify") as span:
        result = verify_draft(draft, state["retrieved"], state["searches"], state["modifier_calls"], ent)
        llm_check = None
        if result["passed"] and draft.get("coverage_position") != "indeterminate":
            vprompt = load_prompt("verifier_support", s.prompt_verifier_version)
            vstate = {"position": draft.get("coverage_position"), "rationale": draft.get("rationale"),
                      "citations": [{"clause_id": c.get("clause_id"), "quote": c.get("quote"),
                                     "clause_text": state["retrieved"].get(c.get("clause_id"), {}).get("text", "")}
                                    for c in draft.get("citations", [])]}
            try:
                llm_check = await llm_json(ctx, vprompt, vstate, task="verify_support", step="verify_support")
                if llm_check.get("supported") is False:
                    result["passed"] = False
                    result["failures"].append({"check": "V8_model_support", "detail": "verifier model: citations do "
                                               f"not support {llm_check.get('unsupported_claims')}"})
            except (LLMUnavailable, ValueError) as exc:
                result["checks"].append({"check": "V8_model_support", "ok": None, "detail": f"skipped: {exc}"})
        set_attrs(span, passed=result["passed"], failures=[f["check"] for f in result["failures"]])
    M.STEP_LAT.labels(AGENT, "verify").observe(time.perf_counter() - t0)
    attempts = state.get("attempts", 0) + 1
    M.VERIFICATION.labels("pass" if result["passed"] else "fail").inc()
    await ctx.event("verification", "verify", attempt=attempts, passed=result["passed"], failures=result["failures"],
                    checks=result["checks"], draft=draft, model_check=llm_check)
    update: dict = {"draft": draft, "verification": result | {"attempts": attempts}, "attempts": attempts,
                    "pending": {}}
    if result["passed"]:
        reasons = []
        if draft.get("coverage_position") == "indeterminate":
            reasons.append("coverage position indeterminate: " + "; ".join(draft.get("conflicts") or
                                                                          ["evidence insufficient"]))
        elif draft.get("conflicts"):
            reasons.append("clauses conflict: " + "; ".join(map(str, draft["conflicts"]))[:300])
        if result["weak_retrieval"]:
            reasons.append(f"weak retrieval (best passage cosine {result['top_cosine']})")
        update["outcome"] = "escalate" if reasons else "verified"
        update["escalation_reasons"] = reasons
        return update
    if attempts > s.verify_max_retries:
        update["outcome"] = "escalate"
        update["escalation_reasons"] = ["answer failed verification after retries: " +
                                        "; ".join(f["detail"] for f in result["failures"])[:500]]
        return update
    # retry with feedback and a different strategy
    M.RETRIES.labels(AGENT, "verification").inc()
    state["feedback"][:] = [f"[{f['check']}] {f['detail']}" for f in result["failures"]]
    if attempts == 2:
        alt = "bm25" if ctx.overrides.get("retrieval_mode", s.retrieval_mode) != "bm25" else "dense"
        ctx.overrides["retrieval_mode"] = alt
        state["feedback"].append(f"Strategy change: retrieval switched to {alt}; try different query wording, "
                                 "and use get_clause/find_modifiers to read provisions in full.")
        update["strategy"] = alt
    update["outcome"] = "retry"
    update["feedback"] = state["feedback"]
    return update


async def fallback(state: CovState) -> dict:
    """Degraded path when the model is unavailable: deterministic retrieval of the
    cover clause, its modifiers and any decisive term, attached for a human. The
    position is always 'indeterminate' - never a confident answer."""
    ctx = state["ctx"]
    rc = RetrieverClient(ctx)
    M.DEGRADED.labels(AGENT).inc()
    q = FALLBACK_COVER_QUERY.get(state["handoff"]["claim_type"], FALLBACK_COVER_QUERY["own_damage"])
    cites = []
    try:
        res = await rc.search(q, k=3)
        _remember(state, res["results"], "fallback")
        if res["results"]:
            top = res["results"][0]
            mods = await rc.modifiers(top["clause_id"])
            _remember(state, mods.get("modifiers", []), "fallback")
            for term in [t for t in mods.get("referenced_terms", []) if t in DECISIVE_TERMS]:
                tr = await rc.search(term, k=2)
                _remember(state, tr["results"], "fallback")
            for cid in [top["clause_id"]] + [m["clause_id"] for m in mods.get("modifiers", [])[:3]]:
                text = state["retrieved"][cid]["text"]
                cites.append({"clause_id": cid, "quote": text.split("\n")[0][:200]})
        await ctx.event("degraded", "fallback_retrieval", query=q, clause_ids=list(state["retrieved"]))
    except Exception as exc:
        await ctx.event("degraded", "fallback_retrieval", ok=False, error=str(exc)[:300])
    draft = {"coverage_position": "indeterminate",
             "rationale": "The language model was unavailable, so no coverage position was formed. The clauses "
                          "most likely to govern this claim were retrieved for the handler.",
             "citations": cites, "modifiers_considered": [], "conflicts": []}
    return {"draft": draft, "degraded": True, "outcome": "escalate", "retrieved": state["retrieved"],
            "verification": {"passed": False, "skipped": "degraded path", "attempts": 0},
            "escalation_reasons": state.get("escalation_reasons", []) + ["degraded mode: retrieval-only fallback"]}


async def escalate(state: CovState) -> dict:
    await state["ctx"].event("escalation", "coverage_escalate", reasons=state.get("escalation_reasons", []))
    return {"outcome": "escalate"}


# ------------------------------------------------------------------ graph
def _after_think(state: CovState) -> str:
    if state.get("outcome") in ("escalate", "fallback"):
        return state["outcome"]
    p = state.get("pending") or {}
    if not p:
        return "think"
    return "verify" if p["action"] == "final" else "act"


def _after_act(state: CovState) -> str:
    return "escalate" if state.get("outcome") == "escalate" else "think"


def _after_verify(state: CovState) -> str:
    o = state.get("outcome")
    if o == "retry":
        return "think"
    return END if o == "verified" else "escalate"


def build_graph():
    g = StateGraph(CovState)
    g.add_node("think", think)
    g.add_node("act", act)
    g.add_node("verify", verify)
    g.add_node("fallback", fallback)
    g.add_node("escalate", escalate)
    g.set_entry_point("think")
    g.add_conditional_edges("think", _after_think, {"think": "think", "act": "act", "verify": "verify",
                                                     "escalate": "escalate", "fallback": "fallback"})
    g.add_conditional_edges("act", _after_act, {"think": "think", "escalate": "escalate"})
    g.add_conditional_edges("verify", _after_verify, {"think": "think", "escalate": "escalate", END: END})
    g.add_edge("fallback", END)
    g.add_edge("escalate", END)
    return g.compile()


GRAPH = build_graph()


async def run_coverage(ctx: RunContext, fnol: dict, handoff: dict) -> dict:
    state: CovState = {"ctx": ctx, "fnol": fnol, "handoff": handoff, "retrieved": {}, "searches": [],
                       "modifier_calls": [], "history": [], "feedback": [], "pending": {}, "attempts": 0,
                       "parse_failures": 0, "strategy": "default", "outcome": "", "escalation_reasons": [],
                       "degraded": False}
    final = await GRAPH.ainvoke(state, config={"recursion_limit": 4 * get_settings().agent_max_steps + 10})
    draft = final.get("draft") or {"coverage_position": "indeterminate", "rationale": "No position was formed.",
                                   "citations": []}
    if draft.get("coverage_position") not in POSITIONS:
        draft["coverage_position"] = "indeterminate"
    verified = final.get("outcome") == "verified"
    retrieved = final["retrieved"]
    citations = []
    quote_ok = set((final.get("verification") or {}).get("cited_ok", retrieved))
    for c in draft.get("citations", []):
        meta = retrieved.get(c.get("clause_id"), {})
        if not meta or c.get("clause_id") not in quote_ok:
            continue  # never return a citation that does not resolve to retrieved text
        citations.append({"clause_id": c["clause_id"], "quote": c.get("quote", ""), "doc_title": meta.get("doc_title"),
                          "section": meta.get("section", ""), "page": meta.get("page_start")})
    return {
        "verified": verified,
        "coverage_position": draft["coverage_position"] if verified else "indeterminate",
        "draft_position": draft.get("coverage_position"),
        "rationale": str(draft.get("rationale", ""))[:1500],
        "citations": citations,
        "modifiers_considered": [m for m in draft.get("modifiers_considered", []) if m in retrieved],
        "verification": final.get("verification", {}),
        "escalate": not verified,
        "escalation_reasons": final.get("escalation_reasons", []),
        "degraded": final.get("degraded", False),
        "retrieved_ids": list(retrieved),
        "retrieved_texts": {cid: retrieved[cid]["text"] for cid in retrieved},
        "searches": final["searches"],
        "steps": ctx.steps,
        "tool_calls": ctx.tool_calls,
    }
