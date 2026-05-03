"""
tests/conftest.py — Root Pytest Configuration

Pytest auto-discovers this file and makes ALL fixtures defined here
available to every test in the entire test suite.

WHY AT ROOT LEVEL (not in fixtures/):
  Fixtures in tests/fixtures/conftest.py are only available to tests
  in tests/fixtures/ and below. By placing this at tests/conftest.py,
  every unit test in tests/unit/ gets all fixtures automatically.

FIXTURE PHILOSOPHY (Video 2 — Test strategy):
  Unit tests mock everything external (DB, Redis, Clerk, Cloudinary).
  We test our business logic — not SQLAlchemy, not Redis, not Resend.
  A test that passes because the DB driver works correctly is not a unit test.
"""

import os
import uuid
from decimal import Decimal
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

# Set test env vars BEFORE importing anything from app
# This prevents pydantic-settings from failing on missing required vars
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "rediss://:test@localhost:6380")
os.environ.setdefault("CLERK_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("CLERK_PUBLISHABLE_KEY", "pk_test_dummy")
os.environ.setdefault("CLERK_WEBHOOK_SECRET", "whsec_dummy")
os.environ.setdefault("CORS_ORIGINS", '["http://localhost:5173"]')
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test")
os.environ.setdefault("CLOUDINARY_API_KEY", "test")
os.environ.setdefault("CLOUDINARY_API_SECRET", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("RESEND_API_KEY", "re_test")
os.environ.setdefault("RESEND_FROM_EMAIL", "test@test.com")


# ── Mock DB Session ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_db() -> AsyncMock:
    """
    Mock SQLAlchemy AsyncSession.
    All DB operations are no-ops unless the test configures return values.

    Usage:
        async def test_something(mock_db):
            mock_db.execute.return_value = make_scalar_result(some_object)
            result = await service_function(mock_db, ...)
    """
    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    session.delete = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    return session


# ── Mock Redis ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_redis():
    """
    Patches the global redis_client singleton with a mock.
    Default behavior: cache always misses (get_json returns None).

    Usage:
        async def test_cache_hit(mock_redis):
            mock_redis.get_json.return_value = {"cached": "data"}
            result = await analytics_service.get_dashboard_kpis(...)
            # Should return cached data without hitting DB
    """
    with patch("app.cache.redis_client.redis_client") as mock:
        mock.get_json = AsyncMock(return_value=None)
        mock.set_json = AsyncMock()
        mock.delete = AsyncMock()
        mock.exists = AsyncMock(return_value=False)
        mock.increment = AsyncMock(return_value=1)
        mock.client = MagicMock()
        mock.client.keys = AsyncMock(return_value=[])
        mock.client.delete = AsyncMock()
        yield mock


# ── Test User ──────────────────────────────────────────────────────────────────

@pytest.fixture
def test_user_id() -> uuid.UUID:
    """Stable UUID used as current_user.id across tests."""
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def test_category_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000010")


@pytest.fixture
def test_account_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000020")


@pytest.fixture
def test_budget_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000030")


# ── ORM Object Factories ───────────────────────────────────────────────────────

def make_mock_user(
    user_id: uuid.UUID | None = None,
    email: str = "test@financeflow.app",
) -> MagicMock:
    """Create a mock User ORM object."""
    user = MagicMock()
    user.id = user_id or uuid.UUID("00000000-0000-0000-0000-000000000001")
    user.clerk_user_id = "user_test_abc123"
    user.email = email
    user.full_name = "Test User"
    user.is_active = True
    user.is_deleted = False
    user.notification_prefs = {
        "budget_alert": True,
        "min_balance": True,
        "due_reminder": True,
    }
    return user


def make_mock_category(
    category_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    name: str = "Food",
    is_system: bool = False,
) -> MagicMock:
    """Create a mock Category ORM object."""
    cat = MagicMock()
    cat.id = category_id or uuid.UUID("00000000-0000-0000-0000-000000000010")
    cat.user_id = None if is_system else (user_id or uuid.UUID("00000000-0000-0000-0000-000000000001"))
    cat.name = name
    cat.icon = "🍛"
    cat.color = "#F04438"
    cat.is_active = True
    cat.is_system = is_system
    cat.subcategories = []
    return cat


def make_mock_expense(
    expense_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    amount: Decimal = Decimal("150.00"),
    category_name: str = "Food",
) -> MagicMock:
    """Create a mock Expense ORM object."""
    from app.models.enums import PaymentMode
    exp = MagicMock()
    exp.id = expense_id or uuid.uuid4()
    exp.user_id = user_id or uuid.UUID("00000000-0000-0000-0000-000000000001")
    exp.amount = amount
    exp.expense_date = date(2025, 1, 15)
    exp.payment_mode = PaymentMode.CASH
    exp.description = "Test expense"
    exp.is_deleted = False
    exp.is_recurring = False
    exp.receipt_public_id = None
    exp.category = make_mock_category(name=category_name)
    exp.subcategory = None
    return exp


def make_mock_budget(
    budget_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    limit_amount: Decimal = Decimal("3000.00"),
    alert_threshold_percent: int = 80,
) -> MagicMock:
    """Create a mock Budget ORM object."""
    from app.models.enums import BudgetPeriod
    b = MagicMock()
    b.id = budget_id or uuid.UUID("00000000-0000-0000-0000-000000000030")
    b.user_id = user_id or uuid.UUID("00000000-0000-0000-0000-000000000001")
    b.category_id = category_id or uuid.UUID("00000000-0000-0000-0000-000000000010")
    b.limit_amount = limit_amount
    b.alert_threshold_percent = alert_threshold_percent
    b.period = BudgetPeriod.MONTHLY
    b.is_active = True
    b.category = make_mock_category(category_id=b.category_id)
    return b


def make_scalar_result(value):
    """
    Creates a mock that mimics SQLAlchemy's scalar result.
    Used when service code calls db.execute(...).scalar_one_or_none()
    """
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar.return_value = value
    result.scalars.return_value.all.return_value = [value] if value else []
    result.one.return_value = (value, value)
    result.first.return_value = value
    return result


def make_scalar_list(items: list):
    """Creates a mock that mimics SQLAlchemy's list result."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    result.all.return_value = items
    return result