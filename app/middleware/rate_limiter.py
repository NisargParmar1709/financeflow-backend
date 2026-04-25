"""
app/middleware/rate_limiter.py — Rate Limiting (Video 20 — Security)

WHY RATE LIMITING EXISTS:
  Without it, a single attacker can:
    - Brute-force login attempts (Video 8 — Auth security)
    - Overwhelm the Gemini AI endpoint (expensive API calls)
    - DoS the server with thousands of requests per second
    - Exhaust the Neon DB connection pool

IMPLEMENTATION — Redis Sliding Window Counter:
  On each request from IP "1.2.3.4":
    1. INCR "financeflow:ratelimit:1.2.3.4" → count
    2. If count == 1: SET expiry to 60 seconds (start of window)
    3. If count > RATE_LIMIT_PER_MINUTE: reject with 429

  Why Redis (not in-memory dict):
    In-memory counters reset when the process restarts and don't
    work with multiple server instances. Redis is shared across all
    Render instances (Video 21 — Horizontal Scaling).

  Why INCR is safe (Video 22 — Concurrency):
    Redis is single-threaded. INCR is atomic — two simultaneous
    requests from the same IP cannot both read "0" and both write "1".
    One will get 1, the other will get 2. No race condition.

LEVELS OF RATE LIMITING (this file implements IP-level):
  - IP-level: 100 req/min (all routes) — prevents DoS
  - User-level: 30 req/min (authenticated routes) — fairer per user
  - AI-level: 10 req/min (AI endpoints) — expensive Gemini calls
"""

import logging
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.cache.redis_client import redis_client
from app.cache.keys import CacheKeys
from app.config import settings

logger = logging.getLogger(__name__)

# Routes exempt from IP rate limiting (health check must never be blocked)
RATE_LIMIT_EXEMPT: set[str] = {
    "/api/v1/health",
    "/docs",
    "/redoc",
    "/openapi.json",
}


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """
    IP-based rate limiting using Redis atomic counters.

    Execution order in main.py:
      Request → AuthGuardMiddleware → RateLimiterMiddleware → Router

    Why after AuthGuard: Authenticated requests can be identified by user
    in logs. Rate limit decisions are made BEFORE reaching route handlers,
    so the route handler only runs for legitimate traffic.
    """

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for exempt routes
        if request.url.path in RATE_LIMIT_EXEMPT:
            return await call_next(request)

        # Get client IP — handles reverse proxy (Render sits behind a proxy)
        client_ip = self._get_client_ip(request)
        rate_limit_key = CacheKeys.rate_limit(client_ip)

        # Atomically increment the counter (see module docstring for why safe)
        current_count = await redis_client.increment(
            key=rate_limit_key,
            ttl_seconds=60,  # 60-second sliding window
        )

        # Add rate limit info to response headers (standard practice)
        # Frontend can read these to show "too many requests" UI proactively
        limit = settings.RATE_LIMIT_PER_MINUTE
        remaining = max(0, limit - current_count)

        if current_count > limit:
            logger.warning(f"Rate limit exceeded: IP={client_ip} count={current_count}")
            response = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": True,
                    "code": "RATE_LIMITED",
                    "message": f"Too many requests. Max {limit} per minute.",
                },
            )
        else:
            response = await call_next(request)

        # Standard rate limit headers (RFC 6585)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = "60"

        return response

    def _get_client_ip(self, request: Request) -> str:
        """
        Extracts the real client IP address.

        WHY X-Forwarded-For: Render sits behind a load balancer/reverse proxy.
        The request.client.host would be the proxy's IP, not the user's.
        The proxy adds X-Forwarded-For with the real client IP.

        We take the FIRST IP in the chain — that's the original client.
        (The header can have multiple IPs: client → proxy1 → proxy2)

        SECURITY: In production, only trust X-Forwarded-For if it comes
        from a trusted proxy. Render's proxy is trusted.
        """
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # "1.2.3.4, 10.0.0.1, 172.16.0.1" → "1.2.3.4"
            return forwarded_for.split(",")[0].strip()

        # Fallback for direct connections (local dev)
        return request.client.host if request.client else "unknown"