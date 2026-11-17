# Incident log

Real failures from development, recorded as they happened.

---

### INC-011 - Groq daily token allowance exhausted during the prompt comparison (2026-11-16)

- **Impact:** the v1-vs-v2 prompt comparison on a real model could not finish. After about 25 successful
  calls, every request returned 429. Runs degraded safely (escalated to a human), as designed.
- **Detection:** gateway log `Rate limit reached ... tokens per day (TPD): Limit 200000, Used 198974`, for
  every model: the daily limit is per account, not per model.
- **Root cause:** a coverage run uses 40,000-50,000 tokens (about 6 calls of 5,000-7,000), so the free tier
  supports only about 4 claims a day. Waiting out a per-minute limit does not help with a daily one.
- **Action:** stopped the comparison. Lowered `AGENT_STATE_CHAR_BUDGET` for the free tier (cuts tokens per
  run), added `LLM_EXTRA_BODY` so models such as gpt-oss can be used by configuration, and kept the
  comparison as one command (`scripts/dev/groq_compare.sh`) to run after the allowance resets.
- **Lesson:** budget a real-model evaluation in tokens per day before starting. A paid tier or a local
  model is the realistic option for a 26-scenario evaluation.

### INC-007 - First real-model run: Groq rate limit treated as an outage (2026-11-16)

- **Impact:** with the Groq free tier switched on, the difficult case (S03) escalated as "LLM
  unavailable" after 26 s. The degraded path worked as designed, but no answer was ever formed.
- **Detection:** gateway log `http 429: Rate limit reached ... on input tokens per minute (ITPM): Limit 7000`.
- **Root cause:** a 429 was handled like a 5xx: five quick retries with at most an 8 s backoff, each
  counted as a failure by the circuit breaker. The free tier allows about 8,000 tokens a minute, and one
  coverage step sends 2,000-6,000, so the limit resets on a minute scale.
- **Fix:** a dedicated `RateLimitedError`. The gateway waits the time the provider asks for
  (`retry-after` / `x-ratelimit-reset-tokens`, parsed from `975ms`, `12.96s`, `1m2s`), outside the
  per-call timeout, up to `LLM_RATE_LIMIT_WAITS` times, and **never** trips the breaker. A new metric,
  `llm_rate_limited_total`, records it. Pacing is the provider throttling us, not failing.

### INC-008 - Requests rejected for size (413) and for invalid JSON (400) (2026-11-16)

- **Impact:** after INC-007 was fixed, runs reached the 6th-7th model call and then escalated.
- **Root cause:** (1) the coverage agent's STATE block grew with every search (2,400 to 6,600
  tokens), and Groq counts prompt + `max_tokens` against the per-request limit, so later steps got
  HTTP 413. (2) Groq JSON mode returned `400 json_validate_failed` when the final answer was cut off at
  `max_tokens=700`. The gateway mapped it to a generic provider error, so the run degraded.
- **Fix:** `AGENT_STATE_CHAR_BUDGET` shortens older passages first, keeping the general
  exclusions and the newest search intact. Coverage `max_tokens` was raised to 1,100.
  `json_validate_failed` becomes an `invalid_output` reply (HTTP 422): the agent retries the step with
  a "keep it short" hint, and the breaker is not touched (the provider answered; the answer was bad).

### INC-009 - TLS error escaped the gateway's error handling (2026-11-16)

- **Impact:** one run escalated within 4 s with "LLM unavailable: 500".
- **Root cause:** on the unstable home network an `ssl.SSLError: SSLV3_ALERT_BAD_RECORD_MAC` was raised
  outside the `httpx` exception types the provider caught, so FastAPI returned a bare 500.
- **Fix:** the provider now treats `httpx.HTTPError`, `ssl.SSLError` and `OSError` as retryable
  transport failures.

### INC-010 - The configured model was retired by the provider (2026-11-16)

- **Impact:** the planned model (`llama-3.1-8b-instant`) was no longer offered. The available chat
  models were `openai/gpt-oss-20b`, `openai/gpt-oss-120b` and `qwen/qwen3.8-27b`.
- **Fix:** none in code. The model id is pure configuration (D4), so switching to `qwen/qwen3.8-27b` was
  a one-line `.env` change. We verified with a JSON-mode probe that each model returns valid JSON before
  choosing. gpt-oss models need an extra `reasoning_effort` parameter the gateway does not send.
- **Lesson:** check the provider's live model list (`GET /models`) before a demo.

---

### INC-001 - Gateway 500 crashed the triage agent instead of degrading (2026-10-27)

- **Impact:** in the first full evaluation run, 3 of 26 scenarios (S05, S06, S22) ended
  `escalated` with "triage agent failed after 2 attempts: HTTPStatusError" instead of a
  recommendation.
- **Detection:** eval summary (`outcome` accuracy 0.88). The triage log showed
  `HTTPStatusError: Server error '500 Internal Server Error' for url .../v1/complete`.
