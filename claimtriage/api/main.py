"""Public API (FastAPI).

Submission is asynchronous (A4): POST /v1/claims validates at the boundary,
then enqueues and returns 202 with a run id immediately; the caller polls
GET /v1/runs/{id}. Polling rather than callbacks because (1) callers such as
call-centre desktops and the web client are not reachable for inbound HTTP,
(2) a callback endpoint is an SSRF and retry-semantics surface we do not need,
(3) polling is idempotent and survives client restarts. A Redis queue sits
between submission and execution.

Ingestion boundary (D1): JSON parse -> schema/type/range/category (pydantic) ->
cross-field rules -> prompt-injection guardrail. Any failure quarantines the
record with field-level errors and returns 422; no run is created and no
agent is invoked.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter as Count
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import ValidationError

from ..common import guardrails, store
from ..common.config import get_settings
from ..common.contracts import FNOL, FeedbackIn, cross_field_errors
from ..common.logging import bind, configure_logging, get_logger, reset_context
from ..common.telemetry import current_trace_id, init_tracing, inject_carrier, instrument_fastapi, set_attrs, tracer

SERVICE = "api"
settings = get_settings()
configure_logging(SERVICE, settings.log_level)
init_tracing(SERVICE)
log = get_logger(__name__)

HTTP_LAT = Histogram("http_request_duration_seconds", "HTTP latency", ["service", "route", "method", "status"])
SUBMISSIONS = Counter("fnol_submissions_total", "FNOL submissions by outcome", ["outcome"])
QUARANTINED = Counter("fnol_quarantined_total", "Quarantined records by reason", ["reason"])
QUEUE_DEPTH = Gauge("queue_depth", "Jobs waiting / in processing", ["queue", "state"])
FEEDBACK = Counter("feedback_total", "Feedback received", ["satisfactory"])
GUARD = Counter("guardrail_events_total", "Guardrail triggers", ["kind"])

app = FastAPI(title="Motor Claim Intake & Triage API", version="1.0",
              description="Advisory FNOL triage. The system never approves or rejects a claim.")
instrument_fastapi(app)
STATIC = Path(__file__).parent / "static"
PUBLIC_FIELDS = ("run_id", "claim_reference", "status", "stage", "submitted_at", "completed_at", "e2e_seconds",
                 "result", "cost", "prompt_versions", "cache_hit", "correlation_id", "trace_id")


@app.middleware("http")
async def context_mw(request: Request, call_next):
    reset_context(SERVICE)
    cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    request.state.correlation_id = cid
    bind(correlation_id=cid)
    t0 = time.perf_counter()
    resp = await call_next(request)
    route = request.scope.get("route")
    HTTP_LAT.labels(SERVICE, getattr(route, "path", request.url.path), request.method, resp.status_code) \
        .observe(time.perf_counter() - t0)
    resp.headers["x-correlation-id"] = cid
    return resp


def _client_id(request: Request) -> str:
    return request.headers.get("x-client-id") or (request.client.host if request.client else "anonymous")


def _cache_key(fnol: dict) -> str:
    """Whole-run cache key: the claim content, excluding identifiers that differ
    between duplicate submissions of the same loss (reference, channel, timestamps
    of reporting, contact details)."""
    f = json.loads(json.dumps(fnol, default=str))
    f.pop("claim_reference", None)
    f.pop("channel", None)
    f.pop("claimant", None)
    f.get("incident", {}).pop("reported_at", None)
    return hashlib.sha256(json.dumps(f, sort_keys=True).encode()).hexdigest()


async def _reject(reason: str, errors: list, raw: object, client: str, cid: str) -> JSONResponse:
    qid = await store.quarantine({"reason": reason, "errors": errors, "record": raw, "client_id": client,
                                  "correlation_id": cid, "agent_run_started": False})
    QUARANTINED.labels(reason).inc()
    SUBMISSIONS.labels("quarantined").inc()
    log.warning("fnol quarantined", reason=reason, quarantine_id=qid, errors=errors[:5])
    return JSONResponse(status_code=422, content={"status": "rejected", "reason": reason, "quarantine_id": qid,
                                                  "errors": errors, "agent_run_started": False})


@app.post("/v1/claims", status_code=202)
async def submit_claim(request: Request):
    client = _client_id(request)
    cid = request.state.correlation_id
    allowed, remaining = await store.rate_limit_hit(client, settings.rate_limit_per_min)
    if not allowed:
        SUBMISSIONS.labels("rate_limited").inc()
        return JSONResponse(status_code=429, headers={"Retry-After": "60"},
                            content={"status": "rate_limited", "limit_per_min": settings.rate_limit_per_min})
    with tracer().start_as_current_span("api.ingest") as span:
        raw_bytes = await request.body()
        try:
            raw = json.loads(raw_bytes or b"null")
        except json.JSONDecodeError as exc:
            return await _reject("malformed_json", [{"loc": "body", "msg": str(exc), "type": "json"}],
                                 raw_bytes[:2000].decode("utf-8", "replace"), client, cid)
        try:
            fnol = FNOL.model_validate(raw)
        except ValidationError as exc:
            errs = [{"loc": ".".join(map(str, e["loc"])), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
            return await _reject("schema_validation", errs, raw, client, cid)
        xerrs = cross_field_errors(fnol)
        if xerrs:
            return await _reject("cross_field_validation", xerrs, raw, client, cid)
        data = fnol.model_dump(mode="json")
        inj = guardrails.detect_injection(guardrails.free_text_fields(data))
        if not inj.ok:
            GUARD.labels("prompt_injection").inc()
            return await _reject("prompt_injection", inj.findings, raw, client, cid)

        existing = await store.redis_client().get(f"claimref:{fnol.claim_reference}")
        if existing:
            SUBMISSIONS.labels("duplicate").inc()
            return JSONResponse(status_code=200, content={"run_id": existing, "status_url": f"/v1/runs/{existing}",
                                                          "duplicate": True})
        overrides: dict = {}
        if settings.allow_eval_overrides and request.headers.get("x-eval-overrides"):
            overrides = json.loads(request.headers["x-eval-overrides"])
        if settings.fault_injection_enabled and request.headers.get("x-fault"):
            overrides["fault"] = request.headers["x-fault"]
        run_id = "run_" + uuid.uuid4().hex[:16]
        cache_key = _cache_key(data)
        bind(run_id=run_id)
        set_attrs(span, run_id=run_id, claim_reference=fnol.claim_reference)
        record = {"run_id": run_id, "claim_reference": fnol.claim_reference, "status": "queued", "stage": "queued",
                  "submitted_at": time.time(), "correlation_id": cid, "client_id": client, "request": data,
                  "cache_key": cache_key, "overrides": overrides, "trace_id": current_trace_id(),
                  "cost": {"total_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "by_step": {}}}

        cached = None if overrides or not settings.run_cache_enabled else await store.cache_get("run:" + cache_key)
        if cached:
            record.update(status="escalated" if cached["result"]["escalation"]["escalated"] else "completed",
                          stage="done", result=cached["result"], completed_at=time.time(), e2e_seconds=0.0,
                          cache_hit=True, cached_from=cached["source_run"])
            await store.create_run(run_id, record)
            await store.redis_client().set(f"claimref:{fnol.claim_reference}", run_id, ex=7 * 86400)
            await store.append_event(run_id, {"kind": "cache", "name": "run_cache_hit", "source_run":
                                              cached["source_run"], "agent": "api"})
            SUBMISSIONS.labels("cache_hit").inc()
            return {"run_id": run_id, "status": record["status"], "status_url": f"/v1/runs/{run_id}",
                    "cache_hit": True}

        await store.create_run(run_id, record)
        await store.redis_client().set(f"claimref:{fnol.claim_reference}", run_id, ex=7 * 86400)
        await store.append_event(run_id, {"kind": "stage", "name": "accepted", "agent": "api",
                                          "trace_id": current_trace_id()})
        await store.enqueue("triage", {"run_id": run_id, "stage": "intake", "correlation_id": cid,
                                       "trace": inject_carrier(), "overrides": overrides})
    SUBMISSIONS.labels("accepted").inc()
    log.info("fnol accepted", run_id=run_id, claim_reference=fnol.claim_reference)
    return {"run_id": run_id, "status": "queued", "status_url": f"/v1/runs/{run_id}",
            "trace_url": f"/v1/runs/{run_id}/trace", "rate_limit_remaining": remaining}


@app.get("/v1/runs/{run_id}")
async def get_run(run_id: str):
    run = await store.get_run(run_id)
    if not run:
        raise HTTPException(404, "unknown run id")
    return {k: run.get(k) for k in PUBLIC_FIELDS if k in run}


@app.get("/v1/runs/{run_id}/trace")
async def get_trace(run_id: str):
    """Full reconstruction of a run: every tool call, query, passage, prompt
    version, per-step latency and cost, and the verification outcome."""
    run = await store.get_run(run_id)
    if not run:
        raise HTTPException(404, "unknown run id")
    events = await store.get_events(run_id)
    t0 = run.get("submitted_at", 0)
    for e in events:
        e["t_offset_s"] = round(e.get("ts", t0) - t0, 3)
    trace_ids = sorted({e.get("trace_id") for e in events if e.get("trace_id")})
    return {"run": {k: v for k, v in run.items() if k not in ("cache_key",)}, "events": events,
            "summary": {
                "llm_calls": sum(1 for e in events if e.get("kind") == "llm"),
                "tool_calls": sum(1 for e in events if e.get("kind") == "tool"),
                "verification_attempts": sum(1 for e in events if e.get("kind") == "verification"),
                "prompt_versions": run.get("prompt_versions"),
                "cost": run.get("cost"),
                "jaeger_trace_ids": trace_ids,
            }}


@app.post("/v1/runs/{run_id}/feedback", status_code=201)
async def feedback(run_id: str, body: FeedbackIn):
    run = await store.get_run(run_id)
    if not run:
        raise HTTPException(404, "unknown run id")
    res = run.get("result") or {}
    await store.add_feedback(run_id, body.model_dump() | {"route": res.get("route"),
                                                          "coverage_position": res.get("coverage_position"),
                                                          "prompt_versions": run.get("prompt_versions")})
    FEEDBACK.labels(str(body.satisfactory).lower()).inc()
    return {"status": "recorded"}


@app.get("/v1/feedback/summary")
async def feedback_summary():
    fb = await store.all_feedback()
    n = len(fb)
    sat = sum(1 for f in fb if f["satisfactory"])
    by_route: dict = {}
    for f in fb:
        r = by_route.setdefault(str(f.get("route")), {"n": 0, "satisfactory": 0})
        r["n"] += 1
        r["satisfactory"] += int(f["satisfactory"])
    reasons = Count(f["reason"].strip().lower()[:60] for f in fb if not f["satisfactory"]).most_common(5)
    return {"total": n, "satisfactory": sat, "satisfaction_rate": round(sat / n, 3) if n else None,
            "by_route": by_route, "top_unsatisfactory_reasons": reasons}


@app.get("/v1/quality/summary")
async def quality_summary(bucket_minutes: int = 60):
    """Automatic evaluation metrics on a sample of live runs, bucketed over time."""
    samples = await store.quality_samples()
    buckets: dict = {}
    for s in samples:
        b = int(s["ts"] // (bucket_minutes * 60)) * bucket_minutes * 60
        agg = buckets.setdefault(b, {"n": 0, "groundedness": [], "context_relevance": [], "faithfulness": [],
                                     "hallucination_pre": []})
        agg["n"] += 1
        for k in ("groundedness", "context_relevance", "faithfulness"):
            if s.get(k) is not None:
                agg[k].append(s[k])
        agg["hallucination_pre"].append(1.0 if s.get("hallucination_pre") else 0.0)
    out = []
    for b in sorted(buckets):
        a = buckets[b]
        out.append({"bucket_start": b, "n": a["n"], **{k: round(sum(v) / len(v), 3) if v else None
                                                       for k, v in a.items() if k != "n"}})
    return {"samples": len(samples), "series": out}


@app.get("/v1/quarantine")
async def quarantine(limit: int = 20):
    return {"items": await store.list_quarantine(limit)}


@app.get("/v1/contract/fnol")
async def contract():
    return FNOL.model_json_schema()


@app.get("/health")
async def health():
    checks = {}
    try:
        checks["redis"] = await store.redis_client().ping()
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    async with httpx.AsyncClient(timeout=2.0) as c:
        for name, url in (("retriever", settings.retriever_url), ("llm_gateway", settings.llm_gateway_url)):
            try:
                checks[name] = (await c.get(url.rstrip("/") + "/health")).json().get("status")
            except Exception as exc:
                checks[name] = f"error: {type(exc).__name__}"
    ok = checks.get("redis") is True
    return JSONResponse(status_code=200 if ok else 503,
                        content={"status": "ok" if ok and all(v in (True, "ok") for v in checks.values())
                                 else "degraded" if ok else "down", "checks": checks})


@app.get("/metrics")
async def metrics():
    for q in ("triage", "coverage"):
        try:
            waiting, processing = await store.queue_depth(q)
            QUEUE_DEPTH.labels(q, "waiting").set(waiting)
            QUEUE_DEPTH.labels(q, "processing").set(processing)
        except Exception:
            pass
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def ui():
    return FileResponse(STATIC / "index.html")


@app.get("/samples.json")
async def samples():
    from ..evaluation.scenarios import samples as load_samples

    return load_samples()
