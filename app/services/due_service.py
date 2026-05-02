"""
app/services/due_service.py — Dues Service

Simple bilateral debt tracking. No complex logic — just CRUD + settle.

DUE TYPES (Doc1 — Section 5.5):
  I_OWE   → "I owe Raj ₹500 for his birthday gift"   (reduces my net position)
  THEY_OWE → "Priya owes me ₹200 for lunch"          (increases my net position)

NET POSITION (for dashboard summary):
  net = sum(THEY_OWE amounts) - sum(I_OWE amounts)
  Positive = others owe you money overall.
  Negative = you owe others money overall.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.keys import CacheKeys
from app.cache.redis_client import redis_client
from app.models.due import Due
from app.schemas.group_schema import DueCreate, DueFilter, DueUpdate
from app.utils.exceptions import (
    ResourceNotFoundException,
    UnauthorizedAccessException,
)
from app.utils.formatting import calculate_offset
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


async def create_due(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: DueCreate,
    trace_id: str = "no-trace",
) -> Due:
    due = Due(
        user_id=user_id,
        due_type=data.due_type,
        person_name=data.person_name,
        person_phone=data.person_phone,
        amount=data.amount,
        description=data.description,
        due_date=data.due_date,
    )
    db.add(due)
    await db.commit()
    await db.refresh(due)
    await _invalidate_dues_cache(str(user_id))

    log_event(
        logger,
        "due_created",
        trace_id=trace_id,
        user_id=str(user_id),
        due_id=str(due.id),
        due_type=data.due_type.value,
        amount=str(data.amount),
        person=data.person_name,
    )
    return due


async def list_dues(
    db: AsyncSession,
    user_id: uuid.UUID,
    filters: DueFilter,
) -> tuple[list[Due], int]:
    conditions = [Due.user_id == user_id]

    if filters.due_type is not None:
        conditions.append(Due.due_type == filters.due_type)

    # Default: unsettled only (False means show unsettled)
    conditions.append(Due.is_settled == filters.is_settled)

    where_clause = and_(*conditions)

    count_result = await db.execute(select(func.count(Due.id)).where(where_clause))
    total = count_result.scalar()

    offset = calculate_offset(filters.page, filters.limit)
    rows = await db.execute(
        select(Due)
        .where(where_clause)
        .order_by(Due.due_date.asc().nullslast(), Due.created_at.desc())
        .offset(offset)
        .limit(filters.limit)
    )
    return list(rows.scalars().all()), total or 0


async def get_due(
    db: AsyncSession,
    due_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Due:
    result = await db.execute(select(Due).where(Due.id == due_id))
    due = result.scalar_one_or_none()
    if not due:
        raise ResourceNotFoundException("Due", str(due_id))
    if due.user_id != user_id:
        raise UnauthorizedAccessException()
    return due


async def update_due(
    db: AsyncSession,
    due_id: uuid.UUID,
    user_id: uuid.UUID,
    data: DueUpdate,
    trace_id: str = "no-trace",
) -> Due:
    due = await get_due(db, due_id, user_id)

    if data.amount is not None:
        due.amount = data.amount
    if data.description is not None:
        due.description = data.description
    if data.due_date is not None:
        due.due_date = data.due_date
    if data.person_phone is not None:
        due.person_phone = data.person_phone

    await db.commit()
    await db.refresh(due)
    await _invalidate_dues_cache(str(user_id))

    log_event(logger, "due_updated", trace_id=trace_id, user_id=str(user_id), due_id=str(due_id))
    return due


async def settle_due(
    db: AsyncSession,
    due_id: uuid.UUID,
    user_id: uuid.UUID,
    settlement_note: str | None = None,
    trace_id: str = "no-trace",
) -> Due:
    due = await get_due(db, due_id, user_id)
    due.is_settled = True
    due.settled_at = date.today()
    if settlement_note:
        due.notes = settlement_note

    await db.commit()
    await db.refresh(due)
    await _invalidate_dues_cache(str(user_id))

    log_event(
        logger,
        "due_settled",
        trace_id=trace_id,
        user_id=str(user_id),
        due_id=str(due_id),
        amount=str(due.amount),
        person=due.person_name,
    )
    return due


async def delete_due(
    db: AsyncSession,
    due_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> None:
    due = await get_due(db, due_id, user_id)
    await db.delete(due)
    await db.commit()
    await _invalidate_dues_cache(str(user_id))

    log_event(logger, "due_deleted", trace_id=trace_id, user_id=str(user_id), due_id=str(due_id))


async def get_dues_summary(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> dict:
    """
    Net due position for dashboard widget. Cached 30 minutes.
    """
    cache_key = CacheKeys.dues_summary(str(user_id))
    cached = await redis_client.get_json(cache_key)
    if cached:
        return cached

    from app.models.enums import DueType

    result = await db.execute(
        select(
            Due.due_type,
            func.coalesce(func.sum(Due.amount), 0).label("total"),
            func.count(Due.id).label("count"),
        )
        .where(Due.user_id == user_id, Due.is_settled.is_(False))
        .group_by(Due.due_type)
    )
    rows = {r.due_type: Decimal(str(r.total)) for r in result.all()}

    i_owe = rows.get(DueType.I_OWE, Decimal("0"))
    they_owe = rows.get(DueType.THEY_OWE, Decimal("0"))

    summary = {
        "i_owe_total": str(i_owe),
        "they_owe_total": str(they_owe),
        "net_position": str(they_owe - i_owe),
        "unsettled_count": sum(1 for _ in rows),
    }
    await redis_client.set_json(cache_key, summary, ttl_seconds=1800)
    return summary


async def _invalidate_dues_cache(user_id: str) -> None:
    await redis_client.delete(
        CacheKeys.dues_list(user_id),
        CacheKeys.dues_summary(user_id),
    )