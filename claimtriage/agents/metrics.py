"""Per-agent metrics (A6) - exported on :9100/metrics by every agent replica."""
from prometheus_client import Counter, Gauge, Histogram

AGENT_TASKS = Counter("agent_tasks_total", "Agent stage executions by outcome", ["agent", "outcome"])
AGENT_STEPS = Histogram("agent_steps_per_run", "Reasoning steps per run", ["agent"],
                        buckets=(1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20))
AGENT_TOOL_CALLS = Histogram("agent_tool_calls_per_run", "Tool calls per run", ["agent"],
                             buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10, 12))
STEP_LAT = Histogram("agent_step_latency_seconds", "Latency of one agent step", ["agent", "step"],
                     buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21))
STAGE_LAT = Histogram("agent_stage_latency_seconds", "Latency of a whole agent stage", ["agent"],
                      buckets=(0.1, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89))
RUN_E2E = Histogram("run_e2e_latency_seconds", "Submission to final result", ["outcome"],
                    buckets=(1, 2, 3, 5, 8, 10, 13, 16, 20, 25, 30, 40, 60, 90, 120, 180))
RETRIES = Counter("agent_retries_total", "Retries by kind (tool, llm, verification, requeue)", ["agent", "kind"])
TOOL_CALLS = Counter("agent_tool_invocations_total", "Tool invocations", ["agent", "tool", "outcome"])
VERIFICATION = Counter("verification_total", "Verification outcomes", ["result"])
ESCALATIONS = Counter("escalations_total", "Escalations by reason", ["reason"])
DEGRADED = Counter("degraded_runs_total", "Runs completed on the fallback path", ["agent"])
GUARDRAIL = Counter("guardrail_events_total", "Guardrail triggers", ["kind"])
RUN_COST = Histogram("run_cost_usd", "Model cost per run (USD)",
                     buckets=(0, 0.0005, 0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064))
LIVE_QUALITY = Histogram("live_quality_score", "Automatic quality metrics on sampled live runs", ["metric"],
                         buckets=(0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0))
INFLIGHT = Gauge("agent_inflight_jobs", "Jobs currently being processed", ["agent"])
