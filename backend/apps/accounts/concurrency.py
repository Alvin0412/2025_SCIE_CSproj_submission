from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

from asgiref.sync import sync_to_async
from django.conf import settings

try:
    import redis.asyncio as redis_async
except ImportError:  # pragma: no cover
    redis_async = None

from .models import User
from .subscriptions import ensure_account_meta, get_default_plan

CONCURRENCY_WINDOW_SECONDS = int(getattr(settings, "ACCOUNTS_CONCURRENCY_WINDOW_SECONDS", 60))
_redis_client = None
_client_lock = asyncio.Lock()


class ConcurrencyLimitError(Exception):
    """Raised when the user exceeds their concurrent AI search quota."""


async def _get_client():
    if redis_async is None:
        raise RuntimeError("redis.asyncio is required for concurrency enforcement.")
    global _redis_client
    if _redis_client is None:
        async with _client_lock:
            if _redis_client is None:
                _redis_client = redis_async.from_url(
                    getattr(settings, "REDIS_URL", "redis://localhost:6379"),
                    decode_responses=True,
                )
    return _redis_client


async def acquire_search_slot(user: User, *, ttl: int | None = None) -> str:
    """Reserve a slot for active AI searches using Redis sorted sets."""

    ttl = ttl or CONCURRENCY_WINDOW_SECONDS
    meta = getattr(user, "account_meta", None)
    if meta is None or meta.plan is None:
        meta = ensure_account_meta(user)
        if not meta.plan:
            meta.plan = get_default_plan()
            meta.save(update_fields=["plan"])
        limit = meta.plan.concurrency_limit or 1
    else:
        limit = meta.plan.concurrency_limit or 1
    client = await _get_client()
    key = f"acct:concurrency:{user.pk}"
    token = uuid.uuid4().hex
    now_ms = int(time.time() * 1000)
    expire_at = now_ms + ttl * 1000
    lua = """
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
    local current = redis.call('ZCARD', KEYS[1])
    if current >= tonumber(ARGV[2]) then
        return {0, current}
    end
    redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
    redis.call('PEXPIRE', KEYS[1], ARGV[5])
    return {1, current + 1}
    """
    inserted, _ = await client.eval(
        lua,
        1,
        key,
        now_ms,
        limit,
        expire_at,
        ttl * 1000,
        token,
    )
    if inserted != 1:
        raise ConcurrencyLimitError("Concurrent AI search limit reached.")
    return token


async def release_search_slot(user: User, token: str):
    if not token:
        return
    client = await _get_client()
    key = f"acct:concurrency:{user.pk}"
    await client.zrem(key, token)


@asynccontextmanager
async def search_concurrency_guard(user_id: str | int, *, ttl: int | None = None):
    """Async context manager that enforces concurrency limits for a user."""

    user = await sync_to_async(
        lambda: User.objects.select_related("account_meta", "account_meta__plan").filter(pk=user_id).first()
    )()
    if not user:
        yield None
        return
    token = await acquire_search_slot(user, ttl=ttl)
    try:
        yield user
    finally:
        await release_search_slot(user, token)
