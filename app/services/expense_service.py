"""
app/services/expense_service.py — Expense Service

RESPONSIBILITIES:
  create_expense  — validate → budget check → insert → cache invalidate
  list_expenses   — filtered paginated list with total sum
  get_expense     — single expense with ownership check (BOLA protection)
  update_expense  — partial update, re-runs budget check if amount changed
  delete_expense  — soft delete + Cloudinary receipt cleanup
  get_monthly_summary — cached aggregate for dashboard

BUDGET CHECK FLOW (Doc4 — Section 4.2 & 10.1):
  On every POST /expenses:
    1. Find active budget for this category + current month
    2. SUM all expenses this month for this category (real-time, not cached)
    3. projected = current_spent + new_amount
    4. projected > limit          → raise BudgetExceededException (expense NOT saved)
    5. projected >= alert_threshold → create notification (expense IS saved)

CACHE STRATEGY (Doc4 — Section 11.1):
  Invalidated on any create/update/delete:
    - expense list pages      (ff:expenses:{uid}:list:*)
    - monthly summary         (ff:analytics:{uid}:monthly:{y}:{m})
    - dashboard KPIs          (ff:analytics:{uid}:dashboard:{y}:{m})
    - budget status           (ff:budget:{uid}:overview)
"""

import uuid
from datetime import date
from decimal import Decimal
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.expense import Expense
from app.models.budget import Budget
from app.models.category import Category, Subcategory
from app.models.notification import Notification
from app.schemas.expense_schema import ExpenseCreate, ExpenseUpdate, ExpenseFilter
from app.cache.redis_client import redis_client
from app.cache.keys import CacheKeys, TTL_ANALYTICS, TTL_MONTHLY_SUMMARY
from app.utils.exceptions import (
    ResourceNotFoundException,
    UnauthorizedAccessException,
    BudgetExceededException,
    ValidationException,
)
from app.utils.formatting import calculate_offset
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


# ── Create ─────────────────────────────────────────────────────────────────────

async def create_expense(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: ExpenseCreate,
    trace_id: str = "no-trace",
) -> Expense:
    """
    Full expense creation with budget check and cache invalidation.

    Steps:
      1. Verify category ownership
      2. Verify account ownership (if non-CASH)
      3. Check active budget — raise if exceeded, notify if at threshold
      4. INSERT expense row
      5. Invalidate all related caches
      6. Return fresh expense with relationships loaded
    """
    # ── Step 1: Verify category ────────────────────────────────────────────────
    cat_result = await db.execute(
        select(Category).where(
            Category.id == data.category_id,
            Category.is_active.is_(True),
        )
    )
    category = cat_result.scalar_one_or_none()
    if not category:
        raise ResourceNotFoundException("Category", str(data.category_id))

    # Category must belong to user OR be a system category (user_id IS NULL)
    if category.user_id is not None and category.user_id != user_id:
        raise UnauthorizedAccessException()

    # ── Step 2: Budget check ───────────────────────────────────────────────────
    await _check_and_handle_budget(
        db=db,
        user_id=user_id,
        category=category,
        new_amount=data.amount,
        expense_date=data.expense_date,
        trace_id=trace_id,
    )

    # ── Step 3: INSERT ─────────────────────────────────────────────────────────
    expense = Expense(
        user_id=user_id,
        category_id=data.category_id,
        subcategory_id=data.subcategory_id,
        account_id=data.account_id,
        amount=data.amount,
        expense_date=data.expense_date,
        description=data.description,
        notes=data.notes,
        payment_mode=data.payment_mode,
        receipt_url=data.receipt_url,
        receipt_public_id=data.receipt_public_id,
        is_recurring=data.is_recurring,
        recurrence_period=data.recurrence_period,
    )
    db.add(expense)
    await db.commit()
    await db.refresh(expense)

    # ── Step 4: Reload with relationships ──────────────────────────────────────
    expense = await _load_expense_with_relations(db, expense.id)

    # ── Step 5: Invalidate caches ──────────────────────────────────────────────
    await _invalidate_expense_caches(
        user_id=str(user_id),
        year=data.expense_date.year,
        month=data.expense_date.month,
    )

    log_event(logger, "expense_created",
              trace_id=trace_id,
              user_id=str(user_id),
              expense_id=str(expense.id),
              amount=str(data.amount),
              category=category.name,
              payment_mode=data.payment_mode.value)
    return expense


