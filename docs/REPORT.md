# Report: Motor Insurance Claim Intake & Triage Service

UE24AM342AA5 ML System Design & AgentOps, Mini Project 01

## 1. Problem and scope

A motor insurer receives First Notices of Loss (FNOL) through a call centre, an app and the web. A
handler has to check each one is complete and consistent, classify it, look up what the policy covers
and route it: fast-track (small cashless repairs), surveyor, or investigation. Getting the coverage
position wrong is a regulatory and financial problem.

The service automates the *recommendation*, not the decision. In scope: a contract-validated intake
API, claim classification and entity extraction, policy retrieval including restrictions stated
elsewhere in the documents, a cited coverage position that has been checked before release, routing
with reasons, and escalation when the system should not answer. Out of scope, by design: approving or
rejecting claims, settlement amounts, model training, and cloud deployment.

**Corpus:** Universal Sompo *Motor Private Car Package Policy* (UIN IRDAN134RP0004V03200708):
the 60-page policy wording, including its Section IX claim procedure, and the 49-page product
prospectus. The prospectus is needed because it states the permitted use of the vehicle (the
"Limitations as to Use"). The wording only refers to that term, in General Exclusion 3(a).
`docs/EVALUATION_1.md` explains why this corpus was chosen over six other insurers.

## 2. Agent design

**Topology (A2):** two agents with distinct responsibilities, separate containers and queues, and a
typed hand-off (diagram in `docs/ARCHITECTURE.md`).

| | Triage agent | Coverage agent |
|---|---|---|
| Owns | the claim from intake to final recommendation | the question "what does the policy say about this claim?" |
| Graph (LangGraph) | guard -> derive facts -> classify -> scope -> *hand-off*; later finalize: route -> output guardrails -> persist | think <-> act (tool loop) -> verify -> {done, retry with feedback and a new strategy, escalate}; degraded fallback |
| Model calls | 1 (`triage_extract`) | 2-6 (`coverage_agent` steps) + 1 verifier (`verifier_support`) |
| Tools | none (facts are derived by code) | `search_policy`, `get_clause`, `find_modifiers`, `get_fnol_field` |
| Scaled | 1 replica | N replicas (`--scale coverage-agent=N`) |

**Hand-off packet:** `{claim_type, secondary_types, entities, facts, research_question}`. It is stored
on the run record, so if the coverage agent dies mid-run only the coverage stage is retried. The
reaper requeues on lease expiry and escalates after the retry budget is spent.

**Retrieval is a tool (A1).** The model picks one action per turn from a JSON protocol. The prompt
(`coverage_agent/v2`) sets out the method the brief requires: find the cover clause, call
`find_modifiers` on it, re-search using a query *derived from what it read* when a provision depends on
a defined term stated elsewhere, check facts, then finalise. `find_modifiers` returns the general
exclusions, deductible and conditions plus the quoted defined terms these rely on. That tells the agent
what it has not yet looked up, without doing the look-up for it.

**Verification (A3), before anything is returned:**

