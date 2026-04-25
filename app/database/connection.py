"""
app/database/connection.py — Database Layer

WHY THIS FILE EXISTS:
  This file has one job: manage the database connection lifecycle.
  Nothing else belongs here — no business logic, no routing, no schemas.

CONCEPTS FROM VIDEO 12 (Mastering Databases):
  - We use async SQLAlchemy (asyncpg driver) so DB queries never block the
    event loop. A blocking DB call would freeze ALL concurrent requests.
  - Connection pooling: instead of opening a new TCP connection per request
    (expensive), SQLAlchemy maintains a pool of reusable connections.
  - get_db() is a FastAPI dependency — it hands a session to the route,
    and guarantees the session is closed after the request (even on error).

CONNECTION POOL EXPLAINED (from Video 21 — Scaling):
  DB_POOL_SIZE=5 means: maintain 5 persistent connections.
  DB_MAX_OVERFLOW=10 means: allow 10 extra connections during traffic spikes.
  So max simultaneous DB connections = 5 + 10 = 15.
  Neon free tier supports ~20 connections. Never exceed that.

CLERK AUTH FLOW NOTE:
  The database layer knows NOTHING about auth. Auth validation happens in
  middleware (app/middleware/auth_guard.py) BEFORE the request reaches any
  router. By the time get_db() is called, the user is already authenticated.
"""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase
from typing import AsyncGenerator
from app.config import settings


# ── Declarative Base ──────────────────────────────────────────────────────────
# All SQLAlchemy ORM models inherit from this Base.
# It holds the metadata (table definitions) used by Alembic for migrations.
class Base(DeclarativeBase):
    """
    Base class for all ORM models.

    Why a custom Base class instead of declarative_base():
    SQLAlchemy 2.0 recommends this pattern. It allows us to add shared
    columns (like created_at, updated_at) to the Base later if needed.
    """
    pass


# ── Async Engine ──────────────────────────────────────────────────────────────
# The engine is the core connection pool. Created ONCE at module import time.
# It is reused across ALL requests — not recreated per request.
engine: AsyncEngine = create_async_engine(
    url=settings.DATABASE_URL,

    # Why pool_size and max_overflow: See module docstring above.
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,

    # Timeout before raising "could not get a connection from the pool"
    pool_timeout=settings.DB_POOL_TIMEOUT,

    # Why pool_pre_ping=True: Before handing a connection from the pool to a
    # request, SQLAlchemy sends a lightweight "SELECT 1" to check if the
    # connection is still alive. Neon serverless connections can go stale.
    # Without this, you'd get cryptic "connection lost" errors mid-request.
    pool_pre_ping=True,

    # echo=True logs every SQL query — useful in development, NEVER in production
    # (would log sensitive user data and flood logs)
    echo=settings.is_development,
)


# ── Session Factory ───────────────────────────────────────────────────────────
# async_sessionmaker creates new AsyncSession objects from the engine.
# Think of it as a factory — the engine is the connection pool,
# the session is a single "unit of work" (one request's DB scope).
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,

    # Why expire_on_commit=False:
    # After a session.commit(), SQLAlchemy by default marks all ORM objects
    # as "expired" — the next attribute access would trigger a new SELECT.
    # In async code, this causes "MissingGreenlet" errors because the lazy
    # load happens outside the async context.
    # Setting this to False means we can safely return ORM objects after commit.
    expire_on_commit=False,
)


# ── FastAPI Dependency ────────────────────────────────────────────────────────
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that provides a database session per request.

    HOW IT WORKS (Video 10 — Middlewares & Request Context):
      1. FastAPI calls this generator before the route handler
      2. `yield session` hands the session to the route
      3. After the route returns (or raises), execution resumes after yield
      4. The session is closed in the finally block — guaranteed cleanup

    USAGE IN ROUTERS:
      from app.database.connection import get_db
      from sqlalchemy.ext.asyncio import AsyncSession

      @router.get("/expenses")
      async def get_expenses(db: AsyncSession = Depends(get_db)):
          ...

    WHY FINALLY: If the route raises an HTTPException or any other error,
    we still MUST close the session to return it to the pool.
    Without finally, a crashed route would leak DB connections.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            # Commit is NOT done here — services call session.commit() explicitly.
            # This gives services control over transaction boundaries.
        except Exception:
            # If anything goes wrong after yield, roll back the transaction.
            # This prevents partial writes — either all changes commit or none do.
            await session.rollback()
            raise
        finally:
            # Always close — returns the connection to the pool.
            await session.close()