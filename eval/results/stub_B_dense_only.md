# Evaluation: stub_B_dense_only

- run at: 2026-10-27 16:10:21
- overrides: `{'retrieval_mode': 'dense'}`
- provider: stub model, dense-only retrieval

## Summary

| metric | value |
|---|---|
| scenarios | 26 |
| accuracy | outcome=1.0, claim_type=1.0, position=1.0, route=1.0 |
| all_correct_rate | 1.0 |
| groundedness | 0.5435 |
| context_relevance | 0.3363 |
| faithfulness | 1.0 |
| hallucination_rate_pre_verification | 0.0 |
| hallucination_rate_returned | 0.0 |
| support_passage_recall | 1.0 |
| support_passage_cited | 0.6 |
| mean_cost_usd_per_run | 0.0064 |
| cost_per_100_runs_usd | 0.6415 |
| largest_cost_driver | coverage.think |
| cost_by_step_usd | coverage.think=0.128186, triage.classify=0.012242, coverage.verify_support=0.007114 |
| mean_llm_calls | 4.8261 |
| mean_tool_calls | 2.1304 |
| e2e_p50_s | 6.078 |
| e2e_p95_s | 9.167 |

## Per scenario

| id | outcome | type | position | route | support retr/cited | ground. | faith. | correct |
|---|---|---|---|---|---|---|---|---|
| S01 | routed | own_damage | likely_covered | fast_track | 1/0 of 1 | 0.5 | 1.0 | yes |
| S02 | routed | own_damage | likely_covered | surveyor | 1/0 of 1 | 0.5 | 1.0 | yes |
| S03 | routed | own_damage | likely_not_covered | investigation | 2/2 of 2 | 1.0 | 1.0 | yes |
| S04 | routed | own_damage | likely_not_covered | investigation | 2/2 of 2 | 1.0 | 1.0 | yes |
| S05 | routed | theft | likely_covered | investigation | 1/1 of 1 | 0.6666666666666666 | 1.0 | yes |
| S06 | routed | theft | covered_with_conditions | investigation | 1/1 of 1 | 0.5 | 1.0 | yes |
| S07 | routed | own_damage | likely_not_covered | investigation | 1/1 of 1 | 0.0 | 1.0 | yes |
| S08 | routed | total_loss | likely_covered | surveyor | 1/1 of 1 | 0.5 | 1.0 | yes |
| S09 | routed | own_damage | likely_covered | surveyor | 1/0 of 1 | 0.5 | 1.0 | yes |
| S10 | routed | third_party | likely_covered | surveyor | 1/1 of 1 | 0.5 | 1.0 | yes |
| S11 | routed | own_damage | covered_with_conditions | investigation | 1/1 of 1 | 0.3333333333333333 | 1.0 | yes |
| S12 | routed | own_damage | likely_covered | investigation | 1/0 of 1 | 0.5 | 1.0 | yes |
| S13 | escalated | own_damage | indeterminate | None | 0/0 of 0 | 0.0 | None | yes |
| S14 | escalated | own_damage | indeterminate | None | 0/0 of 0 | 0.0 | None | yes |
| S15 | rejected |  |  |  | -/- of 0 |  |  | yes |
| S16 | rejected |  |  |  | -/- of 0 |  |  | yes |
| S17 | rejected |  |  |  | -/- of 0 |  |  | yes |
| S18 | routed | own_damage | likely_not_covered | investigation | 1/1 of 1 | 1.0 | 1.0 | yes |
| S19 | routed | own_damage | likely_covered | fast_track | 1/0 of 1 | 0.5 | 1.0 | yes |
| S20 | routed | own_damage | likely_not_covered | investigation | 1/1 of 1 | 1.0 | 1.0 | yes |
| S21 | routed | own_damage | likely_not_covered | investigation | 1/1 of 1 | 1.0 | 1.0 | yes |
| S22 | escalated | own_damage | indeterminate | None | 0/0 of 0 | 0.0 | None | yes |
| S23 | routed | own_damage | likely_covered | fast_track | 1/0 of 1 | 0.5 | 1.0 | yes |
| S24 | escalated | own_damage | likely_covered | fast_track | 1/0 of 1 | 0.5 | 1.0 | yes |
| S25 | routed | own_damage | likely_covered | fast_track | 1/0 of 1 | 0.5 | 1.0 | yes |
| S26 | escalated | own_damage | indeterminate | surveyor | 1/1 of 1 | 1.0 | 1.0 | yes |
