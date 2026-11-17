"""Agent worker process. AGENT_ROLE=triage consumes q:triage (intake, finalize);
AGENT_ROLE=coverage consumes q:coverage. Each replica runs WORKER_CONCURRENCY
jobs concurrently (the work is I/O-bound: model and retriever calls).

Failure handling (A2):
- Jobs are leased (BLMOVE to a processing list + TTL lease, renewed while
  running). If a replica dies mid-run its lease expires and any replica's
  reaper requeues the job; after JOB_MAX_ATTEMPTS it is dead-lettered and the
  run is ESCALATED to a human with the reason - never silently dropped.
- An exception inside a stage is retried once on another pass, then escalated.
- The coverage agent failing mid-run does not lose triage's work: the hand-off
  packet is on the run record, so a retry restarts only the coverage stage.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time

from prometheus_client import start_http_server

from ..common import store
from ..common.config import get_settings
from ..common.logging import bind, configure_logging, get_logger, reset_context
from ..common.telemetry import extracted_context, init_tracing, inject_carrier, set_attrs, tracer
from . import metrics as M
from .runtime import RunContext

settings = get_settings()
ROLE = settings.agent_role
SERVICE = f"{ROLE}-agent"
configure_logging(SERVICE, settings.log_level)
init_tracing(SERVICE)
log = get_logger(__name__)
CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "4"))
_stop = asyncio.Event()


def _ctx(run: dict, payload: dict, agent: str) -> RunContext:
    return RunContext(run_id=run["run_id"], correlation_id=payload.get("correlation_id", run["run_id"]), agent=agent,
                      deadline=run["submitted_at"] + settings.agent_wall_clock_s,
                      max_steps=settings.agent_max_steps, max_tool_calls=settings.agent_max_tool_calls,
                      overrides=dict(payload.get("overrides") or {}))


async def _accumulate_cost(run_id: str, ctx: RunContext) -> None:
    run = await store.get_run(run_id) or {}
    cost = run.get("cost") or {"total_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "by_step": {}}
    cost["total_usd"] = round(cost["total_usd"] + ctx.cost_usd, 6)
    cost["prompt_tokens"] += ctx.prompt_tokens
    cost["completion_tokens"] += ctx.completion_tokens
    cost["by_step"] = {**cost.get("by_step", {}), **ctx.by_step}
    await store.update_run(run_id, cost=cost, prompt_versions={**run.get("prompt_versions", {}),
                                                               **ctx.prompt_versions})


async def escalate_failed_run(run_id: str, reason: str) -> None:
    """Terminal failure path: hand the run to a human, never a confident answer."""
    run = await store.get_run(run_id)
    if not run or run.get("status") in ("completed", "escalated"):
        return
    from .triage import run_finalize

    ctx = RunContext(run_id=run_id, correlation_id=run.get("correlation_id", run_id), agent="triage",
                     deadline=time.time() + 30, max_steps=1, max_tool_calls=0)
    try:
        if run.get("handoff"):
            await run_finalize(ctx, run, {"escalate": True, "coverage_position": "indeterminate",
                                          "escalation_reasons": [reason], "citations": []})
            return
    except Exception as exc:  # fall through to a minimal record
        log.error("finalize during escalation failed", error=str(exc))
    await store.update_run(run_id, status="escalated", stage="done", completed_at=time.time(),
                           result={"escalation": {"escalated": True, "reasons": [reason],
                                                  "path": "Human review queue -> claims handler (system failure)"},
                                   "coverage_position": "indeterminate", "route": None, "citations": [],
                                   "rationale": "The run did not complete; no recommendation was formed."})
    M.ESCALATIONS.labels("system_failure").inc()


async def handle(job: dict) -> None:
    payload = job["payload"]
    run_id = payload["run_id"]
    run = await store.get_run(run_id)
    if not run:
        log.warning("job for unknown run", run_id=run_id)
        return
    reset_context(SERVICE)
    bind(run_id=run_id, correlation_id=payload.get("correlation_id", run_id), agent=ROLE)
    stage = payload.get("stage", "coverage" if ROLE == "coverage" else "intake")
    agent = "coverage" if ROLE == "coverage" else "triage"
    t0 = time.perf_counter()
    with extracted_context(payload.get("trace")):
        with tracer().start_as_current_span(f"{agent}.{stage}") as span:
            set_attrs(span, run_id=run_id, attempt=job.get("attempts", 0))
            ctx = _ctx(run, payload, agent)
            if stage == "intake":
                from .triage import run_intake

                await store.update_run(run_id, status="running", stage="intake")
                await ctx.event("stage", "intake_start", attempt=job.get("attempts", 0))
                await run_intake(ctx, run)
                await _accumulate_cost(run_id, ctx)
                M.AGENT_STEPS.labels("triage").observe(ctx.steps)
            elif stage == "coverage":
                from .coverage import run_coverage

                await ctx.event("stage", "coverage_start", attempt=job.get("attempts", 0))
                result = await run_coverage(ctx, run.get("sanitized_fnol") or run["request"], run["handoff"])
                await _accumulate_cost(run_id, ctx)
                M.AGENT_STEPS.labels("coverage").observe(ctx.steps)
                M.AGENT_TOOL_CALLS.labels("coverage").observe(ctx.tool_calls)
                await store.update_run(run_id, stage="finalize")
                await ctx.event("handoff", "coverage->triage", verified=result["verified"],
                                escalate=result["escalate"], reasons=result["escalation_reasons"])
                await store.enqueue("triage", {"run_id": run_id, "stage": "finalize",
                                               "correlation_id": ctx.correlation_id, "trace": inject_carrier(),
                                               "overrides": payload.get("overrides") or {}, "coverage": result})
            elif stage == "finalize":
                from .triage import run_finalize

                await run_finalize(ctx, run, payload["coverage"])
            else:
                raise ValueError(f"unknown stage {stage}")
    M.STAGE_LAT.labels(agent).observe(time.perf_counter() - t0)
    M.AGENT_TASKS.labels(agent, "success").inc()


async def _renew(job_id: str, done: asyncio.Event) -> None:
    while not done.is_set():
        try:
            await asyncio.wait_for(done.wait(), timeout=settings.job_lease_s / 3)
        except asyncio.TimeoutError:
            await store.renew_lease(job_id)


async def consumer(queue: str, n: int) -> None:
    while not _stop.is_set():
        got = await store.dequeue(queue, timeout_s=2)
        if not got:
            continue
        job, raw = got
        done = asyncio.Event()
        renew = asyncio.create_task(_renew(job["job_id"], done))
        M.INFLIGHT.labels(ROLE).inc()
        try:
            await handle(job)
        except Exception as exc:
            agent = "coverage" if ROLE == "coverage" else "triage"
            M.AGENT_TASKS.labels(agent, "failure").inc()
            log.exception("stage failed", run_id=job["payload"].get("run_id"), attempts=job.get("attempts", 0))
            attempts = job.get("attempts", 0) + 1
            if attempts < settings.job_max_attempts:
                M.RETRIES.labels(agent, "requeue").inc()
                await store.enqueue(queue, job["payload"], attempts=attempts)
            else:
                await escalate_failed_run(job["payload"]["run_id"],
                                          f"{agent} agent failed after {attempts} attempts: {type(exc).__name__}")
        finally:
            done.set()
            renew.cancel()
            M.INFLIGHT.labels(ROLE).dec()
            await store.ack(queue, raw, job["job_id"])


async def reaper(queue: str) -> None:
    while not _stop.is_set():
        try:
            for job in await store.reap_expired(queue):
                log.error("job dead-lettered after lease expiry", run_id=job["payload"].get("run_id"))
                await escalate_failed_run(job["payload"]["run_id"],
                                          f"{ROLE} agent stopped mid-run (lease expired); retries exhausted")
        except Exception:
            log.exception("reaper error")
        await asyncio.sleep(10)


async def main() -> None:
    start_http_server(settings.metrics_port)
    queue = "coverage" if ROLE == "coverage" else "triage"
    log.info("worker starting", role=ROLE, queue=queue, concurrency=CONCURRENCY)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop.set)
        except NotImplementedError:
            pass
    await asyncio.gather(reaper(queue), *[consumer(queue, i) for i in range(CONCURRENCY)])


if __name__ == "__main__":
    asyncio.run(main())
