"""Batch evaluation from the command line (L3).

    python -m claimtriage.evaluation.run_eval --label baseline
    python -m claimtriage.evaluation.run_eval --label prompt_v1 --overrides '{"coverage_prompt":"v1"}'
    python -m claimtriage.evaluation.run_eval --label dense_only --overrides '{"retrieval_mode":"dense"}'
    python -m claimtriage.evaluation.compare eval/results/a.json eval/results/b.json

Every scenario goes through the real API (validation, queue, agents, verifier),
with the run cache disabled. Results: eval/results/<label>.json and .md.
Metric definitions: claimtriage/evaluation/metrics.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from .metrics import run_metrics, searched_texts
from .scenarios import build, load

TERMINAL = ("completed", "escalated", "failed")


def _match(expected, actual) -> bool:
    if expected is None and actual is None:
        return True
    if isinstance(expected, list):
        return actual in expected
    return expected == actual


async def run_one(c: httpx.AsyncClient, api: str, retriever: str, base: dict, sc: dict, overrides: dict,
                  timeout_s: float) -> dict:
    exp = sc["expect"]
    t0 = time.time()
    # one client id per scenario-run so a batch is not throttled by the per-client rate limit
    hdr = {"x-client-id": f"eval-{sc['id']}-{int(t0)}",
           "x-eval-overrides": json.dumps({**overrides, "no_cache": True})}
    r = await c.post(f"{api}/v1/claims", json=build(base, sc), headers=hdr)
    out = {"id": sc["id"], "title": sc["title"], "expect": exp, "http_status": r.status_code}
    if r.status_code == 422:
        body = r.json()
        out.update(outcome="rejected", reason=body.get("reason"), agent_run_started=body.get("agent_run_started"))
        out["correct"] = {"outcome": _match(exp["outcome"], "rejected"), "reason": exp.get("reason") == body["reason"]}
        return out
    if r.status_code != 202:
        out.update(outcome="error", detail=r.text[:300], correct={"outcome": False})
        return out
    run_id = r.json()["run_id"]
    run = {}
    while time.time() - t0 < timeout_s:
        run = (await c.get(f"{api}/v1/runs/{run_id}")).json()
        if run["status"] in TERMINAL:
            break
        await asyncio.sleep(1.0)
    trace = (await c.get(f"{api}/v1/runs/{run_id}/trace")).json()
    full_run, events = trace["run"], trace["events"]
    res = full_run.get("result") or {}
    outcome = "escalated" if (res.get("escalation") or {}).get("escalated") else \
        "routed" if run.get("status") == "completed" else "timeout" if run.get("status") not in TERMINAL else "failed"
    texts = (full_run.get("coverage") or {}).get("retrieved_texts") or {}
    ctx_texts = searched_texts(full_run.get("coverage") or {})
    relevance = None
    if ctx_texts:
        q = full_run["request"]["incident"]["description"]
        try:
            relevance = (await c.post(f"{retriever}/similarity", json={"a": q, "b": ctx_texts[:30]}))\
                .json()["scores"]
        except Exception:
            relevance = None
    m = run_metrics(full_run, events, relevance)
    support_hits = [s for s in sc.get("support", []) if any(s.lower() in t.lower() for t in texts.values())]
    support_cited = [s for s in sc.get("support", [])
                     if any(s.lower() in texts.get(ci["clause_id"], "").lower() for ci in res.get("citations", []))]
    llm_events = [e for e in events if e.get("kind") == "llm"]
    out.update(
        run_id=run_id, outcome=outcome, status=run.get("status"), e2e_s=full_run.get("e2e_seconds"),
        claim_type=res.get("claim_type"), position=res.get("coverage_position"), route=res.get("route"),
        escalation_reasons=(res.get("escalation") or {}).get("reasons"),
        verification_attempts=(res.get("verification") or {}).get("attempts"),
        citations=[c_["clause_id"] for c_ in res.get("citations", [])],
        support_retrieved=len(support_hits), support_cited=len(support_cited), support_total=len(sc.get("support", [])),
        cost_usd=(full_run.get("cost") or {}).get("total_usd"), cost_by_step=(full_run.get("cost") or {}).get("by_step"),
        llm_calls=len(llm_events), tool_calls=sum(1 for e in events if e.get("kind") == "tool"),
        prompt_versions=full_run.get("prompt_versions"), metrics=m,
    )
    out["correct"] = {
        "outcome": _match(exp["outcome"], outcome),
        "claim_type": _match(exp["claim_type"], res.get("claim_type")) if "claim_type" in exp else None,
        "position": _match(exp["position"], res.get("coverage_position")) if "position" in exp else None,
        "route": _match(exp["route"], res.get("route")) if "route" in exp and outcome == "routed" else None,
    }
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def summarise(results: list[dict]) -> dict:
    def acc(key):
        vals = [r["correct"].get(key) for r in results if r["correct"].get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    ran = [r for r in results if r.get("metrics")]
    sup = [r for r in ran if r.get("support_total")]
    costs = [r["cost_usd"] for r in ran if r.get("cost_usd") is not None]
    drivers: dict = {}
    for r in ran:
        for step, agg in (r.get("cost_by_step") or {}).items():
            drivers[step] = drivers.get(step, 0.0) + agg.get("cost_usd", 0.0)
    lat = sorted(r["e2e_s"] for r in ran if r.get("e2e_s") is not None)
    return {
        "scenarios": len(results),
        "accuracy": {k: acc(k) for k in ("outcome", "claim_type", "position", "route")},
        "all_correct_rate": round(sum(all(v for v in r["correct"].values() if v is not None) for r in results)
                                  / len(results), 4),
        "groundedness": _mean([r["metrics"]["groundedness"] for r in ran]),
        "context_relevance": _mean([r["metrics"]["context_relevance"] for r in ran]),
        "faithfulness": _mean([r["metrics"]["faithfulness"] for r in ran]),
        "hallucination_rate_pre_verification": _mean([1.0 if r["metrics"]["hallucination_pre"] else 0.0 for r in ran]),
        "hallucination_rate_returned": _mean([1.0 if r["metrics"]["hallucination_post"] else 0.0 for r in ran]),
        "support_passage_recall": _mean([r["support_retrieved"] / r["support_total"] for r in sup]),
        "support_passage_cited": _mean([r["support_cited"] / r["support_total"] for r in sup]),
        "mean_cost_usd_per_run": _mean(costs),
        "cost_per_100_runs_usd": round(100 * sum(costs) / len(costs), 4) if costs else None,
        "largest_cost_driver": max(drivers, key=drivers.get) if drivers else None,
        "cost_by_step_usd": {k: round(v, 6) for k, v in sorted(drivers.items(), key=lambda x: -x[1])},
        "mean_llm_calls": _mean([r["llm_calls"] for r in ran]),
        "mean_tool_calls": _mean([r["tool_calls"] for r in ran]),
        "e2e_p50_s": lat[len(lat) // 2] if lat else None,
        "e2e_p95_s": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None,
    }


def to_markdown(label: str, meta: dict, summary: dict, results: list[dict]) -> str:
    lines = [f"# Evaluation: {label}", "", f"- run at: {meta['started']}", f"- overrides: `{meta['overrides']}`",
             f"- provider: {meta.get('provider')}", "", "## Summary", "", "| metric | value |", "|---|---|"]
    for k, v in summary.items():
        if isinstance(v, dict):
            v = ", ".join(f"{a}={b}" for a, b in v.items())
        lines.append(f"| {k} | {v} |")
    lines += ["", "## Per scenario", "",
              "| id | outcome | type | position | route | support retr/cited | ground. | faith. | correct |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        m = r.get("metrics") or {}
        ok = all(v for v in r["correct"].values() if v is not None)
        lines.append(f"| {r['id']} | {r['outcome']} | {r.get('claim_type', '')} | {r.get('position', '')} | "
                     f"{r.get('route', '')} | {r.get('support_retrieved', '-')}/{r.get('support_cited', '-')} of "
                     f"{r.get('support_total', 0)} | {m.get('groundedness', '')} | {m.get('faithfulness', '')} | "
                     f"{'yes' if ok else 'NO'} |")
    return "\n".join(lines) + "\n"


async def main_async(a) -> None:
    base, scenarios = load(a.scenarios)
    if a.only:
        scenarios = [s for s in scenarios if s["id"] in a.only.split(",")]
    overrides = json.loads(a.overrides) if a.overrides else {}
    sem = asyncio.Semaphore(a.concurrency)
    async with httpx.AsyncClient(timeout=30) as c:
        provider = None
        try:
            provider = (await c.get(f"{a.api}/health")).json()
        except Exception:
            pass

        async def guarded(sc):
            async with sem:
                return await run_one(c, a.api, a.retriever, base, sc, overrides, a.timeout)

        results = await asyncio.gather(*[guarded(s) for s in scenarios])
    summary = summarise(results)
    meta = {"label": a.label, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "overrides": overrides,
            "provider": a.provider_note or (provider or {}).get("checks")}
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{a.label}.json").write_text(json.dumps({"meta": meta, "summary": summary, "results": results},
                                                       indent=2, default=str), encoding="utf-8")
    (outdir / f"{a.label}.md").write_text(to_markdown(a.label, meta, summary, results), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for r in results:
        ok = all(v for v in r["correct"].values() if v is not None)
        print(f"{r['id']:4s} {'ok ' if ok else 'BAD'} {r['outcome']:9s} {str(r.get('claim_type')):12s} "
              f"{str(r.get('position')):24s} {str(r.get('route')):13s} {r.get('escalation_reasons') or ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    ap.add_argument("--retriever", default=os.environ.get("RETRIEVER_URL", "http://localhost:8001"))
    ap.add_argument("--scenarios", default=str(Path(__file__).resolve().parents[2] / "eval" / "scenarios.yaml"))
    ap.add_argument("--label", default="run")
    ap.add_argument("--overrides", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--provider-note", default="")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "eval" / "results"))
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
