# Motor Insurance Claim Intake & Triage Service

*Mini Project 01, UE24AM342AA5 ML System Design & AgentOps (PESU)*

A multi-agent service that accepts a **First Notice of Loss (FNOL)**, validates it, works out the
likely **coverage position** from the insurer's policy wording with **clause-level citations**, and
routes the claim to **fast-track**, **surveyor** or **investigation**. When retrieval is weak, clauses
conflict, verification fails or the claim is outside the corpus, it **escalates to a human**.

> The system never approves or rejects a claim. It produces an advisory recommendation with evidence;
> a human claims handler decides.

Corpus: Universal Sompo General Insurance, *Motor Private Car Package Policy* (UIN IRDAN134RP0004V03200708):
the policy wording (including its claim procedure) and the product prospectus. See [corpus/manifest.yaml](corpus/manifest.yaml).

---

## 1. Run it (Docker, from a clean clone)

**Prerequisites:** Docker Desktop (Compose v2) with at least 6 GB RAM for Docker and about 8 GB free disk.
The first build downloads PyTorch (CPU) and the embedding model, which takes 5-15 min. Later starts take seconds.

```bash
git clone <this repo> && cd MLSD_MiniProject01_Motor_Claim_Triage
cp .env.example .env          # works as-is: the LLM is a deterministic stub until you add a key
docker compose up --build -d
docker compose ps             # wait until api/retriever/agents show "healthy" and ingest "exited (0)"
```

| URL | What |
|---|---|
| http://localhost:8000 | **Web client**: pick a sample FNOL, submit, see the recommendation, citations, escalation path, feedback |
| http://localhost:8000/docs | OpenAPI (FastAPI) |
| http://localhost:3000 | **Grafana** dashboard "Claim Triage - Service & Agents" (anonymous viewer; admin login is in `.env`) |
| http://localhost:16686 | **Jaeger** traces (service `api`, `triage-agent`, `coverage-agent`, `retriever`, `llm-gateway`) |
| http://localhost:9090 | Prometheus |

Try the difficult case from the command line:

```bash
docker compose exec api python -m claimtriage.tools.submit S03 --trace
```

Or with plain HTTP:

```bash
curl -s localhost:8000/samples.json            # 26 ready-made FNOLs (the evaluation set)
curl -s -XPOST localhost:8000/v1/claims -H 'content-type: application/json' -d @my_fnol.json   # -> 202 {run_id}
curl -s localhost:8000/v1/runs/<run_id>        # poll until status is completed|escalated
curl -s localhost:8000/v1/runs/<run_id>/trace  # full reconstruction of the run
```

### Using Groq (the real LLM)

The stub lets everything run with no key. To use a real model on the Groq free tier:

