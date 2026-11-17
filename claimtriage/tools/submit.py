"""Submit evaluation scenarios to the API and print the outcome (demo helper).

    python -m claimtriage.tools.submit S03                 # the difficult case
    python -m claimtriage.tools.submit S01 --fault bad_citation   # self-verification demo
    python -m claimtriage.tools.submit S15,S16             # rejected + quarantined
    python -m claimtriage.tools.submit S03 --trace         # print the reconstructed run timeline
"""
from __future__ import annotations

import argparse
import json
import os
import time

import httpx

from ..evaluation.scenarios import build, load


def show(run: dict, title: str) -> None:
    res = run.get("result") or {}
    v = res.get("verification") or {}
    print(f"\n=== {run['run_id']}  {title}")
    print(f" status={run['status']}  e2e={run.get('e2e_seconds')}s  cost=${(run.get('cost') or {}).get('total_usd')}"
          f"  prompts={run.get('prompt_versions')}  cache_hit={run.get('cache_hit', False)}")
    print(f" claim_type={res.get('claim_type')}  position={res.get('coverage_position')}  route={res.get('route')}")
    for r in res.get("route_reasons", []):
        print(f"   route reason: {r}")
    esc = res.get("escalation") or {}
    if esc.get("escalated"):
        print(f" ESCALATED -> {esc.get('path')}")
        for r in esc.get("reasons", []):
            print(f"   reason: {r}")
    print(f" verification: passed={v.get('passed')} attempts={v.get('attempts')} "
          f"failures={[f.get('check') for f in v.get('failures', [])]}")
    for c in res.get("citations", []):
        print(f"   [{c['clause_id']}] {c.get('section', '')[:45]} p{c.get('page')}: {c['quote'][:120]!r}")
    print(f" rationale: {res.get('rationale', '')[:400]}")
    if res.get("flags"):
        print(f" flags: {res['flags']}")


def timeline(trace: dict) -> None:
    print("\n--- run reconstruction ---")
    for e in trace["events"]:
        extra = ""
        if e["kind"] == "llm":
            extra = (f"{e.get('prompt_id')}@{e.get('prompt_version')} {e.get('latency_ms')}ms "
                     f"tok={e.get('prompt_tokens')}+{e.get('completion_tokens')} ${e.get('cost_usd', 0):.5f} "
                     f"-> {json.dumps(e.get('output'))[:140]}")
        elif e["kind"] == "tool":
            extra = (f"query={e.get('query')!r} " if e.get("query") else f"{e.get('clause_id') or e.get('path')} ") + \
                f"passages={[p['clause_id'] for p in e.get('passages', [])]}"
        elif e["kind"] == "verification":
            extra = f"attempt={e.get('attempt')} passed={e.get('passed')} failures={[f['check'] for f in e['failures']]}"
        else:
            extra = json.dumps({k: v for k, v in e.items() if k not in ("kind", "name", "agent", "ts", "trace_id",
                                                                       "t_offset_s", "step")})[:160]
        print(f" +{e.get('t_offset_s', 0):6.2f}s {e.get('agent', ''):8s} {e['kind']:12s} {e['name']:22s} {extra}")
    print(" summary:", json.dumps(trace["summary"], default=str)[:600])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", help="comma-separated scenario ids, e.g. S03,S01")
    ap.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    ap.add_argument("--fault", default="", help="X-Fault header (bad_citation) - needs FAULT_INJECTION_ENABLED")
    ap.add_argument("--overrides", default="", help='X-Eval-Overrides JSON, e.g. {"coverage_prompt":"v1"}')
    ap.add_argument("--use-cache", action="store_true", help="allow the whole-run cache")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--timeout", type=float, default=240)
    a = ap.parse_args()
    base, scenarios = load()
    by = {s["id"]: s for s in scenarios}
    hdr = {"x-client-id": f"demo-{int(time.time())}"}
    ov = json.loads(a.overrides) if a.overrides else {}
    if not a.use_cache:
        ov["no_cache"] = True
    if ov:
        hdr["x-eval-overrides"] = json.dumps(ov)
    if a.fault:
        hdr["x-fault"] = a.fault
    c = httpx.Client(timeout=30)
    runs = {}
    for sid in a.ids.split(","):
        r = c.post(f"{a.api}/v1/claims", json=build(base, by[sid]), headers=hdr)
        body = r.json()
        print(f"{sid} -> HTTP {r.status_code}: {json.dumps(body)[:400]}")
        if r.status_code in (200, 202):
            runs[sid] = body["run_id"]
    t0 = time.time()
    while runs and time.time() - t0 < a.timeout:
        for sid, rid in list(runs.items()):
            run = c.get(f"{a.api}/v1/runs/{rid}").json()
            if run["status"] in ("completed", "escalated", "failed"):
                show(run, f"{sid}: {by[sid]['title']}")
                if a.trace:
                    timeline(c.get(f"{a.api}/v1/runs/{rid}/trace").json())
                del runs[sid]
        time.sleep(0.5)
    for sid in runs:
        print(f"{sid}: still running after {a.timeout}s")


if __name__ == "__main__":
    main()
