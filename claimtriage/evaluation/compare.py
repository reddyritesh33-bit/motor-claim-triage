"""Compare two evaluation runs (prompt variants, or before/after a change).

    python -m claimtriage.evaluation.compare eval/results/A.json eval/results/B.json [--out eval/results/A_vs_B.md]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

KEYS = ["accuracy.outcome", "accuracy.claim_type", "accuracy.position", "accuracy.route", "all_correct_rate",
        "groundedness", "context_relevance", "faithfulness", "hallucination_rate_pre_verification",
        "hallucination_rate_returned", "support_passage_recall", "support_passage_cited", "mean_llm_calls",
        "mean_tool_calls", "mean_cost_usd_per_run", "cost_per_100_runs_usd", "e2e_p50_s", "e2e_p95_s"]
LOWER_IS_BETTER = {"hallucination_rate_pre_verification", "hallucination_rate_returned", "mean_cost_usd_per_run",
                   "cost_per_100_runs_usd", "e2e_p50_s", "e2e_p95_s", "mean_llm_calls", "mean_tool_calls"}


def get(d: dict, dotted: str):
    for part in dotted.split("."):
        d = (d or {}).get(part)
    return d


def compare(a: dict, b: dict) -> str:
    la, lb = a["meta"]["label"], b["meta"]["label"]
    rows = [f"# {la} vs {lb}", "", f"- A: `{la}` overrides `{a['meta']['overrides']}` ({a['meta'].get('provider')})",
            f"- B: `{lb}` overrides `{b['meta']['overrides']}` ({b['meta'].get('provider')})", "",
            "| metric | A | B | B - A | better |", "|---|---|---|---|---|"]
    for k in KEYS:
        va, vb = get(a["summary"], k), get(b["summary"], k)
        if va is None or vb is None:
            rows.append(f"| {k} | {va} | {vb} | | |")
            continue
        delta = round(vb - va, 4)
        better = "=" if delta == 0 else ("B" if (delta < 0) == (k in LOWER_IS_BETTER) else "A")
        rows.append(f"| {k} | {va} | {vb} | {delta:+} | {better} |")
    ra = {r["id"]: r for r in a["results"]}
    diffs = []
    for r in b["results"]:
        o = ra.get(r["id"])
        if not o:
            continue
        fa = (o.get("position"), o.get("route"), o.get("support_cited"))
        fb = (r.get("position"), r.get("route"), r.get("support_cited"))
        if fa != fb:
            diffs.append(f"| {r['id']} | {fa[0]} / {fa[1]} / cited {fa[2]} | {fb[0]} / {fb[1]} / cited {fb[2]} |")
    if diffs:
        rows += ["", "## Scenarios that changed", "", "| id | A (position / route / support cited) | B |",
                 "|---|---|---|"] + diffs
    return "\n".join(rows) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    a = json.loads(Path(args.a).read_text(encoding="utf-8"))
    b = json.loads(Path(args.b).read_text(encoding="utf-8"))
    md = compare(a, b)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
