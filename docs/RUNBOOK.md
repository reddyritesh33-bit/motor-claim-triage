# Runbook

These are the two most likely failure modes in production, followed by quick references
for the others.

---

## 1. LLM provider unavailable or rate-limited: circuit breaker open, runs escalating in degraded mode

**Symptom**
- Grafana "Circuit breaker" panel at **2 (open)**. `llm_requests_total{outcome="circuit_open"|"unavailable"}` rising.
- Escalations by reason dominated by `LLM unavailable` / `degraded mode`. Run status mostly `escalated` with
  path "Manual triage queue (model unavailable; retrieved clauses attached)".
- Run latency *drops* (degraded runs finish in about 1.5 s). Do not mistake this for recovery.

**Confirm**
```bash
curl -s localhost:8000/health                                   # retriever/gateway checks
docker compose exec llm-gateway python -c "import urllib.request,json;print(urllib.request.urlopen('http://localhost:8002/admin/breaker').read().decode())"
docker compose logs llm-gateway --since 10m | grep -E "provider unavailable|circuit breaker state change|http 429|http 5"
```
- `provider rate limit, waiting` lines are normal pacing: the gateway waits out each 429 without tripping the
  breaker (INC-007). Only when the waits run out (`LLM_RATE_LIMIT_WAITS`) does a run degrade. `transport`,
  `http 5xx` or `SSLError` means a provider or network outage. `413 Payload Too Large` means prompts outgrew the
  per-request limit: lower `AGENT_STATE_CHAR_BUDGET` (INC-008). `fault injection` means someone left the demo
  switch on. Check `curl $LLM_BASE_URL/models` if the model id itself is rejected (INC-010).

**Recover**
1. If fault injection is on: `curl -XPOST .../admin/fault -d '{"provider_down":false}'` (from inside the network).
2. Rate limit (429): lower offered load (rate limit `RATE_LIMIT_PER_MIN`, scale coverage agents *down*, since more
   replicas mean more parallel calls), or switch to a smaller model with higher limits in `.env` (`LLM_MODEL`),
   then `docker compose up -d llm-gateway`. Only this container restarts.
3. Outage: nothing to do in the system. It is already degrading safely. Optionally switch provider by config.
   The breaker half-opens after `BREAKER_RESET_S` (30 s) and closes on the first success.
4. Re-run escalated degraded runs if the business wants a machine recommendation: resubmit the FNOL with a new
   `claim_reference` (the run cache does not store degraded results).

---

## 2. Agent backlog: queue growing, p95 run latency breaching the 30 s SLO, wall-clock escalations

**Symptom**
- "Queue depth" panel: `queue_depth{queue="coverage",state="waiting"}` climbing. Run p95 above 30 s.
- Escalations with reason `loop bound reached: wall_clock`. Runs sit in stage `coverage` for a long time.

**Confirm**
```bash
docker compose exec redis redis-cli LLEN q:coverage            # waiting
docker compose exec redis redis-cli LLEN q:coverage:processing # leased (in flight)
docker compose ps coverage-agent                                # replicas healthy?
```
- If waiting grows while processing equals replicas x `WORKER_CONCURRENCY`, the agent tier is saturated.
- If processing is high but agent CPU is low, the bottleneck is downstream. Check the retriever CPU panel
  (embedding queries are CPU-bound) and gateway latency.
- If processing is 0 and waiting grows, the agents are down (see healthchecks / logs).

**Recover**
1. Agents saturated: `docker compose up -d --scale coverage-agent=3` (Prometheus discovers new replicas via DNS).
2. Retriever saturated: raise its CPU limit in `docker-compose.yml`, or keep the retrieval cache warm
   (`RETRIEVAL_CACHE_TTL_S`). See `docs/LOADTEST.md` for where the knee is.
3. Agents crashed mid-run: no action needed for in-flight work. Leases expire and the reaper requeues, or
   escalates after `JOB_MAX_ATTEMPTS`. Inspect the dead letters: `redis-cli LRANGE q:dead 0 10`.

---

## Quick references

| Symptom | Likely cause | Action |
|---|---|---|
| `api /health` 503 | Redis down | `docker compose up -d redis`. Runs persist (AOF) |
| Many 422 `schema_validation` from one client | client on an old contract | `GET /v1/quarantine`, share `GET /v1/contract/fnol` |
| 429 from `/v1/claims` | per-client rate limit (`RATE_LIMIT_PER_MIN`) | intended as a cost control. Raise per client only after review |
| Retriever fails to start | index volume missing/corrupt | `docker compose run --rm ingest python -m claimtriage.retriever.ingest --force` |
| Policy PDF updated upstream | new version | replace the file + `sha256` in `corpus/manifest.yaml`, then `docker compose run --rm ingest`. Only changed chunks re-embed |
| Reconstruct a disputed run | - | `GET /v1/runs/{id}/trace` (or `python -m claimtriage.tools.submit ... --trace`), then Jaeger by trace id |
