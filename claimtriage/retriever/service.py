"""Retriever service: owns the embedding model and the FAISS/BM25 index.

Deployment boundary: separate container because it has a distinct resource
profile (torch + model weights, ~1 GB RAM) and is read-mostly; agents scale
independently of it, and a re-index never restarts the agents.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from ..common.config import get_settings
from ..common.logging import bind, configure_logging, get_logger, reset_context
from ..common.telemetry import init_tracing, instrument_fastapi, set_attrs, tracer
from .chunker import chunk_document
from .index import PolicyIndex

SERVICE = "retriever"
settings = get_settings()
configure_logging(SERVICE, settings.log_level)
init_tracing(SERVICE)
log = get_logger(__name__)

SEARCH_LAT = Histogram("retrieval_latency_seconds", "Search latency", ["mode", "cache"],
                       buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2))
SEARCHES = Counter("retrieval_requests_total", "Searches", ["mode", "cache"])
INDEX_CHUNKS = Gauge("index_chunks", "Chunks in the index")
INDEX_BUILT = Gauge("index_built_timestamp_seconds", "When the index was built")
INDEX_AGE = Gauge("index_age_seconds", "Seconds since the index was built")
INGEST_TPUT = Gauge("ingest_throughput_chunks_per_second", "Last ingestion throughput (chunks/s)")
INGEST_PAGES = Gauge("ingest_throughput_pages_per_second", "Last ingestion throughput (pages/s)")
INGEST_DURATION = Gauge("ingest_duration_seconds", "Last ingestion duration")
DOC_INFO = Gauge("index_document_info", "Indexed document versions", ["doc_id", "version", "sha256", "chunks"])
HTTP_LAT = Histogram("http_request_duration_seconds", "HTTP latency", ["service", "route", "method", "status"])

app = FastAPI(title="claimtriage retriever", version="1.0")
instrument_fastapi(app)
index = PolicyIndex(settings.index_dir, settings.embedding_model)
_redis = None


def _refresh_gauges() -> None:
    m = index.meta
    INDEX_CHUNKS.set(len(index.order))
    INDEX_BUILT.set(m.get("built_at", 0))
    INGEST_TPUT.set(m.get("throughput_chunks_per_s", 0))
    INGEST_PAGES.set(m.get("throughput_pages_per_s", 0))
    INGEST_DURATION.set(m.get("duration_s", 0))
    DOC_INFO.clear()
    for d in m.get("documents", []):
        DOC_INFO.labels(d["id"], d["version"], d["sha256"][:12], str(d["chunks"])).set(1)


@app.on_event("startup")
async def startup() -> None:
    global _redis
    if not index.exists():
        raise RuntimeError("index missing: run `python -m claimtriage.retriever.ingest` (compose 'ingest' service)")
    index.load()
    index.embed_query("warm up")
    _refresh_gauges()
    if settings.redis_url:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    log.info("retriever ready", chunks=len(index.order), index_version=index.meta.get("index_version"))


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


class SearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    k: int = Field(default=5, ge=1, le=20)
    mode: str = Field(default="", pattern=r"^(|hybrid|dense|bm25)$")
    filters: dict = {}
    use_cache: bool = True


@app.post("/search")
async def search(body: SearchIn):
    mode = body.mode or settings.retrieval_mode
    key = "retr:" + hashlib.sha256(json.dumps([body.query.strip().lower(), body.k, mode, body.filters,
                                               index.meta.get("index_version")], sort_keys=True).encode()).hexdigest()
    t0 = time.perf_counter()
    with tracer().start_as_current_span("retriever.search") as span:
        set_attrs(span, query=body.query, mode=mode, k=body.k)
        if _redis is not None and body.use_cache:
            try:
                cached = await _redis.get(key)
            except Exception:
                cached = None
            if cached:
                SEARCHES.labels(mode, "hit").inc()
                SEARCH_LAT.labels(mode, "hit").observe(time.perf_counter() - t0)
                return {"results": json.loads(cached), "cache_hit": True, "mode": mode}
        results = await asyncio.to_thread(index.search, body.query, body.k, mode, body.filters)
        set_attrs(span, clause_ids=[r["clause_id"] for r in results])
        if _redis is not None:
            try:
                await _redis.set(key, json.dumps(results), ex=settings.retrieval_cache_ttl_s)
            except Exception:
                pass
    SEARCHES.labels(mode, "miss").inc()
    SEARCH_LAT.labels(mode, "miss").observe(time.perf_counter() - t0)
    return {"results": results, "cache_hit": False, "mode": mode}


@app.get("/clause/{clause_id}")
async def clause(clause_id: str):
    c = index.get(clause_id)
    if not c:
        raise HTTPException(404, f"unknown clause id {clause_id}")
    return c


class ModIn(BaseModel):
    clause_id: str


@app.post("/modifiers")
async def modifiers(body: ModIn):
    out = index.modifiers(body.clause_id)
    if "error" in out:
        raise HTTPException(404, out["error"])
    return out


class SimIn(BaseModel):
    a: str
    b: list[str] = Field(max_length=50)


@app.post("/similarity")
async def similarity(body: SimIn):
    """Cosine similarity between `a` and each of `b` (used by evaluation metrics)."""
    return {"scores": await asyncio.to_thread(lambda: [round(index.cosine(body.a, x), 4) for x in body.b])}


class ReembedIn(BaseModel):
    doc_id: str | None = None
    clause_ids: list[str] = []


@app.post("/admin/reembed")
async def reembed(body: ReembedIn):
    """Re-chunk and re-embed one document (or specific clauses) in place, without
    rebuilding the index. Unchanged chunks are skipped by content hash."""
    manifest = yaml.safe_load((Path(settings.corpus_dir) / "manifest.yaml").read_text(encoding="utf-8"))
    docs = [d for d in manifest["documents"] if body.doc_id in (None, d["id"])]
    if not docs:
        raise HTTPException(404, "unknown doc id")
    t0 = time.perf_counter()
    fresh = []
    for d in docs:
        fresh.extend(c.to_dict() for c in chunk_document(str(Path(settings.corpus_dir) / d["file"]), d))
    if body.clause_ids:
        fresh = [c for c in fresh if c["clause_id"] in set(body.clause_ids)]
        for c in fresh:  # force re-embed of the named clauses
            index.chunks.get(c["clause_id"], {}).update({"sha": "stale"})
    stats = await asyncio.to_thread(index.upsert, fresh)
    meta = dict(index.meta)
    meta.update({"built_at": time.time(), "chunks": len(index.order), "last_reembed": stats,
                 "duration_s": round(time.perf_counter() - t0, 3)})
    await asyncio.to_thread(index.save, meta)
    _refresh_gauges()
    log.info("re-embed done", **stats)
    return {"status": "ok", **stats, "chunks": len(index.order)}


@app.get("/index/info")
async def info():
    return index.meta | {"chunks_loaded": len(index.order)}


@app.get("/health")
async def health():
    return {"status": "ok" if index.vs is not None else "loading", "chunks": len(index.order),
            "index_version": index.meta.get("index_version")}


@app.get("/metrics")
async def metrics():
    INDEX_AGE.set(time.time() - index.meta.get("built_at", time.time()))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
