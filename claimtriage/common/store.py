"""Redis-backed run store, reliable work queue, quarantine, cache, rate limiter
and feedback store. Redis runs with AOF persistence (see docker-compose.yml).

Key layout
  run:{id}               JSON run record (status, request, result, cost, ...)
  run:{id}:events        list of JSON step events (full reconstruction, D5)
  runs:index             sorted set of run ids by submission time
  q:{name}               pending jobs           (LPUSH / BLMOVE)
  q:{name}:processing    jobs leased by a worker
  lease:{job_id}         lease key with TTL; expiry => job is requeued by reaper
  q:dead                 dead-letter list
  quarantine             list of rejected records with field-level errors
  cache:run:{sha}        whole-run cache
  rl:{client}:{minute}   rate-limit counters
  feedback:{run_id}      feedback records, feedback:all aggregated list
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import redis.asyncio as aioredis

from .config import get_settings

_client: aioredis.Redis | None = None


def redis_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


# ---------------------------------------------------------------- runs
async def create_run(run_id: str, record: dict) -> None:
    r = redis_client()
    pipe = r.pipeline()
    pipe.set(f"run:{run_id}", _dumps(record))
    pipe.zadd("runs:index", {run_id: time.time()})
    await pipe.execute()


async def get_run(run_id: str) -> dict | None:
    raw = await redis_client().get(f"run:{run_id}")
    return json.loads(raw) if raw else None


async def update_run(run_id: str, **fields: Any) -> dict:
    """Read-modify-write under WATCH so concurrent stage updates do not clobber."""
    r = redis_client()
    key = f"run:{run_id}"
    async with r.pipeline() as pipe:
        while True:
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                rec = json.loads(raw) if raw else {"run_id": run_id}
                rec.update(fields)
                pipe.multi()
                pipe.set(key, _dumps(rec))
                await pipe.execute()
                return rec
            except aioredis.WatchError:
                continue


async def append_event(run_id: str, event: dict) -> None:
    event.setdefault("ts", time.time())
    await redis_client().rpush(f"run:{run_id}:events", _dumps(event))


async def get_events(run_id: str) -> list[dict]:
    raw = await redis_client().lrange(f"run:{run_id}:events", 0, -1)
    return [json.loads(x) for x in raw]


async def recent_runs(limit: int = 50) -> list[str]:
    return await redis_client().zrevrange("runs:index", 0, limit - 1)


# ---------------------------------------------------------------- reliable queue
async def enqueue(queue: str, payload: dict, attempts: int = 0) -> str:
    job = {"job_id": uuid.uuid4().hex, "queue": queue, "attempts": attempts, "payload": payload,
           "enqueued_at": time.time()}
    await redis_client().lpush(f"q:{queue}", _dumps(job))
    return job["job_id"]


async def dequeue(queue: str, timeout_s: float = 2.0) -> tuple[dict, str] | None:
    """Atomically move a job to the processing list and take a lease on it."""
    r = redis_client()
    raw = await r.blmove(f"q:{queue}", f"q:{queue}:processing", timeout_s, "RIGHT", "LEFT")
    if raw is None:
        return None
    job = json.loads(raw)
    await r.set(f"lease:{job['job_id']}", "1", ex=int(get_settings().job_lease_s))
    return job, raw


async def renew_lease(job_id: str) -> None:
    await redis_client().set(f"lease:{job_id}", "1", ex=int(get_settings().job_lease_s))


async def ack(queue: str, raw: str, job_id: str) -> None:
    r = redis_client()
    await r.lrem(f"q:{queue}:processing", 1, raw)
    await r.delete(f"lease:{job_id}")


async def reap_expired(queue: str) -> list[dict]:
    """Requeue jobs whose worker died mid-run (lease expired). Jobs that exhausted
    their attempts are dead-lettered and returned so the caller can escalate."""
    r = redis_client()
    settings = get_settings()
    dead: list[dict] = []
    for raw in await r.lrange(f"q:{queue}:processing", 0, -1):
        job = json.loads(raw)
        if await r.exists(f"lease:{job['job_id']}"):
            continue
        if await r.lrem(f"q:{queue}:processing", 1, raw) == 0:
            continue  # another reaper got it
        job["attempts"] = job.get("attempts", 0) + 1
        if job["attempts"] >= settings.job_max_attempts:
            await r.lpush("q:dead", _dumps(job))
            dead.append(job)
        else:
            await r.lpush(f"q:{queue}", _dumps(job))
    return dead


async def queue_depth(queue: str) -> tuple[int, int]:
    r = redis_client()
    return await r.llen(f"q:{queue}"), await r.llen(f"q:{queue}:processing")


# ---------------------------------------------------------------- quarantine
async def quarantine(record: dict) -> str:
    qid = "Q-" + uuid.uuid4().hex[:12]
    record = {"quarantine_id": qid, "quarantined_at": time.time(), **record}
    r = redis_client()
    await r.lpush("quarantine", _dumps(record))
    await r.ltrim("quarantine", 0, 9999)
    return qid


async def list_quarantine(limit: int = 50) -> list[dict]:
    return [json.loads(x) for x in await redis_client().lrange("quarantine", 0, limit - 1)]


# ---------------------------------------------------------------- cache
async def cache_get(key: str) -> dict | None:
    raw = await redis_client().get(f"cache:{key}")
    return json.loads(raw) if raw else None


async def cache_set(key: str, value: dict, ttl_s: int) -> None:
    await redis_client().set(f"cache:{key}", _dumps(value), ex=ttl_s)


# ---------------------------------------------------------------- rate limit
async def rate_limit_hit(client_id: str, limit_per_min: int) -> tuple[bool, int]:
    """Fixed one-minute window. Returns (allowed, remaining)."""
    minute = int(time.time() // 60)
    key = f"rl:{client_id}:{minute}"
    r = redis_client()
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 120)
    count, _ = await pipe.execute()
    return count <= limit_per_min, max(0, limit_per_min - count)


# ---------------------------------------------------------------- feedback
async def add_feedback(run_id: str, record: dict) -> None:
    r = redis_client()
    record = {"run_id": run_id, "ts": time.time(), **record}
    await r.rpush(f"feedback:{run_id}", _dumps(record))
    await r.rpush("feedback:all", _dumps(record))


async def all_feedback() -> list[dict]:
    return [json.loads(x) for x in await redis_client().lrange("feedback:all", 0, -1)]


# ---------------------------------------------------------------- live quality samples
async def add_quality_sample(sample: dict) -> None:
    r = redis_client()
    await r.rpush("quality:samples", _dumps(sample))
    await r.ltrim("quality:samples", -5000, -1)


async def quality_samples(limit: int = 500) -> list[dict]:
    return [json.loads(x) for x in await redis_client().lrange("quality:samples", -limit, -1)]
