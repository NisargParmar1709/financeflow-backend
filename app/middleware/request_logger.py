"""
app/middleware/request_logger.py — Request Logging Middleware

WHY THIS FILE EXISTS (Video 18 — Observability):
  Every HTTP request that enters the server should produce exactly two log lines:
    1. REQUEST START  → "→ POST /api/v1/expenses" (with trace_id, IP, user)
    2. REQUEST END    → "← POST /api/v1/expenses 201 (42ms)" (with status, duration)

  If a request only produces a START log and no END log, something crashed.
  That's immediately actionable — no guessing needed.

TRACE ID PATTERN (from Video 18 — Distributed Tracing):
  A trace_id is generated HERE (the first middleware to see the request)
  and stored in request.state.trace_id. Every subsequent middleware and
  service can read this ID and include it in their own log lines.

  Result: you can grep one trace_id and see the ENTIRE journey of one
  request — auth check, cache lookup, DB query, response sent.

  Example trace for one request:
    trace_id=req_a3f92c1b | → POST /api/v1/expenses (request_start)
    trace_id=req_a3f92c1b | Auth OK: user_2abc (auth_guard)
    trace_id=req_a3f92c1b | Cache MISS: financeflow:budget:user_2abc (cache)
    trace_id=req_a3f92c1b | DB query: SELECT * FROM budgets WHERE... (service)
    trace_id=req_a3f92c1b | Expense created: exp_xyz789 (service)
    trace_id=req_a3f92c1b | ← POST /api/v1/expenses 201 (67ms) (request_end)

MIDDLEWARE POSITION in main.py (innermost = runs after auth and rate limit):
  CORS → AuthGuard → RateLimiter → RequestLogger → Router

WHY AFTER AUTH: We want user_id in the request log. AuthGuard sets
request.state.clerk_user_id. If RequestLogger ran before AuthGuard,
user_id would always be None.
"""

import time
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request

from app.utils.logging import get_logger, RequestLogger, generate_trace_id

logger = get_logger(__name__)
request_logger = RequestLogger(logger)

# Routes to skip logging (health check pings every 14 min — would flood logs)
LOG_SKIP_PATHS = {"/api/v1/health", "/docs", "/redoc", "/openapi.json"}


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware that logs every request with timing, trace ID, and user context.

    Adds to request.state:
      - trace_id: unique ID for this request ("req_a3f92c1b")
      - start_time: monotonic timestamp for duration calculation

    Usage in services (to include trace_id in service-layer logs):
        from fastapi import Request

        @router.get("/expenses")
        async def list_expenses(request: Request, ...):
            logger.info("Fetching expenses",
                extra={"trace_id": request.state.trace_id})
    """

    async def dispatch(self, request: Request, call_next):
        # Skip noisy health check logs
        if request.url.path in LOG_SKIP_PATHS:
            return await call_next(request)

        # ── Generate trace ID and inject into request state ────────────────────
        trace_id = generate_trace_id()
        request.state.trace_id = trace_id

        # ── Get user_id if already set by AuthGuard ────────────────────────────
        user_id = getattr(request.state, "clerk_user_id", None)

        # ── Get client IP ──────────────────────────────────────────────────────
        client_ip = self._get_ip(request)

        # ── Log request start + capture timing ─────────────────────────────────
        start_time = request_logger.log_request_start(
            method=request.method,
            path=request.url.path,
            client_ip=client_ip,
            trace_id=trace_id,
            user_id=user_id,
        )

        # ── Process request ────────────────────────────────────────────────────
        response = await call_next(request)

        # ── Log request completion ─────────────────────────────────────────────
        # Re-read user_id — AuthGuard may have set it during this request
        user_id = getattr(request.state, "clerk_user_id", user_id)

        request_logger.log_request_end(
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            start_time=start_time,
            trace_id=trace_id,
            user_id=user_id,
        )

        # ── Add trace ID to response headers ──────────────────────────────────
        # Why: Frontend can read X-Trace-ID from the response and send it
        # back to support when reporting a bug. Support can grep that ID
        # in logs to find the exact request.
        response.headers["X-Trace-ID"] = trace_id

        return response

    def _get_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"