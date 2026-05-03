"""
app/services/income_service.py — Income Service

RESPONSIBILITIES:
  create_income — insert income row, invalidate caches
  list_incomes  — filtered paginated list
  get_income    — single income with ownership check
  update_income — partial update
  delete_income — soft delete + Cloudinary screenshot cleanup
  get_summary   — monthly income breakdown by source (cached)
"""

import uuid
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.keys import TTL_MONTHLY_SUMMARY, CacheKeys
from app.cache.redis_client import redis_client
from app.models.income import Income
from app.schemas.income_schema import IncomeCreate, IncomeFilter, IncomeUpdate
from app.utils.exceptions import (
    ResourceNotFoundException,
    UnauthorizedAccessException,
)
from app.utils.formatting import calculate_offset, get_month_range
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


async def create_income(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: IncomeCreate,
    trace_id: str = "no-trace",
) -> Income:
    income = Income(
        user_id=user_id,
        amount=data.amount,
        source=data.source,
        income_date=data.income_date,
        sender_name=data.sender_name,
        description=data.description,
        notes=data.notes,
        account_id=data.account_id,
        payment_mode=data.payment_mode,
        screenshot_url=data.screenshot_url,
        screenshot_public_id=data.screenshot_public_id,
        is_recurring=data.is_recurring,
        due_id=data.due_id,
    )
    db.add(income)
    await db.commit()
    await db.refresh(income)

    await _invalidate_income_caches(str(user_id), data.income_date.year, data.income_date.month)

    log_event(
        logger,
        "income_created",
        trace_id=trace_id,
        user_id=str(user_id),
        income_id=str(income.id),
        amount=str(data.amount),
        source=data.source.value,
    )
    return income


async def list_incomes(
    db: AsyncSession,
    user_id: uuid.UUID,
    filters: IncomeFilter,
) -> tuple[list[Income], int]:
    conditions = [
        Income.user_id == user_id,
        Income.is_deleted.is_(False),
    ]
    if filters.from_date:
        conditions.append(Income.income_date >= filters.from_date)
    if filters.to_date:
        conditions.append(Income.income_date <= filters.to_date)
    if filters.source:
        conditions.append(Income.source == filters.source)
    if filters.account_id:
        conditions.append(Income.account_id == filters.account_id)
    if filters.search:
        conditions.append(Income.description.ilike(f"%{filters.search}%"))

    where_clause = and_(*conditions)

    count_result = await db.execute(select(func.count(Income.id)).where(where_clause))
    total = count_result.scalar()

    offset = calculate_offset(filters.page, filters.limit)
    rows = await db.execute(
        select(Income)
        .where(where_clause)
        .order_by(Income.income_date.desc(), Income.created_at.desc())
        .offset(offset)
        .limit(filters.limit)
    )
    return list(rows.scalars().all()), total or 0


async def get_income(
    db: AsyncSession,
    income_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Income:
    result = await db.execute(
        select(Income).where(Income.id == income_id, Income.is_deleted.is_(False))
    )
    income = result.scalar_one_or_none()
    if not income:
        raise ResourceNotFoundException("Income", str(income_id))
    if income.user_id != user_id:
        raise UnauthorizedAccessException()
    return income


async def update_income(
    db: AsyncSession,
    income_id: uuid.UUID,
    user_id: uuid.UUID,
    data: IncomeUpdate,
    trace_id: str = "no-trace",
) -> Income:
    income = await get_income(db, income_id, user_id)
    old_date = income.income_date

    if data.amount is not None:
        income.amount = data.amount
    if data.source is not None:
        income.source = data.source
    if data.income_date is not None:
        income.income_date = data.income_date
    if data.sender_name is not None:
        income.sender_name = data.sender_name
    if data.description is not None:
        income.description = data.description
    if data.notes is not None:
        income.notes = data.notes
    if data.account_id is not None:
        income.account_id = data.account_id
    if data.payment_mode is not None:
        income.payment_mode = data.payment_mode
    if data.screenshot_url is not None:
        income.screenshot_url = data.screenshot_url
    if data.is_recurring is not None:
        income.is_recurring = data.is_recurring

    await db.commit()
    await db.refresh(income)

    await _invalidate_income_caches(str(user_id), old_date.year, old_date.month)
    if data.income_date and data.income_date != old_date:
        await _invalidate_income_caches(str(user_id), data.income_date.year, data.income_date.month)

    log_event(
        logger, "income_updated", trace_id=trace_id, user_id=str(user_id), income_id=str(income_id)
    )
    return income


async def delete_income(
    db: AsyncSession,
    income_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> None:
    income = await get_income(db, income_id, user_id)

    if income.screenshot_public_id:
        try:
            import cloudinary.uploader

            cloudinary.uploader.destroy(income.screenshot_public_id)
        except Exception as e:
            logger.warning(
                f"Cloudinary delete failed for income screenshot: {e}",
                extra={"trace_id": trace_id},
            )

    income.is_deleted = True
    await db.commit()
    await _invalidate_income_caches(str(user_id), income.income_date.year, income.income_date.month)

    log_event(
        logger, "income_deleted", trace_id=trace_id, user_id=str(user_id), income_id=str(income_id)
    )


async def get_income_summary(
    db: AsyncSession,
    user_id: uuid.UUID,
    year: int,
    month: int,
) -> dict:
    """Monthly income summary by source. Cached 10 minutes."""
    cache_key = f"financeflow:incomes:{user_id}:summary:{year}:{month:02d}"
    cached = await redis_client.get_json(cache_key)
    if cached:
        return cached

    start, end = get_month_range(year, month)
    start_date, end_date = start.date(), end.date()

    rows = await db.execute(
        select(
            Income.source,
            func.coalesce(func.sum(Income.amount), 0).label("total"),
            func.count(Income.id).label("count"),
        )
        .where(
            Income.user_id == user_id,
            Income.is_deleted.is_(False),
            Income.income_date >= start_date,
            Income.income_date <= end_date,
        )
        .group_by(Income.source)
        .order_by(func.sum(Income.amount).desc())
    )
    source_rows = rows.all()

    grand_total = sum(Decimal(str(r.total)) for r in source_rows)
    by_source = [
        {
            "source": r.source.value,
            "total_amount": str(r.total),
            "transaction_count": r.count,
            "pct_of_total": round(float(r.total) / float(grand_total) * 100, 2)
            if grand_total > 0
            else 0.0,
        }
        for r in source_rows
    ]

    result = {
        "year": year,
        "month": month,
        "total_amount": str(grand_total),
        "transaction_count": sum(row.count for row in source_rows),  # type: ignore[misc]
        "by_source": by_source,
    }
    await redis_client.set_json(cache_key, result, ttl_seconds=TTL_MONTHLY_SUMMARY)
    return result


async def _invalidate_income_caches(user_id: str, year: int, month: int) -> None:
    await redis_client.delete(
        f"financeflow:incomes:{user_id}:summary:{year}:{month:02d}",
        CacheKeys.analytics_monthly(user_id, year, month),
    )
    try:
        pattern = CacheKeys.income_list_pattern(user_id)
        keys = await redis_client.client.keys(pattern)
        if keys:
            await redis_client.client.delete(*keys)
    except Exception as e:
        logger.warning(f"Income list cache pattern delete failed: {e}")