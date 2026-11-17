# stub_A_hybrid vs stub_B_dense_only

- A: `stub_A_hybrid` overrides `{}` (stub model, hybrid retrieval (BM25+dense RRF))
- B: `stub_B_dense_only` overrides `{'retrieval_mode': 'dense'}` (stub model, dense-only retrieval)

| metric | A | B | B - A | better |
|---|---|---|---|---|
| accuracy.outcome | 1.0 | 1.0 | +0.0 | = |
| accuracy.claim_type | 1.0 | 1.0 | +0.0 | = |
| accuracy.position | 1.0 | 1.0 | +0.0 | = |
| accuracy.route | 1.0 | 1.0 | +0.0 | = |
| all_correct_rate | 1.0 | 1.0 | +0.0 | = |
| groundedness | 0.529 | 0.5435 | +0.0145 | B |
| context_relevance | 0.3086 | 0.3363 | +0.0277 | B |
| faithfulness | 1.0 | 1.0 | +0.0 | = |
| hallucination_rate_pre_verification | 0.0 | 0.0 | +0.0 | = |
| hallucination_rate_returned | 0.0 | 0.0 | +0.0 | = |
| support_passage_recall | 1.0 | 1.0 | +0.0 | = |
| support_passage_cited | 1.0 | 0.6 | -0.4 | A |
| mean_llm_calls | 4.8261 | 4.8261 | +0.0 | = |
| mean_tool_calls | 2.1304 | 2.1304 | +0.0 | = |
| mean_cost_usd_per_run | 0.0067 | 0.0064 | -0.0003 | B |
| cost_per_100_runs_usd | 0.6712 | 0.6415 | -0.0297 | B |
| e2e_p50_s | 6.26 | 6.078 | -0.182 | B |
| e2e_p95_s | 8.736 | 9.167 | +0.431 | A |

## Scenarios that changed

| id | A (position / route / support cited) | B |
|---|---|---|
| S01 | likely_covered / fast_track / cited 1 | likely_covered / fast_track / cited 0 |
| S02 | likely_covered / surveyor / cited 1 | likely_covered / surveyor / cited 0 |
| S09 | likely_covered / surveyor / cited 1 | likely_covered / surveyor / cited 0 |
| S12 | likely_covered / investigation / cited 1 | likely_covered / investigation / cited 0 |
| S19 | likely_covered / fast_track / cited 1 | likely_covered / fast_track / cited 0 |
| S23 | likely_covered / fast_track / cited 1 | likely_covered / fast_track / cited 0 |
| S24 | likely_covered / fast_track / cited 1 | likely_covered / fast_track / cited 0 |
| S25 | likely_covered / fast_track / cited 1 | likely_covered / fast_track / cited 0 |
