"""Fixed-window rate limiting. Redis when available; an in-process fallback for dev and tests."""

import time
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from lanvex.core.errors import RateLimited
from lanvex.core.logging import get_logger

log = get_logger(__name__)


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> None: ...
    async def reset(self, key: str) -> None: ...


class MemoryRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, int]] = {}

    async def hit(self, key: str, limit: int, window_seconds: int) -> None:
        now = time.monotonic()
        start, count = self._buckets.get(key, (now, 0))
        if now - start >= window_seconds:
            start, count = now, 0
        count += 1
        self._buckets[key] = (start, count)
        if count > limit:
            retry = max(1, int(window_seconds - (now - start)))
            raise RateLimited(f"Too many requests. Try again in {retry} seconds.")

    async def reset(self, key: str) -> None:
        self._buckets.pop(key, None)


class RedisRateLimiter:
    def __init__(self, redis: Redis, fallback: MemoryRateLimiter | None = None) -> None:
        self._redis = redis
        self._fallback = fallback or MemoryRateLimiter()

    async def hit(self, key: str, limit: int, window_seconds: int) -> None:
        rkey = f"lanvex:rl:{key}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(rkey)
                pipe.expire(rkey, window_seconds, nx=True)
                pipe.ttl(rkey)
                count, _, ttl = await pipe.execute()
        except RedisError:
            # Fail closed would lock everyone out when Redis blips; fall back to per-process limits instead.
            log.warning("rate_limiter_redis_unavailable")
            await self._fallback.hit(key, limit, window_seconds)
            return
        if int(count) > limit:
            raise RateLimited(f"Too many requests. Try again in {max(int(ttl), 1)} seconds.")

    async def reset(self, key: str) -> None:
        try:
            await self._redis.delete(f"lanvex:rl:{key}")
        except RedisError:
            await self._fallback.reset(key)
