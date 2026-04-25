"""
tests/fixtures/conftest.py — Shared Pytest Fixtures

WHY CONFTEST.PY:
  pytest auto-discovers conftest.py files and makes their fixtures
  available to all tests in the same directory and below.
  This file lives in tests/fixtures/ — importable from both unit/ and integration/.

FIXTURE STRATEGY:
  - Unit tests: mock DB and Redis entirely (no real connections)
  - Integration tests: use real test DB (separate Neon branch or local Postgres)

  Why mock for unit tests (Video 22 — Testing philosophy from video 2):
    Unit tests must be fast (< 1 second each) and runnable offline.
    Real DB connections are slow (network round trip) and require setup.
    We test business logic in isolation — not the DB driver.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.config import settings


# ── Test Client Fixture ────────────────────────────────────────────────────────

@pytest.fixture
async def client() -> AsyncClient:
    """
    Async HTTP client for endpoint testing.

    Uses httpx.AsyncClient with ASGITransport — makes real HTTP requests
    to our FastAPI app WITHOUT starting a real server.

    Usage in tests:
        async def test_health(client):
            response = await client.get("/api/v1/health")
            assert response.status_code == 200
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Mock DB Session Fixture ────────────────────────────────────────────────────

@pytest.fixture
def mock_db() -> AsyncMock:
    """
    Mock SQLAlchemy AsyncSession for unit testing services.

    Returns an AsyncMock that mimics AsyncSession's interface.
    Services receive this mock instead of a real DB session.

    Usage in tests:
        async def test_create_expense(mock_db):
            mock_db.execute.return_value.scalar_one_or_none.return_value = None
            result = await expense_service.create(mock_db, payload)
            mock_db.add.assert_called_once()
    """
    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.add = MagicMock()       # add() is sync in SQLAlchemy
    session.delete = MagicMock()    # delete() is sync
    session.execute = AsyncMock()
    session.refresh = AsyncMock()
    return session


# ── Mock Redis Fixture ─────────────────────────────────────────────────────────

@pytest.fixture
def mock_redis():
    """
    Mock RedisClient for unit testing services.

    Patches redis_client so no real Redis connection is needed.
    All cache operations are no-ops by default.

    Usage in tests:
        async def test_get_expenses_cache_miss(mock_redis):
            mock_redis.get_json.return_value = None   # simulate cache miss
            result = await expense_service.list_expenses(user_id="abc")
            mock_redis.set_json.assert_called_once()  # verify cache was populated
    """
    with patch("app.cache.redis_client.redis_client") as mock:
        mock.get_json = AsyncMock(return_value=None)    # cache miss by default
        mock.set_json = AsyncMock()
        mock.delete = AsyncMock()
        mock.exists = AsyncMock(return_value=False)
        mock.increment = AsyncMock(return_value=1)
        yield mock


# ── Authenticated Request Fixture ──────────────────────────────────────────────

@pytest.fixture
def auth_headers() -> dict:
    """
    Returns fake Authorization headers that bypass auth middleware in tests.

    In tests, we patch the auth middleware to inject a test user.
    These headers are passed to the test client to simulate an authenticated user.

    Note: The actual JWT validation is mocked — we're testing business logic,
    not Clerk's JWT library.
    """
    return {"Authorization": "Bearer test_token_for_pytest"}


@pytest.fixture
def mock_auth_middleware():
    """
    Patches the auth guard to inject a known test user into request.state.

    Why: Unit tests should not call Clerk's JWKS endpoint.
    We mock auth to always succeed with a known test user.

    Usage:
        async def test_create_expense(client, mock_auth_middleware):
            response = await client.post("/api/v1/expenses", ...)
    """
    with patch(
        "app.middleware.auth_guard.AuthGuardMiddleware.dispatch",
    ) as mock_dispatch:
        async def fake_dispatch(self, request, call_next):
            # Inject a fake authenticated user into request.state
            request.state.clerk_user_id = "user_test_abc123"
            request.state.user_email = "test@financeflow.app"
            return await call_next(request)

        mock_dispatch.side_effect = fake_dispatch
        yield mock_dispatch


# ── Sample Data Factories ──────────────────────────────────────────────────────
# Why factories not fixtures: factories return NEW objects each call,
# preventing test state from leaking between tests.

def make_expense_payload(**overrides) -> dict:
    """
    Returns a valid expense creation payload dict.
    Override any field with keyword arguments.

    Usage:
        payload = make_expense_payload(amount="5000.00", notes="Test")
    """
    base = {
        "amount": "150.00",
        "category": "FOOD",
        "payment_mode": "UPI",
        "notes": "Test expense",
        "expense_date": "2024-01-15",
    }
    return {**base, **overrides}