# ── List ───────────────────────────────────────────────────────────────────────

async def list_expenses(
    db: AsyncSession,
    user_id: uuid.UUID,
    filters: ExpenseFilter,
    trace_id: str = "no-trace",
) -> tuple[list[Expense], int, Decimal]:
    """
    Paginated expense list with optional filters.

    Returns: (expenses, total_count, total_amount_sum)
    total_amount is the sum of ALL matching expenses (not just this page).
    Frontend uses this for the 'Total: ₹X' summary strip.
    """
    base_conditions = [
        Expense.user_id == user_id,
        Expense.is_deleted.is_(False),
    ]

    if filters.from_date:
        base_conditions.append(Expense.expense_date >= filters.from_date)
    if filters.to_date:
        base_conditions.append(Expense.expense_date <= filters.to_date)
    if filters.category_id:
        base_conditions.append(Expense.category_id == filters.category_id)
    if filters.subcategory_id:
        base_conditions.append(Expense.subcategory_id == filters.subcategory_id)
    if filters.payment_mode:
        base_conditions.append(Expense.payment_mode == filters.payment_mode)
    if filters.account_id:
        base_conditions.append(Expense.account_id == filters.account_id)
    if filters.search:
        base_conditions.append(
            Expense.description.ilike(f"%{filters.search}%")
        )
    if filters.min_amount is not None:
        base_conditions.append(Expense.amount >= filters.min_amount)
    if filters.max_amount is not None:
        base_conditions.append(Expense.amount <= filters.max_amount)
    if filters.is_recurring is not None:
        base_conditions.append(Expense.is_recurring == filters.is_recurring)

    where_clause = and_(*base_conditions)

    # Total count + sum (for summary strip)
    agg_result = await db.execute(
        select(func.count(Expense.id), func.coalesce(func.sum(Expense.amount), 0))
        .where(where_clause)
    )
    total_count, total_amount = agg_result.one()

    # Paginated rows
    offset = calculate_offset(filters.page, filters.limit)
    rows_result = await db.execute(
        select(Expense)
        .where(where_clause)
        .options(
            selectinload(Expense.category),
            selectinload(Expense.subcategory),
        )
        .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
        .offset(offset)
        .limit(filters.limit)
    )
    expenses = list(rows_result.scalars().all())
    return expenses, total_count, Decimal(str(total_amount))


# ── Get single ─────────────────────────────────────────────────────────────────

