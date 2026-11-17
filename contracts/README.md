# Data contracts (D1)

Every external interface has a versioned, published contract. The JSON Schemas in this folder are
generated from the code (`python -m claimtriage.tools.export_contracts`), so the code and the
schemas cannot drift. They are also served live at `GET /v1/contract/fnol`.

| Interface | Direction | Contract |
|---|---|---|
| `POST /v1/claims` | in | [fnol.v1.schema.json](fnol.v1.schema.json) + the cross-field rules below |
| `GET /v1/runs/{id}` -> `result` | out | [recommendation.v1.schema.json](recommendation.v1.schema.json) |
| `POST /v1/runs/{id}/feedback` | in | [feedback.v1.schema.json](feedback.v1.schema.json) |
| `GET /v1/runs/{id}/trace` | out | run record + ordered event list (`kind` in llm, tool, verification, handoff, guardrail, degraded, stage, final) |

## FNOL v1.0: validation at the ingestion boundary

Checks run in order, before any agent starts. The first failing layer **quarantines** the record
(Redis `quarantine` list, with field-level errors and `agent_run_started: false`) and returns `422`.
No run id is created and nothing is enqueued.

| Layer | Examples | `reason` |
|---|---|---|
| Parse | body is not JSON | `malformed_json` |
| Schema / type | unknown field (`extra="forbid"`), missing required field, wrong type | `schema_validation` |
| Range | `idv_inr` 10,000-50,000,000. `estimated_repair_cost_inr` 0-10,000,000. Description 20-4,000 chars. `year_of_manufacture` >= 1980 | `schema_validation` |
| Category / format | `channel`, `vehicle_class`, `fuel_type`, `injuries` enums. Indian registration number (incl. BH series), 6-digit PIN, Indian mobile, DL number, `claim_reference` `FNOL-\d{6,12}` | `schema_validation` |
| Cross-field | policy end after start, period <= 3 years. Loss not in the future. Reported not before loss and not > 3 years after. Vehicle not manufactured after the loss. `fir_number` required iff `police_report_filed`. Estimate <= 2 x IDV. A fatal accident needs a police report | `cross_field_validation` |
| Guardrail | prompt-injection patterns in any free-text field that can reach a model | `prompt_injection` |

Things that are **not** contract violations, because they are claim facts the system must reason about:
loss outside the policy period, an expired licence at the date of loss, late intimation, a vehicle class
or product outside the corpus. These are accepted, then routed to investigation or escalated.

Idempotency: a repeated `claim_reference` returns the existing run (`200`, `duplicate: true`).
