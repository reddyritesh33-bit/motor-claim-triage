# Load test results (D6, A7)

**Target, declared before testing** ([SLO.md](SLO.md)): complete-run p95 <= 30 s at a sustained
1 run/s, with the LLM replaced by the stub (800-1500 ms per call).

**Method.** `loadtest/locustfile.py`. Each Locust user submits an FNOL drawn at random from the 23
routable evaluation scenarios, made unique so the run cache cannot hit, then polls every 0.5 s until
the run is terminal. The custom event `run_complete` records **submission to final result**. Users
pace at one run per 20 s, so offered load = users / 20 runs/s. Each step runs 2 minutes, and the
queues are drained between steps. `WORKER_CONCURRENCY=4` per agent replica.

**Environment.** Final numbers come from **Docker Compose** on a 12-CPU / 16 GB laptop (Docker Desktop,
WSL2 backend), with Locust in its own container on the compose network:
`docker compose --profile loadtest run --rm locust bash loadtest/run_steps.sh <label> "20 40 60 80"`.
An earlier pass with the stack as local processes (`scripts/dev/run_local.sh`) gave the same picture
and is kept below for comparison.

## Docker Compose results (final)

**1 coverage agent** (`docker compose up -d --scale coverage-agent=1`):

| users | offered runs/s | achieved runs/s | p50 (s) | p95 (s) | max (s) | failed | SLO (p95 <= 30 s) |
|---|---|---|---|---|---|---|---|
| 20 | 1.0 | 0.85 | 20.0 | 25.0 | 30.5 | 0 | met (already saturated) |
| 40 | 2.0 | 0.79 | 46.0 | 51.0 | 52.7 | 0 | **failed** |
| 60 | 3.0 | 0.88 | 50.0 | 73.0 | 76.1 | 0 | failed |

**3 coverage agents** (`docker compose up -d --scale coverage-agent=3`):

| users | offered runs/s | achieved runs/s | p50 (s) | p95 (s) | max (s) | failed | SLO (p95 <= 30 s) |
|---|---|---|---|---|---|---|---|
| 20 | 1.0 | 0.96 | 8.7 | 13.0 | 14.3 | 0 | **met** |
| 40 | 2.0 | 1.92 | 12.0 | 18.0 | 29.0 | 0 | **met** |
| 60 | 3.0 | 2.61 | 18.0 | 30.0 | 30.1 | 0 | met (at the limit) |
| 80 | 4.0 | 2.56 | 27.0 | 40.0 | 40.6 | 0 | **failed** |

* **Target met with 3 agents:** p95 **13 s at 1 run/s**, against a 30 s target.
* **Failure point:** with 1 agent, above about 0.85 runs/s. With 3 agents, between 3 and 4 runs/s
  offered (throughput saturates at about 2.6 runs/s).
* **Effect of `--scale coverage-agent=3`:** about **3x the throughput** (0.85 to 2.6 runs/s) and half
  the p95 at 1 run/s (25 s to 13 s). Zero failed runs in any step.

## Local-process results (earlier pass, for comparison)

### 1 coverage-agent replica (`WORKER_CONCURRENCY=4`)

| users | offered runs/s | achieved runs/s | p50 (s) | p95 (s) | max (s) | failed | SLO (p95 <= 30 s) |
|---|---|---|---|---|---|---|---|
| 20 | 1.0 | 0.82 | 19.0 | 26.0 | 30.5 | 0 | met (barely, already saturated) |
| 40 | 2.0 | 0.90 | 39.0 | 49.0 | 51.6 | 0 | **failed** |
| 60 | 3.0 | 0.83 | 50.0 | 74.0 | 77.9 | 0 | failed |
| 80 | 4.0 | 0.96 | 43.0 | 87.0 | 95.1 | 0 | failed |

Throughput plateaus at **about 0.9 runs/s**. Beyond that, extra load only adds queueing time.

