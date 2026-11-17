"""Generates dashboards/claimtriage.json (run after editing panels):
    python observability/grafana/build_dashboard.py
"""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prom"}
panels, y = [], 0


def row(title):
    global y
    panels.append({"type": "row", "title": title, "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
                   "id": len(panels) + 1, "panels": []})
    y += 1


def ts(title, exprs, unit="short", w=8, h=8, x=0, kind="timeseries", desc=""):
    p = {"type": kind, "title": title, "datasource": DS, "id": len(panels) + 1, "description": desc,
         "gridPos": {"h": h, "w": w, "x": x, "y": y},
         "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
         "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
         "targets": [{"datasource": DS, "expr": e, "legendFormat": l, "refId": chr(65 + i)}
                     for i, (e, l) in enumerate(exprs)]}
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area"}
    panels.append(p)


def line(*specs):
    global y
    x = 0
    for title, exprs, unit, w, kind in specs:
        ts(title, exprs, unit, w=w, x=x, kind=kind)
        x += w
    y += 8


Q = lambda q, m: f"histogram_quantile({q}, sum(rate({m}_bucket[5m])) by (le))"

row("Service level - complete runs (SLO: p95 <= 30 s)")
line(("Run latency p50 / p95 (submit -> final)",
      [(Q(0.5, "run_e2e_latency_seconds"), "p50"), (Q(0.95, "run_e2e_latency_seconds"), "p95")], "s", 8, "timeseries"),
     ("Throughput (runs/s)",
      [('sum(rate(fnol_submissions_total{outcome="accepted"}[1m]))', "accepted"),
       ("sum(rate(run_e2e_latency_seconds_count[1m])) by (outcome)", "finished {{outcome}}")], "reqps", 8, "timeseries"),
     ("Error rate (5xx share) per service",
      [('sum(rate(http_request_duration_seconds_count{status=~"5.."}[5m])) by (service) / '
        'sum(rate(http_request_duration_seconds_count[5m])) by (service)', "{{service}}")], "percentunit", 8,
      "timeseries"))
line(("Availability (up, 5m avg)", [('avg_over_time(up{job!="cadvisor"}[5m])', "{{job}} {{instance}}")],
      "percentunit", 8,
      "timeseries"),
     ("HTTP p95 latency per service",
      [("histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))",
        "{{service}}")], "s", 8, "timeseries"),
     ("Queue depth", [("queue_depth", "{{queue}} {{state}}")], "short", 8, "timeseries"))

row("Resources per container (one process per container)")
line(("CPU (cores) per container", [("rate(process_cpu_seconds_total[1m])", "{{job}} {{instance}}")], "short", 8,
      "timeseries"),
     ("Memory RSS per container", [("process_resident_memory_bytes", "{{job}} {{instance}}")], "bytes", 8,
      "timeseries"),
     ("Container uptime", [("time() - process_start_time_seconds", "{{job}} {{instance}}")], "s", 8, "timeseries"))

row("Agents (A6)")
line(("Task success rate per agent",
      [('sum(rate(agent_tasks_total{outcome="success"}[5m])) by (agent) / sum(rate(agent_tasks_total[5m])) by (agent)',
        "{{agent}}")], "percentunit", 6, "timeseries"),
     ("Steps per run (mean)",
      [("sum(rate(agent_steps_per_run_sum[5m])) by (agent) / sum(rate(agent_steps_per_run_count[5m])) by (agent)",
        "{{agent}}")], "short", 6, "timeseries"),
     ("Tool calls per run (mean)",
      [("sum(rate(agent_tool_calls_per_run_sum[5m])) by (agent) / sum(rate(agent_tool_calls_per_run_count[5m])) by (agent)",
        "{{agent}}")], "short", 6, "timeseries"),
     ("Retry rate (per s) by kind",
      [("sum(rate(agent_retries_total[5m])) by (agent, kind)", "{{agent}} {{kind}}"),
       ("sum(rate(llm_retries_total[5m]))", "provider retries")], "short", 6, "timeseries"))
line(("Step latency p95 per agent step",
      [("histogram_quantile(0.95, sum(rate(agent_step_latency_seconds_bucket[5m])) by (le, agent, step))",
        "{{agent}} {{step}}")], "s", 12, "timeseries"),
     ("Stage latency p95 per agent",
      [("histogram_quantile(0.95, sum(rate(agent_stage_latency_seconds_bucket[5m])) by (le, agent))", "{{agent}}")],
      "s", 6, "timeseries"),
     ("Verification outcomes", [("sum(increase(verification_total[5m])) by (result)", "{{result}}")], "short", 6,
      "timeseries"))

row("LLM provider, cost and guardrails")
line(("Circuit breaker (0 closed, 1 half-open, 2 open)", [("llm_circuit_state", "state")], "short", 6, "timeseries"),
     ("LLM calls by outcome", [("sum(rate(llm_requests_total[1m])) by (outcome)", "{{outcome}}")], "reqps", 6,
      "timeseries"),
     ("Cost per hour (USD) by step", [("sum(rate(llm_cost_usd_total[5m])) by (agent, step) * 3600",
                                       "{{agent}}.{{step}}")], "currencyUSD", 6, "timeseries"),
     ("Mean cost per run (USD)", [("sum(rate(run_cost_usd_sum[15m])) / sum(rate(run_cost_usd_count[15m]))",
                                   "USD/run")], "currencyUSD", 6, "timeseries"))
line(("Escalations by reason", [("sum(increase(escalations_total[15m])) by (reason)", "{{reason}}")], "short", 8,
      "timeseries"),
     ("Quarantined FNOLs by reason", [("sum(increase(fnol_quarantined_total[15m])) by (reason)", "{{reason}}")],
      "short", 8, "timeseries"),
     ("Guardrail events", [("sum(increase(guardrail_events_total[15m])) by (kind)", "{{kind}}")], "short", 8,
      "timeseries"))

row("Quality over time (live sample) and data freshness")
line(("Live quality (mean of sampled runs)",
      [("sum(rate(live_quality_score_sum[30m])) by (metric) / sum(rate(live_quality_score_count[30m])) by (metric)",
        "{{metric}}")], "percentunit", 8, "timeseries"),
     ("Caching: retrieval cache hit ratio and run-cache hits",
      [('sum(rate(retrieval_requests_total{cache="hit"}[5m])) / sum(rate(retrieval_requests_total[5m]))',
        "retrieval hit ratio"),
       ('sum(rate(fnol_submissions_total{outcome="cache_hit"}[5m]))', "run cache hits/s")], "short", 8, "timeseries"),
     ("Index freshness", [("index_age_seconds", "index age (s)"), ("index_chunks", "chunks"),
                          ("ingest_throughput_chunks_per_second", "ingest chunks/s")], "short", 8, "timeseries"))

dash = {"title": "Claim Triage - Service & Agents", "uid": "claimtriage", "schemaVersion": 39, "version": 1,
        "refresh": "10s", "time": {"from": "now-30m", "to": "now"}, "timezone": "browser", "tags": ["claimtriage"],
        "panels": panels}
out = Path(__file__).parent / "dashboards" / "claimtriage.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dash, indent=1), encoding="utf-8")
print(f"wrote {out} ({len(panels)} panels)")
