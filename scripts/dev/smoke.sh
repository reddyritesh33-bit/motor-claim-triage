#!/usr/bin/env bash
# End-to-end smoke test against a running stack (local or compose with port 8000 published):
# valid run + feedback, the difficult case, a rejected injection, health, quality and feedback summaries.
#   bash scripts/dev/smoke.sh [api_url]
set -euo pipefail
cd "$(dirname "$0")/../.."
API="${1:-http://localhost:8000}"
PY="${PYTHON:-python3}"
[ -x "$HOME/claimtriage-venv/bin/python" ] && PY="$HOME/claimtriage-venv/bin/python"
export API_URL="$API"

echo "== health";            curl -sf "$API/health"; echo
echo "== S19 (fast-track) + feedback"
# sed (not head) so the pipe is read to the end: with pipefail, head's early exit SIGPIPEs grep and aborts the script
RID=$("$PY" -m claimtriage.tools.submit S19 | tee /dev/stderr | grep -o "run_[0-9a-f]*" | sed -n 1p)
curl -sf -XPOST "$API/v1/runs/$RID/feedback" -H "content-type: application/json" \
     -d '{"satisfactory": true, "reason": "correct route and citation"}'; echo
echo "== S03 (section 4.1 case)"; "$PY" -m claimtriage.tools.submit S03
echo "== S15 (prompt injection)"; "$PY" -m claimtriage.tools.submit S15
echo "== feedback summary";  curl -sf "$API/v1/feedback/summary"; echo
echo "== live quality";      curl -sf "$API/v1/quality/summary" | head -c 400; echo
echo "== quarantine (latest)"; curl -sf "$API/v1/quarantine?limit=1" | head -c 400; echo