| Check | Rule |
|---|---|
| V1/V2 | valid position. Non-indeterminate positions need citations |
| V3 | every citation's clause was retrieved *in this run* |
| V4 | every quote occurs in that clause (normalised, >= 90% token match) |
| V5 | a modifier search happened (the agent looked beyond the cover clause) |
| V6 | if a quote relies on a decisive defined term ('Limitations as to Use', Driver's Clause), the agent searched for it **and** cited the passage that gives it content |
| V7 | fact-triggered restrictions (commercial use, racing, intoxication) have their governing provision in evidence |
| V8 | an independent model call (`verifier_support`) confirms the cited text supports the rationale |

On failure, the failures go back to the agent as feedback. On the second failure the retrieval strategy
also changes (hybrid to BM25, or to dense). After `VERIFY_MAX_RETRIES` the run escalates. Weak
retrieval (best cosine < 0.30), conflicts, `indeterminate`, out-of-corpus claims and loop-bound breaches
all escalate too (F7).

**Loop limits:** 12 steps, 10 tool calls, 120 s wall clock from submission. Every model and tool call
has its own timeout.

## 3. Architecture and deployment boundaries

See `docs/ARCHITECTURE.md` for the diagram with boundaries marked. In summary: **api** (edge;
independent release and failure isolation), **redis** (queue and state), **triage-agent** /
**coverage-agent** (the scaling unit), **retriever** + one-shot **ingest** (heavy resource profile:
torch and model weights live only here), **llm-gateway** (one breaker and cost meter per provider;
switching provider is configuration), plus **Prometheus, Grafana, Jaeger**. Six application services,
three observability services, and a one-shot ingestion job, all under one `docker compose up`.

## 4. Design decisions and what was rejected

| Decision | Rejected alternative | Why |
|---|---|---|
| Structure-aware chunks with a `[Doc > Section > Sub-heading]` header | fixed 500-token windows | windows straddle cover/exclusion boundaries and lose the heading that marks a passage as an exclusion |
| Hybrid BM25 + dense (RRF) | dense only | measured (section 5): dense-only cites the expected passage in 60% of runs vs 100% |
| Agent derives the second query | hard-coded query expansion; one bigger top-k | the deciding passage never uses the phrase "Limitations as to Use". Measured: neither the case text nor the bare term retrieves it, only a query built from what was read does |
| Deterministic verifier first, model verifier second | model-only verification ("LLM as judge") | exact checks (V3/V4/V6) are free, reproducible and cannot be talked round. The model check covers what string checks cannot |
| Deterministic routing rules | model chooses the route | routing is an auditable business rule grounded in Section IX (intimation, proximity, CTL 75%). The model's job is the coverage position |
| Facts computed by code (delays, proximity, licence, ratio) | model reads dates | LLMs are unreliable at date arithmetic, and these facts decide routing |
| Leased Redis list queues | Celery; in-process asyncio | fewer moving parts, visible leases and dead letters, and an agent crash cannot take the API down |
| 202 + polling | callbacks / websockets | callers are not reachable inbound. A callback URL adds an SSRF surface and retry semantics |
| Separate llm-gateway | provider SDK inside each agent | N replicas would mean N breakers and N retry storms. Cost metering in one place |
| Injection attempts quarantined at the boundary | pass to the model with a warning | agents hold tools. Injected text should never reach them |

## 5. Evaluation (L2, L3)

**Set:** 26 fixed scenarios (`eval/scenarios.yaml`). Each has an expected outcome (routed / escalated
/ rejected, claim type, position, route) and the *verbatim* passage that supports it. They cover:
the section 4.1 case and a ride-hailing variant, theft (prompt and late), drink-driving, fire total
loss, flood, third-party injury, proximity, expired licence, racing, malicious act, tyre-only and
mechanical exclusions, earthquake, riot, outside India, three out-of-corpus claims, injection,
schema and cross-field rejects, and a toxic but genuine claim. Run as a batch:
`python -m claimtriage.evaluation.run_eval --label X`.

**Metrics** (TruLens-equivalent, computed locally; definitions in `claimtriage/evaluation/metrics.py`):
*groundedness*, the share of rationale sentences whose content words are at least 50% covered by the
cited text. *Context relevance*, the mean cosine (MiniLM) between the claim description and each
passage the agent's searches returned. *Faithfulness*, the share of returned citations whose quote is
found in the cited, retrieved clause. *Hallucination rate*, the share of runs in which any draft (even
one later caught by the verifier) cited an unretrieved clause or a quote not in the clause; reported
pre- and post-verification.

**Deliberate change (L3): hybrid vs dense-only retrieval**, with everything else fixed
(deterministic stub model, same 26 scenarios). Full table: `eval/results/stub_A_vs_B_retrieval.md`.

| metric | A: hybrid (BM25 + dense) | B: dense only | change |
|---|---|---|---|
| outcome / position / route accuracy | 1.00 / 1.00 / 1.00 | 1.00 / 1.00 / 1.00 | = |
| groundedness | 0.529 | 0.544 | +0.015 |
| context relevance | 0.309 | 0.336 | +0.028 |
| faithfulness | 1.00 | 1.00 | = |
| hallucination rate (pre / post verification) | 0.00 / 0.00 | 0.00 / 0.00 | = |
| **expected supporting passage cited** | **1.00** | **0.60** | **-0.40** |
| e2e p95 (s) | 8.7 | 9.2 | +0.4 |

How to read this: removing BM25 leaves routing intact but in 8 of 20 routed runs the cover citation
became a *Conditions* clause instead of the Section I peril list ("by accidental external means"). The
answer is right for the wrong reason. Dense similarity rewards passages that *sound like* the
query, and context relevance (a cosine metric) rises for the same reason. That is why we kept hybrid
retrieval, and it shows why "context relevance" alone is not a safe selection metric. **Limitation:**
the stub's verifier model (V8) always approves, so V8 did not catch the weaker citation. That check
needs a real model.

**Prompt comparison (L2):** `coverage_agent/v1` (baseline: tools listed, "answer from what you
find") vs `v2` (explicit cover, modifiers, derived-search method). Each response records the prompt id,
version and sha. The comparison has to run on a real model because the stub ignores prompt text:

```bash
python -m claimtriage.evaluation.run_eval --label groq_v2 --overrides '{"coverage_prompt":"v2"}'
python -m claimtriage.evaluation.run_eval --label groq_v1 --overrides '{"coverage_prompt":"v1"}'
python -m claimtriage.evaluation.compare eval/results/groq_v1.json eval/results/groq_v2.json
```

*Results: see `eval/results/` once run with a Groq key (not available when this report was drafted).*
Expected discriminating metrics: support-passage-cited and position accuracy on S03/S04/S18. v1 has no
instruction to look beyond the cover clause.

## 6. Performance, SLO and cost (D6, A7, L5, D7)

**SLO, declared before testing (`docs/SLO.md`):** complete-run p95 <= 30 s at a sustained 1 run/s, with
the LLM replaced by the stub (0.8-1.5 s per call). The run is measured from submission to final result.

Measured with Locust (full tables in `docs/LOADTEST.md`; local processes, stub LLM):

| configuration | 1 run/s offered: p95 | max sustained throughput | failure point |
|---|---|---|---|
| 1 coverage replica | 26 s (met, saturated) | ~0.9 runs/s | > 0.9 runs/s (p95 49 s at 2 runs/s) |
| **3 coverage replicas** | **13 s (met)** | ~2.5 runs/s | between 2 and 3 runs/s (p95 32 s at 3 runs/s) |

**Bottleneck:** the coverage tier's concurrency slots (replicas x `WORKER_CONCURRENCY`). Every
replica had 4 of 4 jobs in flight while host CPU was about 97% idle and the retriever answered in
milliseconds. A coverage stage is about 6 *sequential* model calls (6.5-8.6 s). Scaling 1 to 3 replicas
gave about 2.8x throughput. Because the work is I/O-bound, raising per-replica concurrency is the
cheaper lever. In production the provider's rate limit caps throughput long before this does.

**Caching (D7).** With the run cache on and repeated claims, 86% of submissions hit: p50 fell to
0.1 s and cost to $0.08 per 100 runs (vs $0.58 with unique claims). p95 moved little, 13 to 11 s,
because the first submission of each burst misses. Details in `docs/LOADTEST.md`.

**Cost (L5).** Cost is recorded at call time per step and per run, priced at the list price for a hosted
70B model ($0.59 / $0.79 per 1M input/output tokens; the free tier bills $0). Over the 26-scenario set:
**about $0.67 per 100 runs**, 4.8 model calls and 2.1 tool calls per run. **Largest driver:
`coverage.think`, 87% of spend** (triage classification 8%, verifier 5%). Each coverage step re-sends the retrieved clauses in its STATE block. We cut this by
showing non-exclusion modifier clauses as 240-character previews (the agent can `get_clause` the full
text). The next lever would be a smaller model for triage and the verifier.

## 7. What did not work / limitations

* **Verifier model under the stub:** V8 is a no-op in stub mode, so citation *relevance* (not just
  correctness) is only enforced with a real model (see the dense-only result above).
* **Chunk granularity vs verification:** a chunk holding several sub-clauses made V6 over-fire
  (INC-003). We scoped V6 to the quote. Sub-clause chunking would be cleaner.
* **Toxicity detection** is lexicon-based (no network dependency, no extra model in the API image). It
  will miss paraphrased abuse. A small classifier in the retriever container is the upgrade path.
* **The "Driver's Clause" and "geographical area"** are defined in the per-policy schedule, which is not
  public, so claims that turn on them can only be escalated (S26 does exactly that).
* **Free-tier throughput:** at roughly 5 calls and several thousand tokens per run, a free-tier
  70B model's tokens-per-minute limit caps real throughput at a few runs per minute. The design
  absorbs this (breaker, degraded path, per-client rate limit), but it cannot remove it.
* **A shell-escaping bug silently disabled two rules (INC-002).** Only the fixed evaluation set caught it.
