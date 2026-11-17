"""LLM gateway: the only component that talks to the model provider.

- per-call timeout (httpx), bounded retry with exponential backoff + jitter
- circuit breaker on the provider (closed -> open -> half-open)
- token + cost accounting at call time, attributed to run / agent / step (L5)
- fault injection endpoint so the breaker can be demonstrated on request (A5)

Deployment boundary: one place to enforce provider limits and cost controls;
agents scale out without multiplying breaker state, and changing provider is a
config change to this container only.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from ..common.config import get_settings
from ..common.logging import bind, configure_logging, get_logger, reset_context
from ..common.resilience import CircuitBreaker, CircuitOpenError, RetryableError, retry_async
from ..common.telemetry import init_tracing, instrument_fastapi, set_attrs, tracer
from .providers import InvalidOutputError, ProviderError, RateLimitedError, build_provider

SERVICE = "llm-gateway"
settings = get_settings()
configure_logging(SERVICE, settings.log_level)
init_tracing(SERVICE)
log = get_logger(__name__)

LLM_REQ = Counter("llm_requests_total", "LLM calls", ["provider", "task", "outcome"])
LLM_LAT = Histogram("llm_call_latency_seconds", "LLM call latency incl. retries", ["provider", "task"],
                    buckets=(0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34))
LLM_RETRY = Counter("llm_retries_total", "Retries against the provider", ["provider"])
LLM_RATE_LIMITED = Counter("llm_rate_limited_total", "429 waits honoured (provider pacing)", ["provider"])
LLM_TOKENS = Counter("llm_tokens_total", "Tokens", ["provider", "agent", "step", "kind"])
LLM_COST = Counter("llm_cost_usd_total", "Cost in USD at call time", ["provider", "agent", "step"])
BREAKER_STATE = Gauge("llm_circuit_state", "0=closed 1=half_open 2=open")
BREAKER_OPENS = Counter("llm_circuit_open_total", "Times the breaker opened")
HTTP_LAT = Histogram("http_request_duration_seconds", "HTTP latency", ["service", "route", "method", "status"])

_STATE_NUM = {"closed": 0, "half_open": 1, "open": 2}


def _on_state(state: str) -> None:
    BREAKER_STATE.set(_STATE_NUM[state])
    if state == "open":
        BREAKER_OPENS.inc()
    log.warning("circuit breaker state change", state=state)


breaker = CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_reset_s, on_state_change=_on_state)
provider = build_provider(settings)
fault = {"provider_down": False, "extra_latency_s": 0.0}

app = FastAPI(title="claimtriage llm-gateway", version="1.0")
instrument_fastapi(app)


@app.middleware("http")
async def ctx(request: Request, call_next):
    reset_context(SERVICE)
    bind(correlation_id=request.headers.get("x-correlation-id", ""), run_id=request.headers.get("x-run-id", ""))
    t0 = time.perf_counter()
    resp = await call_next(request)
    route = request.scope.get("route")
    HTTP_LAT.labels(SERVICE, getattr(route, "path", request.url.path), request.method, resp.status_code) \
        .observe(time.perf_counter() - t0)
    return resp


class CompleteIn(BaseModel):
    task: str = Field(pattern=r"^[a-z_]{3,40}$")
    messages: list[dict] = Field(min_length=1, max_length=20)
    json_mode: bool = True
    max_tokens: int = Field(default=700, ge=16, le=4096)
    temperature: float = Field(default=0.0, ge=0, le=1.5)
    meta: dict = {}


def _cost(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * settings.llm_price_input_per_1m_usd
            + completion_tokens * settings.llm_price_output_per_1m_usd) / 1_000_000


@app.post("/v1/complete")
async def complete(body: CompleteIn):
    agent = str(body.meta.get("agent", "unknown"))
    step = str(body.meta.get("step", body.task))
    attempts = {"n": 0}
    t0 = time.perf_counter()

    async def call():
        attempts["n"] += 1
        breaker.before_call()  # raises CircuitOpenError without touching the provider

        async def provider_call():
            if fault["provider_down"]:
                raise RetryableError("fault injection: provider unavailable")
            if fault["extra_latency_s"]:
                await asyncio.sleep(fault["extra_latency_s"])
            return await provider.complete(body.messages, task=body.task, json_mode=body.json_mode,
                                           max_tokens=body.max_tokens, temperature=body.temperature)

        try:
            # hard per-attempt timeout around the whole provider call (A5). A 429 is the
            # provider pacing us, not failing: wait as told and try again, outside the
            # timeout and without touching the breaker, up to LLM_RATE_LIMIT_WAITS times.
            for wait_no in range(settings.llm_rate_limit_waits + 1):
                try:
                    out = await asyncio.wait_for(provider_call(), timeout=settings.llm_timeout_s)
                    break
                except RateLimitedError as exc:
                    if wait_no == settings.llm_rate_limit_waits:
                        raise
                    LLM_RATE_LIMITED.labels(provider.name).inc()
                    pause = min(exc.retry_after + 0.5, settings.llm_rate_limit_max_wait_s)
                    log.warning("provider rate limit, waiting", seconds=round(pause, 1), wait_no=wait_no + 1)
                    await asyncio.sleep(pause)
        except RateLimitedError:
            raise
        except asyncio.TimeoutError:
            breaker.record_failure()
            raise RetryableError(f"provider call timed out after {settings.llm_timeout_s}s")
        except RetryableError:
            breaker.record_failure()
            raise
        except InvalidOutputError:
            breaker.record_success()  # the provider answered; the answer was bad
            raise
        except ProviderError:
            breaker.record_failure()
            raise
        breaker.record_success()
        return out

    with tracer().start_as_current_span("llm.complete") as span:
        set_attrs(span, task=body.task, agent=agent, step=step, provider=provider.name,
                  prompt_id=body.meta.get("prompt_id"), prompt_version=body.meta.get("prompt_version"))
        try:
            out = await retry_async(call, attempts=settings.llm_max_retries, base_delay_s=settings.llm_backoff_base_s,
                                    retry_on=(RetryableError,),
                                    on_retry=lambda n, e: LLM_RETRY.labels(provider.name).inc())
        except CircuitOpenError:
            LLM_REQ.labels(provider.name, body.task, "circuit_open").inc()
            span.set_attribute("outcome", "circuit_open")
            raise HTTPException(503, {"error": "circuit_open", "breaker": breaker.snapshot()})
        except RetryableError as exc:
            LLM_REQ.labels(provider.name, body.task, "unavailable").inc()
            span.set_attribute("outcome", "unavailable")
            log.error("provider unavailable after retries", error=str(exc), attempts=attempts["n"])
            raise HTTPException(503, {"error": "provider_unavailable", "detail": str(exc),
                                      "attempts": attempts["n"], "breaker": breaker.snapshot()})
        except InvalidOutputError as exc:
            LLM_REQ.labels(provider.name, body.task, "invalid_output").inc()
            log.warning("model output rejected by provider", error=str(exc)[:300])
            raise HTTPException(422, {"error": "invalid_output", "detail": str(exc)[:300]})
        except ProviderError as exc:
            LLM_REQ.labels(provider.name, body.task, "error").inc()
            log.error("provider error", error=str(exc)[:300])
            raise HTTPException(502, {"error": "provider_error", "detail": str(exc)})
        latency = time.perf_counter() - t0
        cost = _cost(out.prompt_tokens, out.completion_tokens)
        set_attrs(span, prompt_tokens=out.prompt_tokens, completion_tokens=out.completion_tokens, cost_usd=cost,
                  attempts=attempts["n"])
    LLM_REQ.labels(provider.name, body.task, "ok").inc()
    LLM_LAT.labels(provider.name, body.task).observe(latency)
    LLM_TOKENS.labels(provider.name, agent, step, "prompt").inc(out.prompt_tokens)
    LLM_TOKENS.labels(provider.name, agent, step, "completion").inc(out.completion_tokens)
    LLM_COST.labels(provider.name, agent, step).inc(cost)
    log.info("llm call", task=body.task, agent=agent, step=step, latency_ms=round(latency * 1000),
             prompt_tokens=out.prompt_tokens, completion_tokens=out.completion_tokens, cost_usd=round(cost, 6),
             attempts=attempts["n"])
    return {"content": out.content, "provider": out.provider, "model": out.model,
            "usage": {"prompt_tokens": out.prompt_tokens, "completion_tokens": out.completion_tokens},
            "cost_usd": cost, "latency_ms": round(latency * 1000, 1), "attempts": attempts["n"],
            "retries": attempts["n"] - 1}


class FaultIn(BaseModel):
    provider_down: bool = False
    extra_latency_s: float = Field(default=0.0, ge=0, le=60)


@app.post("/admin/fault")
async def set_fault(body: FaultIn):
    """Demonstration hook: simulate the provider being unavailable or slow."""
    if not settings.fault_injection_enabled:
        raise HTTPException(403, "fault injection disabled (FAULT_INJECTION_ENABLED=false)")
    fault.update(body.model_dump())
    log.warning("fault injection updated", **fault)
    return {"fault": fault, "breaker": breaker.snapshot()}


@app.get("/admin/breaker")
async def breaker_state():
    return {"breaker": breaker.snapshot(), "fault": fault, "provider": provider.name}


@app.get("/health")
async def health():
    # healthy even when the breaker is open: the gateway itself is serving and
    # reporting degradation is its job; readiness of the provider is in /admin/breaker
    return {"status": "ok", "provider": provider.name, "breaker": breaker.state}


@app.get("/metrics")
async def metrics():
    BREAKER_STATE.set(_STATE_NUM[breaker.state])
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
