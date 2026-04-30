"""
app/main.py — FastAPI Application Entry Point

WHY THIS FILE EXISTS:
  This is the application factory — it assembles all the pieces.
  Its only job is wiring: registering middleware, registering routers,
  and managing startup/shutdown lifecycle.

  No business logic. No database queries. No if/else decisions.
  Just registration and wiring.

STARTUP SEQUENCE (Video 19 — Graceful Shutdown):
  1. Validate all env vars (happens at import time via app/config.py)
  2. Connect to Redis (verify credentials at startup, not mid-request)
  3. Register exception handlers
  4. Register middleware (order matters — outermost registered last)
  5. Register all routers under /api/v1

SHUTDOWN SEQUENCE:
  1. Stop accepting new HTTP connections (Uvicorn handles this)
  2. Disconnect Redis connection pool
  (SQLAlchemy connection pool is managed by the engine — closes with process)

MIDDLEWARE ORDER (requests flow top-to-bottom, responses bottom-to-top):
  Incoming request:
    1. CORSMiddleware       → checks origin, handles preflight OPTIONS
    2. AuthGuardMiddleware  → validates JWT, injects user into request.state
    3. RateLimiterMiddleware → checks request count per IP
    4. Router handlers      → actual route logic

  Response travels back through in reverse order.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.cache.redis_client import redis_client
from app.middleware.error_handler import register_exception_handlers
from app.middleware.auth_guard import AuthGuardMiddleware
from app.middleware.rate_limiter import RateLimiterMiddleware
from app.middleware.request_logger import RequestLoggingMiddleware
from app.utils.logging import setup_logging, get_logger

# ── Router imports ─────────────────────────────────────────────────────────────
# Each router file is one resource. Import them all here.
# We import them even if they're empty stubs — ensures the module loads
# correctly and catches import errors at startup, not at first request.
# from app.routers import (
#     auth,
#     expenses,
#     incomes,
#     accounts,
#     budgets,
#     groups,
#     dues,
#     analytics,
#     ai,
#     documents,
#     notifications,
# )

# ── Logging Setup ─────────────────────────────────────────────────────────────
# Called ONCE here — configures root logger for the entire app.
# In dev: human-readable format. In production: structured JSON.
# All other modules call get_logger(__name__) — no setup needed there.
setup_logging()
logger = get_logger(__name__)


# ── Lifespan (Startup + Shutdown) ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager — replaces deprecated @app.on_event.

    Code BEFORE yield → runs on startup
    Code AFTER yield  → runs on shutdown

    Why verify connections at startup (fail-fast from Video 17):
      If REDIS_URL is wrong, better to fail NOW during deploy than to fail
      during a user's first request. Render's health check will detect the
      startup failure and won't route traffic to the broken instance.
    """
    # ── STARTUP ───────────────────────────────────────────────────────────────
    logger.info(f"Starting FinanceFlow API — env={settings.APP_ENV}")

    # Verify Redis connection (raises if credentials are wrong)
    await redis_client.connect()
    logger.info("All external connections verified. Server ready.")

    yield  # ← Server is running, handling requests

    # ── SHUTDOWN ──────────────────────────────────────────────────────────────
    logger.info("Shutting down FinanceFlow API...")

    # Drain and close Redis connection pool
    await redis_client.disconnect()

    logger.info("Graceful shutdown complete.")


# ── App Factory ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="FinanceFlow API",
    version="1.0.0",
    description=(
        "Personal Finance & Bank Account Manager for Indian students. "
        "Track expenses, income, bank accounts, budgets, dues, and groups."
    ),
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    # Why hide docs in production: Swagger UI exposes all your endpoints
    # and schemas to anyone who visits /docs. In production, only internal
    # teams should have access — use VPN or basic auth if needed.
    lifespan=lifespan,
)


# ── Exception Handlers ─────────────────────────────────────────────────────────
# Registered BEFORE middleware — handlers are matched by exception type,
# not by middleware order.
register_exception_handlers(app)


# ── Middleware Registration ────────────────────────────────────────────────────
# Starlette processes middleware in REVERSE registration order.
# Last registered = outermost = first to see the request.
#
# REQUIRED EXECUTION ORDER (request flows top → bottom):
#   1. CORSMiddleware        → handle preflight OPTIONS before anything else
#   2. RequestLoggingMiddleware → generate trace_id FIRST so every subsequent
#                                 middleware and service can include it in logs
#   3. AuthGuardMiddleware   → validate JWT, inject clerk_user_id into state
#   4. RateLimiterMiddleware → check request count (uses clerk_user_id if available)
#   5. Router handlers       → actual route logic
#
# WHY RequestLogger must be #2 (right after CORS):
#   trace_id is generated here. Auth, RateLimiter, and all services read
#   request.state.trace_id for structured logging. If RequestLogger ran
#   AFTER Auth, auth logs would have no trace_id — breaking the trace chain.
#
# Registration order is REVERSED from execution order:
#   Register RateLimiter first   → runs last (innermost)
#   Register Auth second         → runs second-to-last
#   Register RequestLogger third → runs second (right after CORS)
#   Register CORS last           → runs first (outermost)

app.add_middleware(RateLimiterMiddleware)

app.add_middleware(AuthGuardMiddleware)

app.add_middleware(RequestLoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    # "*" with credentials=True is rejected by browsers. We explicitly
    # whitelist our Vercel frontend domain only.
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,   # Required for Clerk session cookies
    allow_methods=["*"],      # GET, POST, PUT, PATCH, DELETE, OPTIONS
    allow_headers=["*"],      # Authorization, Content-Type, X-Trace-ID, etc.
)


# ── Router Registration ────────────────────────────────────────────────────────
# All routes prefixed with /api/v1 (Video 6 — Route Versioning).
# Why versioning: When we make breaking changes, /api/v2 can coexist
# with /api/v1 — old frontend versions keep working during migration.

API_V1 = "/api/v1"

# app.include_router(auth.router,          prefix=API_V1, tags=["Auth"])
# app.include_router(expenses.router,      prefix=API_V1, tags=["Expenses"])
# app.include_router(incomes.router,       prefix=API_V1, tags=["Income"])
# app.include_router(accounts.router,      prefix=API_V1, tags=["Bank Accounts"])
# app.include_router(budgets.router,       prefix=API_V1, tags=["Budgets"])
# app.include_router(groups.router,        prefix=API_V1, tags=["Groups"])
# app.include_router(dues.router,          prefix=API_V1, tags=["Dues"])
# app.include_router(analytics.router,     prefix=API_V1, tags=["Analytics"])
# app.include_router(ai.router,            prefix=API_V1, tags=["AI"])
# app.include_router(documents.router,     prefix=API_V1, tags=["Documents"])
# app.include_router(notifications.router, prefix=API_V1, tags=["Notifications"])


# ── Health Check ───────────────────────────────────────────────────────────────
@app.get("/api/v1/health", tags=["Health"], include_in_schema=False)
async def health_check() -> dict:
    """
    Health check endpoint.

    Pinged every 14 minutes by cron-job.org to prevent Render free tier
    cold starts (Render sleeps instances after 15 minutes of inactivity).

    Returns 200 if the server process is alive.
    Note: This does NOT check DB or Redis health — those are checked at
    startup. This endpoint just proves the process is running.
    """
    return {"status": "healthy", "env": settings.APP_ENV}