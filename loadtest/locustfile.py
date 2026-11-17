"""Locust load test (D6). Measures a COMPLETE run: submit, then poll until the run
is terminal. The custom request type "RUN" / name "run_complete" carries the
end-to-end latency, which is what the SLO is about (not the 202 alone).

Run against the stub LLM (LLM_PROVIDER=stub) so the test measures this system.

  locust -f loadtest/locustfile.py --headless -u 20 -r 5 -t 3m --host http://localhost:8000 --csv loadtest/results/u20

Env:
  PACING_S     seconds between a user's run starts (constant pacing); users / PACING_S = offered rate
  CACHE_MODE   unique (default; every FNOL differs, run cache never hits) | repeat (identical claims)
"""
from __future__ import annotations

import os
import random
import sys
import time
import uuid
from pathlib import Path

from locust import HttpUser, constant_pacing, events, task

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claimtriage.evaluation.scenarios import build, load  # noqa: E402

BASE, SCENARIOS = load()
ROUTABLE = [s for s in SCENARIOS if s["expect"]["outcome"] != "rejected"]
PACING_S = float(os.environ.get("PACING_S", "20"))
CACHE_MODE = os.environ.get("CACHE_MODE", "unique")
POLL_S = 0.5
RUN_TIMEOUT_S = 180


class ClaimUser(HttpUser):
    wait_time = constant_pacing(PACING_S)

    def on_start(self):
        self.client_id = "locust-" + uuid.uuid4().hex[:8]  # per-user id: the rate limit is per client

    @task
    def submit_and_wait(self):
        sc = random.choice(ROUTABLE) if CACHE_MODE == "unique" else ROUTABLE[0]
        fnol = build(BASE, sc)
        if CACHE_MODE == "unique":  # make content unique so the whole-run cache cannot hit
            fnol["incident"]["estimated_repair_cost_inr"] = (fnol["incident"].get("estimated_repair_cost_inr") or 0) \
                + random.randint(1, 999) if fnol["incident"].get("estimated_repair_cost_inr") is not None else None
            fnol["incident"]["description"] += f" Ref {uuid.uuid4().hex[:6]}."
        t0 = time.perf_counter()
        with self.client.post("/v1/claims", json=fnol, headers={"x-client-id": self.client_id},
                              name="POST /v1/claims", catch_response=True) as r:
            if r.status_code not in (200, 202):
                r.failure(f"submit {r.status_code}")
                return
            run_id = r.json()["run_id"]
        status = "queued"
        while time.perf_counter() - t0 < RUN_TIMEOUT_S:
            with self.client.get(f"/v1/runs/{run_id}", name="GET /v1/runs/{id}", catch_response=True) as pr:
                if pr.status_code != 200:
                    pr.failure(f"poll {pr.status_code}")
                    break
                status = pr.json()["status"]
            if status in ("completed", "escalated", "failed"):
                break
            time.sleep(POLL_S)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        ok = status in ("completed", "escalated")
        events.request.fire(request_type="RUN", name="run_complete", response_time=elapsed_ms, response_length=0,
                            exception=None if ok else RuntimeError(f"run ended {status}"), context={})
