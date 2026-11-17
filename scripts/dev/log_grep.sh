#!/usr/bin/env bash
# Summarise provider errors in the local gateway log (dev helper).
cd "$(dirname "$0")/../../data/logs" || exit 1
echo "429 waits:  $(grep -c 'rate limit, waiting' gateway.log)"
echo "413 errors: $(grep -c '413 Payload' gateway.log)"
grep -o 'http 4[0-9][0-9]: [^"]\{0,10\}.\{0,400\}' gateway.log | grep -v 'http 429' | tail -2
grep -o 'prompt_tokens": [0-9]*' gateway.log | tail -10 | tr '\n' ' '; echo
