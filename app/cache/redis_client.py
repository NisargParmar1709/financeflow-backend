"""
app/cache/redis_client.py — Cache Layer (Redis via Upstash)

WHY THIS FILE EXISTS:
  Wraps the raw redis-py async client into a thin class with:
    - Lifecycle management (connect/disconnect called from main.py lifespan)
    - Typed helper methods (get_json, set_json, delete, exists)
    - A fallback pattern: if Redis is down, fall back to DB silently

CACHING STRATEGY FROM VIDEO 13:
  We use "Cache-aside" (Lazy caching):
    1. Request comes in → check Redis first
    2. Cache HIT → return cached data immediately (no DB query)
    3. Cache MISS → query DB → store result in Redis → return data
    4. On data mutation (PUT/DELETE) → invalidate related cache keys

WHY REDIS INSTEAD OF IN-MEMORY DICT:
  In-memory cache lives inside one process. When we scale to multiple
  Render instances, each instance has its own memory — they can't share cache.
  Redis is an external shared store — all instances read/write the same cache.

KEY DESIGN (from Video 21 — Scaling):
  All keys follow: "financeflow:{resource}:{user_id}:{sub_key}"
  Examples:
    "financeflow:expenses:abc-123:list"
    "financeflow:analytics:abc-123:monthly_2024_01"
  Why namespaced: prevents collisions and makes bulk invalidation easy.
  See app/cache/keys.py for all key builders.

TTL STRATEGY:
  Different data has different staleness tolerance:
    - Analytics summaries: 300s (5 min) — computed aggregations, expensive
    - Expense lists: 60s — changes often, but slight staleness is acceptable
    - User profile: 3600s (1 hour) — rarely changes
"""

import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)


class RedisClient:
    """
    Thin wrapper around redis.asyncio.Redis.

    Why a class instead of module-level functions:
    Classes let us manage the connection lifecycle (connect/disconnect)
    cleanly. The FastAPI lifespan handler calls connect() on startup
    and disconnect() on shutdown — ensuring the connection pool is
    properly initialized and drained.
    """

    def __init__(self) -> None:
        # _client is None until connect() is called.
        # This prevents accidental usage before the app is ready.
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        """
        Called ONCE during FastAPI startup (in app/main.py lifespan).

        Why decode_responses=True: Redis stores bytes. With this flag,
        redis-py automatically decodes bytes → str, so we don't have
        to call .decode() everywhere. Since we JSON-encode all values
        anyway, we always get strings back.
        """
        self._client = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
        # Verify the connection is alive at startup.
        # If Upstash credentials are wrong, this fails NOW (fail-fast).
        await self._client.ping()  # type: ignore[union-attr]
        logger.info("Redis connected successfully")

    async def disconnect(self) -> None:
        """
        Called during FastAPI shutdown (graceful shutdown — Video 19).

        Why: Closing the connection pool releases resources cleanly.
        Without this, the event loop may hang waiting for pending connections.
        """
        if self._client:
            await self._client.aclose()
            logger.info("Redis disconnected")

    @property
    def client(self) -> aioredis.Redis:
        """
        Safe accessor — raises early if connect() was never called.
        Better than a cryptic NullPointerError deep in the call stack.
        """
        if self._client is None:
            raise RuntimeError(
                "RedisClient.connect() was never called. "
                "Is the FastAPI lifespan handler set up in main.py?"
            )
        return self._client

    # ── Core Operations ───────────────────────────────────────────────────────

    async def get_json(self, key: str) -> Any | None:
        """
        Fetches a cached value and deserializes it from JSON.

        Returns None on cache MISS (key doesn't exist) or if Redis is down.
        Never raises — callers should treat None as "go fetch from DB".

        WHY WE CATCH EXCEPTIONS:
          Redis is not a critical dependency for FinanceFlow (see System Design
          Doc 4 risk matrix). If Upstash goes down, the app should degrade
          gracefully — slower, but still functional. We log the error for
          monitoring but don't crash the request.
        """
        try:
            value = await self.client.get(key)
            if value is None:
                return None
            return json.loads(value)
        except Exception as e:
            # Log for monitoring (Grafana, Sentry) but don't crash the request
            logger.warning(f"Redis GET failed for key '{key}': {e}")
            return None

    async def set_json(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        """
        Serializes value to JSON and stores it with a TTL.

        Why always set a TTL:
          Never store without expiry — you'll fill Redis and hit the 10k
          req/day Upstash free limit. TTL ensures stale data auto-expires.

        Args:
            key: Cache key (use builders from app/cache/keys.py)
            value: Any JSON-serializable Python object
            ttl_seconds: Time-to-live. Defaults to 300s (5 minutes).
        """
        try:
            serialized = json.dumps(value, default=str)
            # ex= sets expiry in seconds (Redis TTL)
            await self.client.set(key, serialized, ex=ttl_seconds)
        except Exception as e:
            # Non-critical: cache write failure just means next request won't
            # find it in cache. DB query will run instead. Log, don't crash.
            logger.warning(f"Redis SET failed for key '{key}': {e}")

    async def delete(self, *keys: str) -> None:
        """
        Deletes one or more cache keys (cache invalidation).

        Called when data is mutated (POST/PUT/DELETE) to prevent stale reads.
        Example: after adding a new expense, delete the expense list cache
        for that user so the next GET fetches fresh data from DB.

        Why *keys (variadic): Often you need to invalidate multiple related
        keys at once (expense list + analytics summary + monthly totals).
        One call is cleaner than three separate await delete() calls.
        """
        try:
            if keys:
                await self.client.delete(*keys)
        except Exception as e:
            logger.warning(f"Redis DELETE failed for keys {keys}: {e}")

    async def exists(self, key: str) -> bool:
        """
        Checks if a key exists without fetching its value.
        Useful for rate limiting checks and idempotency keys.
        """
        try:
            return bool(await self.client.exists(key))
        except Exception as e:
            logger.warning(f"Redis EXISTS failed for key '{key}': {e}")
            # Assume key doesn't exist — safe default
            return False

    async def increment(self, key: str, ttl_seconds: int = 60) -> int:
        """
        Atomically increments a counter. Used for rate limiting.

        WHY ATOMIC: Redis is single-threaded. INCR is guaranteed atomic —
        no two requests can INCR at the exact same nanosecond and both
        get the same value. This makes it perfect for rate limiting counters.

        Sets TTL on first increment (window expiry for rate limiter).
        Returns the new count after increment.
        """
        try:
            pipe = self.client.pipeline()
            await pipe.incr(key)
            # Only set TTL on the first increment (when key didn't exist)
            await pipe.expire(key, ttl_seconds, nx=True)  # nx=True: only set if not exists
            results = await pipe.execute()
            return results[0]  # The count after INCR
        except Exception as e:
            logger.warning(f"Redis INCR failed for key '{key}': {e}")
            # Return 0 — rate limiter won't block if Redis is down (graceful degradation)
            return 0


# ── Module-level singleton ────────────────────────────────────────────────────
# Imported in main.py for lifecycle management and in services for cache ops.
# Usage: from app.cache.redis_client import redis_client
redis_client = RedisClient()