1. Create a key at console.groq.com and **set a spending limit** on the account.
2. In `.env` set `LLM_PROVIDER=openai_compatible`, `LLM_API_KEY=<key>`, `LLM_BASE_URL=` Groq's
   OpenAI-compatible base URL (from Groq's "OpenAI compatibility" docs page), and `LLM_MODEL=` a model id from the
   live list (`curl -H "Authorization: Bearer <key>" <base url>/models`). Models change: the one we used is
   `qwen/qwen3.8-27b` (INC-010).
3. For the free tier also set the pacing values (about 8,000 tokens/minute): `LLM_RATE_LIMIT_WAITS=8`,
   `LLM_TIMEOUT_S=45`, `LLM_CALL_TIMEOUT_S=600`, `AGENT_WALL_CLOCK_S=1800`, `AGENT_STATE_CHAR_BUDGET=7000`,
   `WORKER_CONCURRENCY=1`.
4. `docker compose up -d`. The provider is pure configuration.

On the free tier one claim takes several minutes, because the gateway waits out the provider's rate limit instead of
failing (INC-007). Use the stub for demos and load tests, and Groq for the quality comparison
(`scripts/dev/groq_compare.sh`). Keeping the key in a separate `.env.groq` file (gitignored) lets you switch:
`ENV_FILE=.env.groq API_PORT=8100 bash scripts/dev/run_local.sh start`.

### Scale the agent tier

```bash
docker compose up -d --scale coverage-agent=3      # Prometheus discovers replicas via DNS
```

## 2. Evaluate, load test, demo

```bash
# fixed evaluation set (26 scenarios incl. the section 4.1 case), through the real API:
docker compose --profile tools run --rm eval --label baseline
docker compose --profile tools run --rm eval --label prompt_v1 --overrides '{"coverage_prompt":"v1"}'
docker compose --profile tools run --rm --entrypoint python eval -m claimtriage.evaluation.compare \
    eval/results/baseline.json eval/results/prompt_v1.json

# load test against the stub (set LLM_PROVIDER=stub), stepped user counts:
docker compose --profile loadtest run --rm locust bash loadtest/run_steps.sh scale1 "10 20 40 60 80"
```

* All ten demonstration items, with commands: [docs/DEMO.md](docs/DEMO.md)
* Reference answer for the difficult case: [docs/EVALUATION_1.md](docs/EVALUATION_1.md)

## 3. How it works (short)

`api` validates the FNOL against the published [data contract](contracts/README.md) (schema, range,
category, cross-field, prompt-injection guardrail) and quarantines anything that fails, without starting a
run. Valid FNOLs are queued in Redis and the API returns `202 {run_id}`.

1. **Triage agent** (LangGraph): masks toxic text, derives facts with code (intimation delay vs the
   15-day / 24-hour limits, proximity to inception, licence validity, estimate/IDV), classifies the claim
   and extracts entities (LLM), decides scope, and **hands off** to the coverage agent.
2. **Coverage agent** (LangGraph): a bounded loop in which the model *chooses* tools: `search_policy`,
   `get_clause`, `find_modifiers`, `get_fnol_field`. It finds the cover clause, asks which provisions modify
   it, then searches again with a query derived from what it read (e.g. the content of "Limitations as to
   Use"). Its draft is **verified**: every citation must resolve to retrieved text, quotes must be verbatim,
   referenced defined terms must be resolved, and fact-triggered restrictions must be in evidence. A
   second model checks support. Failures go back to the agent with a changed strategy; repeated failure
   escalates. If the LLM is down, a **degraded** retrieval-only path escalates with the clauses attached.
3. **Triage agent (finalize)**: routes by auditable rules (fast-track / surveyor / investigation, with
   reasons), applies output guardrails (no approve/reject/pay statements, toxicity), and stores the result
   and a full event log.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Report: [docs/REPORT.md](docs/REPORT.md).

## 4. Requirement map

| Req | Where |
|---|---|
| F1, D1 | `claimtriage/common/contracts.py`, [contracts/](contracts/), `POST /v1/claims` |
| F2 | `api/main.py` (`_reject` -> quarantine), `GET /v1/quarantine` |
| F3 | `agents/triage.py` (classify + derive facts), prompt `prompts/triage_extract/v1.yaml` |
| F4, L1 | `retriever/` (structure-aware chunker, hybrid FAISS+BM25, `find_modifiers`) |
| F5, A3 | `agents/coverage.py`, `agents/verifier.py` (checks V1-V8) |
| F6 | `agents/router.py` |
| F7 | escalation paths in `coverage.py` / `triage.py` (weak retrieval, conflicts, verification, out-of-corpus, degraded) |
| F8, D5, A6 | `/health` + `/metrics` on every service, structlog JSON with `correlation_id`, OpenTelemetry to Jaeger, `observability/` (Prometheus + Grafana), `GET /v1/runs/{id}/trace` |
| A1, A2 | LangGraph graphs in `agents/triage.py`, `agents/coverage.py`. Separate queues and containers. Failure handling in `agents/worker.py` |
| A4 | 202 + polling, Redis reliable queue (`common/store.py`) |
| A5 | `common/resilience.py`, `gateway/service.py` (timeouts, backoff, breaker, `/admin/fault`), degraded path |
| A7, D6 | `docker-compose.yml` (`--scale coverage-agent=3`), `loadtest/`, [docs/SLO.md](docs/SLO.md), [docs/LOADTEST.md](docs/LOADTEST.md) |
| A8 | `api/static/index.html` |
| L2 | `prompts/<id>/<version>.yaml`, version + sha recorded per call, `evaluation/compare.py` |
| L3 | `eval/scenarios.yaml` (26), `evaluation/run_eval.py`, `evaluation/metrics.py`, `eval/results/` |
| L4 | `common/guardrails.py` (injection, toxicity in/out, determination filter) |
| L5 | `gateway/service.py` (cost at call time), per-step cost in run record, cost per 100 runs in eval summary |
| L6 | `POST /v1/runs/{id}/feedback`, `GET /v1/feedback/summary`, live sampling (`LIVE_EVAL_SAMPLE_RATE`), `GET /v1/quality/summary`, Grafana "quality over time" |
| D2 | `retriever/ingest.py` (idempotent CLI, freshness metadata + metrics) |
| D3 | `docker-compose.yml`: 7 core services + observability + one-shot ingest, healthchecks, `depends_on` conditions |
| D4 | `.env.example`. No credentials, provider endpoints or model ids in code |
| D7 | run cache (API), retrieval cache (retriever, Redis), query-embedding LRU, embedding cache (disk). Per-client rate limit |
| D8 | [docs/RUNBOOK.md](docs/RUNBOOK.md), [docs/INCIDENT_LOG.md](docs/INCIDENT_LOG.md) |

## 5. Repository layout

```
claimtriage/
  api/          FastAPI edge: contract, quarantine, guardrails, rate limit, cache, runs, feedback, web client
  agents/       triage + coverage agents (LangGraph), verifier, router, worker, prompts loader, metrics
  retriever/    chunker, index (FAISS+BM25), ingestion CLI, retriever service
  gateway/      LLM gateway, providers (OpenAI-compatible / stub), stub "model"
  common/       config, contracts, guardrails, resilience, Redis store, logging, tracing
  evaluation/   scenarios loader, metrics, batch runner, comparison
  tools/        submit (demo CLI), fault toggle, contract export
prompts/        versioned prompt artifacts
eval/           scenarios.yaml (fixed evaluation set) and results/
loadtest/       locustfile, stepped runner, summariser, results/
observability/  prometheus.yml, grafana provisioning + dashboard
docker/         Dockerfile (light) and Dockerfile.retriever (torch + model)
corpus/         policy PDFs + manifest
docs/           ARCHITECTURE, REPORT, EVALUATION_1, SLO, LOADTEST, RUNBOOK, INCIDENT_LOG, DEMO
tests/          unit tests (python -m pytest -q tests)
scripts/dev/    run_local.sh: run the stack without Docker (development only)
```

## 6. Development without Docker (optional)

Linux/WSL with `redis-server` installed and Python 3.11:
`pip install -r requirements/dev.txt`, `cp .env.example .env`, then `bash scripts/dev/run_local.sh start`.
This starts every service as a local process on the same ports. Unit tests: `python -m pytest -q tests`.