## 3 coverage-agent replicas (`--scale coverage-agent=3` equivalent)

| users | offered runs/s | achieved runs/s | p50 (s) | p95 (s) | max (s) | failed | SLO |
|---|---|---|---|---|---|---|---|
| 20 | 1.0 | 0.96 | 8.2 | 13.0 | 14.7 | 0 | **met** |
| 40 | 2.0 | 1.85 | 12.0 | 18.0 | 29.8 | 0 | **met** |
| 60 | 3.0 | 2.49 | 18.0 | 32.0 | 32.6 | 0 | **failed** (just over) |
| 80 | 4.0 | 2.48 | 27.0 | 39.0 | 38.8 | 0 | failed |

The 20- and 40-user rows come from a re-run (`results/local_scale3_rerun`). The first 3-replica pass
at those loads (`results/local_scale3/summary.md`) measured the same as a single replica, which is
inconsistent with the 60/80-user rows and with per-replica metrics showing all three replicas busy.
We discarded it and re-ran on the same stack. The likely cause is leftover in-flight work from the
1-replica run at the restart, but we did not confirm it. The raw files are kept.

## Result against the target

* **Target met with 3 replicas:** p95 **13 s at 1 run/s** (target 30 s), and 18 s at 1.85 runs/s.
* **Not met with 1 replica:** capacity is about 0.9 runs/s, below the 1 run/s target.
* **Failure point (3 replicas):** between **2 and 3 runs/s offered**. At 3 runs/s the p95 reaches
  32 s and throughput saturates at about 2.5 runs/s.
* **Effect of `--scale`:** about **2.8x the throughput** (0.9 to 2.5 runs/s) for 3x the replicas.
  At 1 run/s, p95 drops from 26 s to 13 s.

## Caching (D7)

Same stack (3 replicas), 20 users, 2 minutes:

| mode | run-cache hit share | achieved runs/s | p50 (s) | p95 (s) | cost per 100 runs (USD) |
|---|---|---|---|---|---|
| unique claims (cache cannot hit) | 0% | 0.96 | 8.2 | 13.0 | 0.58 (all load-test runs) |
| repeated identical claim (`CACHE_MODE=repeat`) | 86% | 1.17 | **0.1** | 11.0 | **0.078** |

**Why cache whole runs:** call centres and apps resubmit the same loss (retries, a customer calling
twice). The cache key is the claim *content* without the reference, channel, reporting time or
contact details. Degraded runs are never cached. p95 barely moves because the misses (the first
submission of each burst) still run the full agent. There is no in-flight de-duplication, so
concurrent duplicates all miss; that is the next improvement. The retrieval cache (Redis, keyed by
query + mode + index version) served 100% of agent searches during the load test after warm-up,
at about 0.5 ms per hit. Its effect on end-to-end latency is negligible because retrieval is not
the bottleneck. Its value is retriever CPU.

## The bottleneck: agent concurrency slots, not CPU

During the saturated steps:

* host CPU was about **97% idle**. The retriever answered searches in about 2 ms (cache hit) and
  /modifiers in about 3 ms;
* every coverage replica had **4 of 4 jobs in flight** (`agent_inflight_jobs`), and the coverage
  stage averaged **6.5-8.6 s**: about 6 sequential model calls at the stub's 0.8-1.5 s delay;
* the triage agent stayed at about 0.6 s per stage and was never saturated.

Capacity is therefore about `replicas x WORKER_CONCURRENCY / coverage_stage_seconds`
(3 x 4 / 6.6 s ≈ 1.8 runs/s for runs that reach coverage, plus out-of-scope runs that skip it,
about 2.5 runs/s observed). The work is I/O-bound waiting on the model, so **raising
`WORKER_CONCURRENCY`** is a cheaper lever than more replicas. The real ceiling in production is the
provider's rate limit, not this system (see [SLO.md](SLO.md)). The other lever is fewer sequential
model calls per run (prompt v2 already finalises in 3-4 steps).
