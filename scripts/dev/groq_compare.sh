#!/usr/bin/env bash
# L2: compare coverage prompt v1 vs v2 on a real model (Groq free tier).
# Needs the local stack started with the Groq settings:
#   ENV_FILE=.env.groq API_PORT=8100 bash scripts/dev/run_local.sh restart 1
#   bash scripts/dev/groq_compare.sh [scenario ids]
# Free-tier pacing (~8,000 tokens/minute) makes each claim take several minutes.
set -uo pipefail
cd "$(dirname "$0")/../.."
PY="${PYTHON:-$HOME/claimtriage-venv/bin/python}"
API="${API_URL:-http://localhost:8100}"
IDS="${1:-S01,S03,S04,S07,S18,S20,S21,S23}"
MODEL="$(grep '^LLM_MODEL=' .env.groq | cut -d= -f2)"
for v in v2 v1; do
  echo "=== coverage prompt $v  start $(date +%T)"
  "$PY" -m claimtriage.evaluation.run_eval --api "$API" --label "groq_prompt_$v" \
    --overrides "{\"coverage_prompt\":\"$v\"}" --only "$IDS" --concurrency 1 --timeout 2400 \
    --provider-note "Groq free tier, $MODEL, coverage prompt $v" | tail -10
done
"$PY" -m claimtriage.evaluation.compare eval/results/groq_prompt_v1.json eval/results/groq_prompt_v2.json \
  --out eval/results/groq_prompt_v1_vs_v2.md
bash scripts/dev/log_grep.sh
echo "=== done $(date +%T)"
