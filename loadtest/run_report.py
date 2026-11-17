"""Summarise the most recent runs straight from the run store: latency, cost,
cache-hit share, escalation share and the largest cost driver (D7, L5).

    python loadtest/run_report.py --since-min 5 [--label run_cache_on]
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claimtriage.common import store  # noqa: E402


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


async def main(a):
    since = time.time() - a.since_min * 60
    ids = await store.recent_runs(5000)
    runs = [r for r in [await store.get_run(i) for i in ids] if r and r.get("submitted_at", 0) >= since
            and r.get("status") in ("completed", "escalated")]
    e2e = [r.get("e2e_seconds") or 0.0 for r in runs]
    costs = [(r.get("cost") or {}).get("total_usd", 0.0) for r in runs]
    drivers = {}
    for r in runs:
        for k, v in ((r.get("cost") or {}).get("by_step") or {}).items():
            drivers[k] = drivers.get(k, 0.0) + v.get("cost_usd", 0.0)
    out = {
        "label": a.label, "runs": len(runs),
        "cache_hit_share": round(sum(1 for r in runs if r.get("cache_hit")) / len(runs), 3) if runs else None,
        "escalated_share": round(sum(1 for r in runs if r["status"] == "escalated") / len(runs), 3) if runs else None,
        "e2e_p50_s": pct(e2e, 0.5), "e2e_p95_s": pct(e2e, 0.95),
        "mean_cost_usd": round(sum(costs) / len(costs), 6) if costs else None,
        "cost_per_100_runs_usd": round(100 * sum(costs) / len(costs), 4) if costs else None,
        "cost_share_by_step": {k: round(v / sum(drivers.values()), 3) for k, v in
                               sorted(drivers.items(), key=lambda x: -x[1])} if drivers else {},
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-min", type=float, default=5)
    ap.add_argument("--label", default="")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    asyncio.run(main(ap.parse_args()))