- **Root cause (two faults):**
  1. All three FNOLs had no repair estimate, so `estimate_to_idv_ratio` was `None`. The rule
     `derived.get("estimate_to_idv_ratio", 0) >= 0.75` raised `TypeError` (`.get` returns
     `None`, not the default, when the key exists). The gateway returned HTTP 500.
  2. `llm_json` treated only 502/503/504 as "LLM unavailable". A 500 went through
     `raise_for_status()` as an unhandled exception, so the stage was retried and then
     escalated as a system failure. **The degraded path was bypassed.**
- **Fix:** `(derived.get(...) or 0)`. Any 5xx from the gateway now raises `LLMUnavailable`
  and takes the defined fallback path.
- **Follow-up:** this is why the runbook says to treat "degraded" escalations as a signal.
  A bug can look like an outage.

### INC-002 - Regex word boundaries silently corrupted into backspace characters (2026-10-27)

- **Impact:** the out-of-corpus rule for non-motor claims (S22, a health claim on the motor
  form) and the tyre-damage exclusion cue never matched. S22 was routed as a covered own-damage
  claim. **No error was raised anywhere.**
- **Detection:** eval scenario S22 marked `BAD`. Calling the rule directly returned no match
  for text containing "dengue". `repr()` of the source line showed `\x08` where `\b` was intended.
- **Root cause:** a code patch applied through a shell heredoc passed `"\\b"` through two
  layers of escaping and wrote a literal backspace (0x08) into the `r"..."` pattern.
- **Fix:** rewrote the lines, then scanned the whole package for control characters
  (`ord(ch) < 9`). Two files were affected.
- **Lesson:** the fixed evaluation set caught a bug that unit tests and type checks could
  not. Patches are now applied with the editor, not shell heredocs.

### INC-003 - Verifier rejected a correct answer for the difficult case (2026-10-27)

- **Impact:** S03 (food delivery) escalated after 3 verification attempts despite a correct
  draft that cited Exclusion 3(a) and the permitted-use passage.
- **Detection:** first smoke run: `V6_term_resolved: cited clause PW-S5-001 depends on 'Driver's Clause'`.
- **Root cause:** V6 checked every decisive term mentioned *anywhere in the cited chunk*. The
  General Exclusions chunk also contains 3(b), the Driver's Clause, which the answer did not
  rely on.
- **Fix:** V6 now checks only terms in the citation's **quote**.
- **Lesson:** an over-strict verifier turns into an escalation generator. Chunk granularity
  (several sub-clauses per chunk) matters for verification as well as retrieval.

### INC-004 - Corpus acquisition: the deciding clause is usually not published; one insurer blocks downloads (2026-10-27)

- **Impact:** a longer corpus search. The first-choice documents could not support the
  difficult case.
- **Detail:** New India Assurance's official package wording refers to the "Limitations as
  to Use" (General Exception 3(a)) but does not contain them; they are in the per-customer
  schedule. The same was true for SBI General, Raheja QBE, Tata AIG, Liberty and Magma.
  ICICI Lombard's wording does contain a "Limitations as to Use" chapter, but its CDN
  returned an HTML bot-challenge page (HTTP 200, `Content-Type: text/html`) to `curl`
  and 403 to other fetchers.
- **Resolution:** Universal Sompo publishes the permitted-use text in the product
  prospectus (same UIN). Both documents are now in `corpus/` with sha256 recorded in the
  manifest. Ingestion warns if a file's hash differs from the manifest.
- **Lesson:** verify that a downloaded "PDF" is a PDF (`file`, magic bytes) before indexing.
  A 200 response does not mean you got the document.

### INC-006 - First `docker compose up --build` failed: PyPI read timeout on a slow network (2026-10-27)

- **Impact:** the first clean build failed after about 16 minutes. `target ingest: failed to solve:
  pip install -r requirements/retriever.txt ... exit code: 2`. No containers started.
- **Detection:** compose output. The build log showed `ReadTimeoutError: HTTPSConnectionPool(host=
  'files.pythonhosted.org') Read timed out` while downloading at about 70-100 kB/s.
- **Root cause:** pip's default 15 s read timeout and 5 retries are too tight for a slow
  connection, and `PIP_NO_CACHE_DIR=1` meant every retry restarted all downloads from zero
  (about 450 MB for the retriever image).
- **Fix:** `PIP_DEFAULT_TIMEOUT=120`, `PIP_RETRIES=10`, and a BuildKit pip cache mount
  (`RUN --mount=type=cache,target=/root/.cache/pip`), so a re-run resumes from the wheels already
  downloaded. The cache stays out of the final image layers.
- **Lesson:** "starts from a clean clone" (D3) includes the build on the examiner's network,
  not just on ours.

### INC-005 - Local dev environment: no `ensurepip` in WSL Ubuntu, no sudo (2026-10-27)

- **Impact:** could not create a virtualenv for local (non-Docker) runs.
- **Fix:** installed `uv` in the user directory. `uv venv --python 3.11` provides a
  managed CPython matching the Docker base image. No sudo was needed.
