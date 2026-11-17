#!/usr/bin/env bash
# Stepped load test: for each user count, run Locust headless for DURATION and
# record RPS / p50 / p95 of complete runs. Offered rate = users / PACING_S.
#   loadtest/run_steps.sh <label> "10 20 40 60" [duration=2m] [host=http://localhost:8000]
# Inside compose:  docker compose --profile loadtest run --rm locust bash loadtest/run_steps.sh scale1 "10 20 40 60"
set -euo pipefail
LABEL="${1:?label}"; STEPS="${2:-10 20 40}"; DURATION="${3:-2m}"; HOST="${4:-${LOCUST_HOST:-http://localhost:8000}}"
cd "$(dirname "$0")/.."
OUT="loadtest/results/$LABEL"; mkdir -p "$OUT"
LOCUST="${LOCUST:-locust}"

drain() {  # wait until both queues are empty so one step's backlog does not pollute the next
  for _ in $(seq 1 120); do
    left=$(python3 - "$HOST" <<'PY'
import re, sys, urllib.request
m = urllib.request.urlopen(sys.argv[1] + "/metrics", timeout=5).read().decode()
print(int(sum(float(v) for v in re.findall(r'^queue_depth\{[^}]*\} ([0-9.e+]+)$', m, re.M))))
PY
)
    [ "$left" = "0" ] && return 0
    sleep 2
  done
}

for u in $STEPS; do
  drain
  echo ">>> $LABEL: $u users (pacing ${PACING_S:-20}s => offered $(python3 -c "print(round($u/${PACING_S:-20},2))") runs/s)"
  "$LOCUST" -f loadtest/locustfile.py --headless -u "$u" -r "$u" -t "$DURATION" --host "$HOST" \
    --csv "$OUT/u$u" --only-summary >/dev/null 2>&1 || true
done
python3 loadtest/summarise.py "$OUT" | tee "$OUT/summary.md"