async def get_expense(
    db: AsyncSession,
    expense_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Expense:
    """
    Fetch a single expense. Raises 403 if it exists but belongs to another user.
    This is BOLA (Broken Object Level Authorization) protection.
    """
    expense = await _load_expense_with_relations(db, expense_id)
    if not expense:
        raise ResourceNotFoundException("Expense", str(expense_id))
    if expense.user_id != user_id:
        raise UnauthorizedAccessException()
    return expense


# ── Update ─────────────────────────────────────────────────────────────────────

async def update_expense(
    db: AsyncSession,
    expense_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ExpenseUpdate,
    trace_id: str = "no-trace",
) -> Expense:
    """
    Partial update. Re-runs budget check if amount or category changed.
    """
    expense = await get_expense(db, expense_id, user_id)
    old_date = expense.expense_date

    # Re-run budget check only if amount or category changed
    amount_changed = data.amount is not None and data.amount != expense.amount
    category_changed = data.category_id is not None and data.category_id != expense.category_id

    if amount_changed or category_changed:
        new_amount = data.amount if data.amount is not None else expense.amount
        new_category_id = data.category_id if data.category_id is not None else expense.category_id
        new_date = data.expense_date if data.expense_date is not None else expense.expense_date

        cat_result = await db.execute(
            select(Category).where(Category.id == new_category_id)
        )
        category = cat_result.scalar_one_or_none()
        if not category:
            raise ResourceNotFoundException("Category", str(new_category_id))

        # Exclude current expense from the SUM (it's being updated, not added)
        await _check_and_handle_budget(
            db=db,
            user_id=user_id,
            category=category,
            new_amount=new_amount,
            expense_date=new_date,
            exclude_expense_id=expense_id,
            trace_id=trace_id,
        )

    # Apply updates
    if data.amount is not None:
        expense.amount = data.amount
    if data.category_id is not None:
        expense.category_id = data.category_id
    if data.subcategory_id is not None:
        expense.subcategory_id = data.subcategory_id
    if data.payment_mode is not None:
        expense.payment_mode = data.payment_mode
    if data.account_id is not None:
        expense.account_id = data.account_id
    if data.expense_date is not None:
        expense.expense_date = data.expense_date
    if data.description is not None:
        expense.description = data.description
    if data.notes is not None:
        expense.notes = data.notes
    if data.receipt_url is not None:
        expense.receipt_url = data.receipt_url
    if data.receipt_public_id is not None:
        expense.receipt_public_id = data.receipt_public_id
    if data.is_recurring is not None:
        expense.is_recurring = data.is_recurring
    if data.recurrence_period is not None:
        expense.recurrence_period = data.recurrence_period

    await db.commit()
    expense = await _load_expense_with_relations(db, expense_id)

    # Invalidate caches for both old and new date (if date changed)
    await _invalidate_expense_caches(str(user_id), old_date.year, old_date.month)
    if data.expense_date and data.expense_date != old_date:
        await _invalidate_expense_caches(
            str(user_id), data.expense_date.year, data.expense_date.month
        )

    log_event(logger, "expense_updated",
              trace_id=trace_id,
              user_id=str(user_id),
              expense_id=str(expense_id))
    return expense


# ── Delete ─────────────────────────────────────────────────────────────────────

async def delete_expense(
    db: AsyncSession,
    expense_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> None:
    """
    Soft-delete expense. Removes Cloudinary receipt if one exists.
    """
    expense = await get_expense(db, expense_id, user_id)

    # Delete receipt from Cloudinary if it exists
    if expense.receipt_public_id:
        await _delete_cloudinary_file(expense.receipt_public_id, trace_id)

    expense.is_deleted = True
    await db.commit()

    await _invalidate_expense_caches(
        str(user_id), expense.expense_date.year, expense.expense_date.month
    )

    log_event(logger, "expense_deleted",
              trace_id=trace_id,
              user_id=str(user_id),
              expense_id=str(expense_id),
              amount=str(expense.amount))


# ── Monthly summary ────────────────────────────────────────────────────────────

async def get_monthly_summary(
    db: AsyncSession,
    user_id: uuid.UUID,
    year: int,
    month: int,
    trace_id: str = "no-trace",
) -> dict:
    """
    Cached monthly aggregate: total, count, daily_avg, by_category breakdown.
    Cache TTL: 10 minutes.
    """
    cache_key = CacheKeys.analytics_monthly(str(user_id), year, month)
    cached = await redis_client.get_json(cache_key)
    if cached:
        log_event(logger, "monthly_summary_cache_hit",
                  trace_id=trace_id, user_id=str(user_id),
                  year=year, month=month)
        return cached

    # Compute date range for the month
    from app.utils.formatting import get_month_range
    start, end = get_month_range(year, month)
    start_date = start.date()
    end_date = end.date()

    # Total + count
    agg = await db.execute(
        select(
            func.coalesce(func.sum(Expense.amount), 0),
            func.count(Expense.id),
        ).where(
            Expense.user_id == user_id,
            Expense.is_deleted.is_(False),
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
    )
    total_amount, total_count = agg.one()
    total_amount = Decimal(str(total_amount))

    # Days in month for daily average
    import calendar
    days_in_month = calendar.monthrange(year, month)[1]
    daily_avg = total_amount / days_in_month if days_in_month > 0 else Decimal("0")

    # Category breakdown
    cat_rows = await db.execute(
        select(
            Category.id,
            Category.name,
            Category.icon,
            Category.color,
            func.coalesce(func.sum(Expense.amount), 0).label("total"),
            func.count(Expense.id).label("count"),
        )
        .join(Expense, Expense.category_id == Category.id)
        .where(
            Expense.user_id == user_id,
            Expense.is_deleted.is_(False),
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
        .group_by(Category.id, Category.name, Category.icon, Category.color)
        .order_by(func.sum(Expense.amount).desc())
    )
    rows = cat_rows.all()

    by_category = [
        {
            "category_id": str(r.id),
            "category_name": r.name,
            "icon": r.icon,
            "color": r.color,
            "total_amount": str(r.total),
            "transaction_count": r.count,
            "pct_of_total": round(
                float(r.total) / float(total_amount) * 100, 2
            ) if total_amount > 0 else 0.0,
        }
        for r in rows
    ]

    result = {
        "year": year,
        "month": month,
        "total_amount": str(total_amount),
        "transaction_count": total_count,
        "daily_average": str(daily_avg.quantize(Decimal("0.01"))),
        "by_category": by_category,
    }

    await redis_client.set_json(cache_key, result, ttl_seconds=TTL_MONTHLY_SUMMARY)
    return result


# ── Monthly spent for category (used by budget check) ─────────────────────────

async def get_monthly_spent_for_category(
    db: AsyncSession,
    user_id: uuid.UUID,
    category_id: uuid.UUID,
    year: int,
    month: int,
    exclude_expense_id: uuid.UUID | None = None,
) -> Decimal:
    """
    Returns total amount spent in this category for the given month.
    NEVER cached — must always be real-time for budget checks.
    Optionally excludes one expense_id (for update re-checks).
    """
    from app.utils.formatting import get_month_range
    start, end = get_month_range(year, month)
    start_date = start.date()
    end_date = end.date()

    conditions = [
        Expense.user_id == user_id,
        Expense.category_id == category_id,
        Expense.is_deleted.is_(False),
        Expense.expense_date >= start_date,
        Expense.expense_date <= end_date,
    ]
    if exclude_expense_id:
        conditions.append(Expense.id != exclude_expense_id)

    result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(and_(*conditions))
    )
    return Decimal(str(result.scalar()))


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _check_and_handle_budget(
    db: AsyncSession,
    user_id: uuid.UUID,
    category: Category,
    new_amount: Decimal,
    expense_date: date,
    exclude_expense_id: uuid.UUID | None = None,
    trace_id: str = "no-trace",
) -> None:
    """
    Core budget enforcement logic. Called on both create and update.

    Checks:
      1. Is there an active budget for this category + period?
      2. Will the new expense cause it to exceed the limit?
      3. Will it cross the alert threshold?
    """
    budget_result = await db.execute(
        select(Budget).where(
            Budget.user_id == user_id,
            Budget.category_id == category.id,
            Budget.is_active.is_(True),
        )
    )
    budget = budget_result.scalar_one_or_none()
    if not budget:
        return  # No budget for this category — nothing to check

    current_spent = await get_monthly_spent_for_category(
        db=db,
        user_id=user_id,
        category_id=category.id,
        year=expense_date.year,
        month=expense_date.month,
        exclude_expense_id=exclude_expense_id,
    )

    projected = current_spent + new_amount

    # Hard block: budget exceeded
    if projected > budget.limit_amount:
        log_event(logger, "budget_exceeded",
                  trace_id=trace_id,
                  level="warning",
                  user_id=str(user_id),
                  category=category.name,
                  limit=str(budget.limit_amount),
                  projected=str(projected))
        raise BudgetExceededException(
            category=category.name,
            limit=float(budget.limit_amount),
            spent=float(projected),
        )

    # Soft alert: at or above threshold
    pct_used = (float(projected) / float(budget.limit_amount)) * 100
    if pct_used >= budget.alert_threshold_percent:
        await _create_budget_alert_notification(
            db=db,
            user_id=user_id,
            category=category,
            budget=budget,
            pct_used=pct_used,
            trace_id=trace_id,
        )


async def _create_budget_alert_notification(
    db: AsyncSession,
    user_id: uuid.UUID,
    category: Category,
    budget: Budget,
    pct_used: float,
    trace_id: str = "no-trace",
) -> None:
    """
    Creates a BUDGET_ALERT notification. Idempotent — checks for a recent
    duplicate to avoid spamming the user with repeated alerts.
    """
    from datetime import datetime, timezone, timedelta

    # Check for duplicate alert in the last 24 hours
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    existing = await db.execute(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.notification_type == "BUDGET_ALERT",
            Notification.created_at >= cutoff,
            Notification.metadata_["budget_id"].astext == str(budget.id),
        )
    )
    if existing.scalar_one_or_none():
        return  # Already alerted recently — skip

    notification = Notification(
        user_id=user_id,
        notification_type="BUDGET_ALERT",
        title=f"Budget Alert: {category.name}",
        message=(
            f"You've used {pct_used:.0f}% of your "
            f"{category.name} budget this month."
        ),
        metadata_={
            "budget_id": str(budget.id),
            "category_id": str(category.id),
            "category_name": category.name,
            "pct_used": round(pct_used, 1),
            "limit": str(budget.limit_amount),
        },
    )
    db.add(notification)
    # Note: we don't commit here — this runs within the create_expense transaction
    log_event(logger, "budget_alert_created",
              trace_id=trace_id,
              user_id=str(user_id),
              category=category.name,
              pct_used=round(pct_used, 1))


async def _load_expense_with_relations(
    db: AsyncSession,
    expense_id: uuid.UUID,
) -> Expense | None:
    """Load an expense with category and subcategory eagerly loaded."""
    result = await db.execute(
        select(Expense)
        .where(Expense.id == expense_id, Expense.is_deleted.is_(False))
        .options(
            selectinload(Expense.category),
            selectinload(Expense.subcategory),
        )
    )
    return result.scalar_one_or_none()


async def _invalidate_expense_caches(
    user_id: str,
    year: int,
    month: int,
) -> None:
    """Invalidate all caches that may be stale after an expense mutation."""
    await redis_client.delete(
        CacheKeys.analytics_monthly(user_id, year, month),
        CacheKeys.analytics_category_breakdown(user_id, year, month),
        CacheKeys.budget_overview(user_id),
    )
    # Pattern delete for paginated list (all pages)
    try:
        pattern = CacheKeys.expense_list_pattern(user_id)
        keys = await redis_client.client.keys(pattern)
        if keys:
            await redis_client.client.delete(*keys)
    except Exception as e:
        logger.warning(f"Pattern cache invalidation failed: {e}")


async def _delete_cloudinary_file(public_id: str, trace_id: str) -> None:
    """
    Attempt to delete a file from Cloudinary.
    Non-fatal: if this fails we log and continue (DB delete still happens).
    """
    try:
        import cloudinary.uploader
        cloudinary.uploader.destroy(public_id)
        log_event(logger, "cloudinary_file_deleted",
                  trace_id=trace_id, public_id=public_id)
    except Exception as e:
        logger.warning(
            "Cloudinary delete failed — file may be orphaned",
            extra={"trace_id": trace_id, "public_id": public_id, "error": str(e)},
        )