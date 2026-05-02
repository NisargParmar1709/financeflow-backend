"""
app/services/budget_service.py — Budget Service

RESPONSIBILITIES:
  create_budget     — one active budget per category per period (enforced)
  list_budgets      — all budgets with real-time status (SAFE/WARNING/EXCEEDED)
  get_budget_status — single budget with current spend computed live
  update_budget     — change limit or alert threshold
  deactivate_budget — soft-delete (is_active=False)

STATUS COMPUTATION (Doc4 — Section 10.1):
  Status is NEVER cached — must always reflect the real current spend.
  This is the only service that queries expenses synchronously on read.
  Every budget row is enriched with: spent_so_far, remaining, spent_pct, status.

UNIQUENESS RULE:
  One ACTIVE budget per user per category per period.
  Enforced by: DuplicateResourceException before DB insert.
  The DB also has a partial unique index as the final safeguard.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.budget import Budget
from app.models.category import Category
from app.models.expense import Expense
from app.schemas.budget_schema import BudgetCreate, BudgetUpdate
from app.utils.exceptions import (
    DuplicateResourceException,
    ResourceNotFoundException,
    UnauthorizedAccessException,
)
from app.utils.formatting import get_month_range
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


async def create_budget(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: BudgetCreate,
    trace_id: str = "no-trace",
) -> Budget:
    """
    Create a new budget. Enforces uniqueness: one active budget per
    user/category/period combination.
    """
    # Uniqueness check (before hitting DB constraint)
    existing = await db.execute(
        select(Budget).where(
            Budget.user_id == user_id,
            Budget.category_id == data.category_id,
            Budget.period == data.period,
            Budget.is_active.is_(True),
        )
    )
    if existing.scalar_one_or_none():
        raise DuplicateResourceException(
            "Budget",
            f"An active {data.period.value} budget for this category already exists. "
            "Deactivate it before creating a new one.",
        )

    # Verify category exists and user can access it
    cat_result = await db.execute(
        select(Category).where(
            Category.id == data.category_id,
            Category.is_active.is_(True),
        )
    )
    category = cat_result.scalar_one_or_none()
    if not category:
        raise ResourceNotFoundException("Category", str(data.category_id)) 

    budget = Budget(
        user_id=user_id,
        category_id=data.category_id,
        subcategory_id=data.subcategory_id,
        limit_amount=data.limit_amount,
        period=data.period,
        alert_threshold_percent=data.alert_threshold_percent,
        start_date=data.start_date,
        end_date=data.end_date,
    )
    db.add(budget)
    await db.commit()
    await db.refresh(budget)

    log_event(
        logger,
        "budget_created",
        trace_id=trace_id,
        user_id=str(user_id),
        budget_id=str(budget.id),
        category_id=str(data.category_id),
        limit=str(data.limit_amount),
        period=data.period.value,
    )
    return budget


async def list_budgets_with_status(
    db: AsyncSession,
    user_id: uuid.UUID,
    active_only: bool = True,
    trace_id: str = "no-trace",
) -> list[dict]:
    """
    Returns all budgets enriched with real-time spend data.
    Each budget gets: spent_so_far, remaining, spent_pct, status.
    NOT cached — must always reflect real spend.
    """
    conditions = [Budget.user_id == user_id]
    if active_only:
        conditions.append(Budget.is_active.is_(True))

    result = await db.execute(
        select(Budget)
        .where(and_(*conditions))
        .options(selectinload(Budget.category))
        .order_by(Budget.created_at.desc())
    )
    budgets = list(result.scalars().all())

    today = date.today()
    enriched = []
    for budget in budgets:
        spent = await _get_current_period_spend(db, budget, today)
        status_dict = _compute_status(budget, spent)
        enriched.append(
            {
                "id": str(budget.id),
                "category": {
                    "id": str(budget.category.id),
                    "name": budget.category.name,
                    "icon": budget.category.icon,
                    "color": budget.category.color,
                },
                "subcategory_id": str(budget.subcategory_id) if budget.subcategory_id else None,
                "limit_amount": str(budget.limit_amount),
                "period": budget.period.value,
                "alert_threshold_percent": budget.alert_threshold_percent,
                "is_active": budget.is_active,
                "start_date": budget.start_date.isoformat() if budget.start_date else None,
                "end_date": budget.end_date.isoformat() if budget.end_date else None,
                "created_at": budget.created_at.isoformat(),
                "updated_at": budget.updated_at.isoformat(),
                **status_dict,
            }
        )
    return enriched


async def get_budget(
    db: AsyncSession,
    budget_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Budget:
    """Fetch single budget with ownership check."""
    result = await db.execute(
        select(Budget).where(Budget.id == budget_id).options(selectinload(Budget.category))
    )
    budget = result.scalar_one_or_none()
    if not budget:
        raise ResourceNotFoundException("Budget", str(budget_id))
    if budget.user_id != user_id:
        raise UnauthorizedAccessException()
    return budget


async def get_budget_with_status(
    db: AsyncSession,
    budget_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict:
    """Single budget enriched with real-time status."""
    budget = await get_budget(db, budget_id, user_id)
    spent = await _get_current_period_spend(db, budget, date.today())
    status_dict = _compute_status(budget, spent)
    return {
        "id": str(budget.id),
        "category": {
            "id": str(budget.category.id),
            "name": budget.category.name,
            "icon": budget.category.icon,
            "color": budget.category.color,
        },
        "limit_amount": str(budget.limit_amount),
        "period": budget.period.value,
        "alert_threshold_percent": budget.alert_threshold_percent,
        "is_active": budget.is_active,
        **status_dict,
    }


async def update_budget(
    db: AsyncSession,
    budget_id: uuid.UUID,
    user_id: uuid.UUID,
    data: BudgetUpdate,
    trace_id: str = "no-trace",
) -> Budget:
    budget = await get_budget(db, budget_id, user_id)

    if data.limit_amount is not None:
        budget.limit_amount = data.limit_amount
    if data.alert_threshold_percent is not None:
        budget.alert_threshold_percent = data.alert_threshold_percent
    if data.is_active is not None:
        budget.is_active = data.is_active
    if data.end_date is not None:
        budget.end_date = data.end_date

    await db.commit()
    await db.refresh(budget)

    log_event(
        logger, "budget_updated", trace_id=trace_id, user_id=str(user_id), budget_id=str(budget_id)
    )
    return budget


async def deactivate_budget(
    db: AsyncSession,
    budget_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> None:
    """Soft-delete: set is_active=False. Budget history preserved."""
    budget = await get_budget(db, budget_id, user_id)
    budget.is_active = False
    await db.commit()

    log_event(
        logger,
        "budget_deactivated",
        trace_id=trace_id,
        user_id=str(user_id),
        budget_id=str(budget_id),
    )


async def get_alerts_only(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[dict]:
    """Return only budgets at or above their alert threshold — for dashboard widget."""
    all_budgets = await list_budgets_with_status(db, user_id, active_only=True)
    return [b for b in all_budgets if b["status"] in ("WARNING", "EXCEEDED")]


# ── Internal helpers ───────────────────────────────────────────────────────────


async def _get_current_period_spend(
    db: AsyncSession,
    budget: Budget,
    today: date,
) -> Decimal:
    """
    SUM expenses for this budget's category in the current period.
    Always real-time — never cached.
    """
    start, end = get_month_range(today.year, today.month)
    start_date, end_date = start.date(), end.date()

    result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.user_id == budget.user_id,
            Expense.category_id == budget.category_id,
            Expense.is_deleted.is_(False),
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
    )
    return Decimal(str(result.scalar()))


def _compute_status(budget: Budget, spent: Decimal) -> dict:
    """
    Compute spent_pct and status from budget + current spend.
    Returns a dict ready to be merged into the budget response.
    """
    limit = budget.limit_amount
    remaining = max(limit - spent, Decimal("0"))
    pct = round(float(spent) / float(limit) * 100, 1) if limit > 0 else 0.0

    if spent >= limit:
        status = "EXCEEDED"
    elif pct >= budget.alert_threshold_percent:
        status = "WARNING"
    else:
        status = "SAFE"

    return {
        "spent_so_far": str(spent),
        "remaining": str(remaining),
        "spent_pct": pct,
        "status": status,
    }