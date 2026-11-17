# Service level objective (declared before any load test)

**Declared:** 2026-10-27, before the first Locust run. Results that came later are in
[LOADTEST.md](LOADTEST.md). The target was not changed after seeing them.

## SLO

| SLI | Objective | Measured how |
|---|---|---|
| **Complete-run latency, p95** | **≤ 30 s** from `POST /v1/claims` to the run reaching `completed`/`escalated` | Locust custom event `run_complete`: submit, then poll until terminal (not the 202 alone) |
| Sustained load the objective must hold at | **1 run/s** (3,600 runs/hour) | Locust constant-pacing users |
| Run completion | ≥ 99% of accepted runs reach a terminal state within the 120 s agent wall-clock bound | `run_e2e_latency_seconds` count vs `fnol_submissions_total{outcome="accepted"}` |
| API availability | ≥ 99.5% non-5xx on `/v1/claims` and `/v1/runs/*` | `http_request_duration_seconds_count` by status |

The objective applies to the **system** with the LLM replaced by the stub (800-1500 ms per
call, roughly the median latency of a hosted 70B model on a short JSON completion), so it
measures our queueing, agents, retrieval and storage rather than a provider's rate limits.
The test gives no SLO for the free-tier provider itself. Its rate limits (tokens per minute)
cap throughput far below 1 run/s, which is why D6 requires a stub.

## Justification (against section 1 of the brief)

* **Latency-sensitive intake.** An FNOL arrives while the customer is on a call-centre line
  or in the app, waiting to be told what happens next: cashless garage, surveyor visit or
  investigation. A routed recommendation inside about half a minute keeps that inside one
  conversation. Beyond that the call-centre agent has to promise a call-back.
* **The policy's own claim procedure sets the downstream clock.** Section IX of the wording
  requires surveyor appointment within 24 h of intimation. Triage is a small part of that
  budget, so 30 s at p95 is ambitious for user experience but has a large margin against
  the regulatory TAT.
* **Agents are slow by construction.** A run makes about 5 model calls in sequence (triage
  classification, 3-4 coverage steps, verifier). At about 1.2 s each, the floor is roughly
  6-8 s before any queueing. 30 s leaves room for one verification retry plus queueing at load.
* **Volume.** These are planning estimates, not insurer data. A large Indian motor insurer
  handling on the order of 10^6 own-damage claims a year sees about 2,700 FNOL/day on
  average. Monsoon-flood peaks can be 10x that, about 0.3 FNOL/s. 1 run/s gives about 3x
  headroom over that peak.

## Error budget

At 99% run completion, 1% of accepted runs (36 per hour at the target load) may exceed the
wall-clock bound. Those runs still end as **escalated to a human**, never as a silent drop
(see the worker reaper and dead-letter path).
