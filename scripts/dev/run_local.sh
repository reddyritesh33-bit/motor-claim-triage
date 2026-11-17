#!/usr/bin/env bash
# Run the whole stack WITHOUT Docker (development only; the deliverable is
# `docker compose up`). Starts redis, retriever, llm-gateway, triage agent,
# coverage agent (N replicas) and api as background processes.
#   scripts/dev/run_local.sh start [coverage_replicas]   |   scripts/dev/run_local.sh stop
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-$HOME/claimtriage-venv/bin/python}"
LOG="$ROOT/data/logs"; mkdir -p "$LOG"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
[ -f "$ENV_FILE" ] || ENV_FILE="$ROOT/.env.example"

stop() {
  pkill -f "claimtriage\.(api|retriever|gateway|agents)" 2>/dev/null || true
  pkill -f "uvicorn claimtriage" 2>/dev/null || true
  redis-cli -p 6379 shutdown nosave 2>/dev/null || true
  echo "stopped"
}

start() {
  local replicas="${1:-1}"
  set -a; source "$ENV_FILE"; set +a
  # local overrides: everything on localhost, no Jaeger unless running
  export REDIS_URL=redis://localhost:6379/0 RETRIEVER_URL=http://localhost:8001 LLM_GATEWAY_URL=http://localhost:8002
  export OTEL_ENABLED="${OTEL_ENABLED_LOCAL:-false}" INDEX_DIR="$ROOT/data/index" CORPUS_DIR="$ROOT/corpus"
  redis-server --port 6379 --daemonize yes --appendonly no --save "" --dir /tmp >/dev/null
  [ -f data/index/meta.json ] || "$PY" -m claimtriage.retriever.ingest
  nohup "$PY" -m uvicorn claimtriage.retriever.service:app --port 8001 >"$LOG/retriever.log" 2>&1 &
  nohup "$PY" -m uvicorn claimtriage.gateway.service:app --port 8002 >"$LOG/gateway.log" 2>&1 &
  AGENT_ROLE=triage METRICS_PORT=9101 nohup "$PY" -m claimtriage.agents.worker >"$LOG/triage.log" 2>&1 &
  for i in $(seq 1 "$replicas"); do
    AGENT_ROLE=coverage METRICS_PORT=$((9110 + i)) nohup "$PY" -m claimtriage.agents.worker >"$LOG/coverage-$i.log" 2>&1 &
  done
  nohup "$PY" -m uvicorn claimtriage.api.main:app --port "${API_PORT:-8000}" >"$LOG/api.log" 2>&1 &
  for _ in $(seq 1 90); do
    if curl -sf localhost:8001/health >/dev/null && curl -sf "localhost:${API_PORT:-8000}/health" >/dev/null; then
      echo "stack up: api http://localhost:${API_PORT:-8000}  (coverage replicas: $replicas)"; return 0; fi
    sleep 1
  done
  echo "stack did not become healthy; see $LOG" >&2; return 1
}

case "${1:-start}" in
  start) start "${2:-1}" ;;
  stop) stop ;;
  restart) stop; sleep 1; start "${2:-1}" ;;
esac
