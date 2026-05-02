"""
app/services/notification_service.py — Notification Service

RESPONSIBILITIES:
  list_notifications   — paginated list, filter by read/unread
  mark_read            — mark single notification as read
  mark_all_read        — mark all as read
  check_all_alerts     — run all alert checks (budget, FD maturity, min balance)
  get_unread_count     — cached count for bell badge

ALERT CHECKS (Doc4 — Section 5.5):
  Called on every dashboard load via GET /notifications/check.
  Three checks run:
    1. Budget alerts   — any budget at or above alert_threshold_percent?
    2. FD maturity     — any FD maturing in the next 30 days?
    3. Min balance     — any account balance near minimum?

  All checks are IDEMPOTENT — they check for recent duplicates before
  inserting to avoid spamming the user with repeated alerts.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import redis_client
from app.models.account import Account, FixedDeposit
from app.models.budget import Budget
from app.models.category import Category
from app.models.expense import Expense
from app.models.notification import Notification
from app.utils.formatting import get_month_range
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

_UNREAD_CACHE_TTL = 60 * 10  # 10 minutes


async def list_notifications(
    db: AsyncSession,
    user_id: uuid.UUID,
    is_read: bool | None = None,
    page: int = 1,
    limit: int = 20,
) -> tuple[list[Notification], int]:
    from app.utils.formatting import calculate_offset

    conditions = [Notification.user_id == user_id]
    if is_read is not None:
        conditions.append(Notification.is_read == is_read)

    total = await db.scalar(select(func.count(Notification.id)).where(and_(*conditions)))
    offset = calculate_offset(page, limit)
    rows = await db.execute(
        select(Notification)
        .where(and_(*conditions))
        .order_by(Notification.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), total or 0


async def get_unread_count(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """Cached unread notification count for bell badge."""
    cache_key = f"financeflow:notifications:{user_id}:unread_count"
    cached = await redis_client.get_json(cache_key)
    if cached is not None:
        return int(cached)

    count = await db.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        )
    )
    count = count or 0
    await redis_client.set_json(cache_key, count, ttl_seconds=_UNREAD_CACHE_TTL)
    return count


async def mark_read(
    db: AsyncSession,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Notification:
    from app.utils.exceptions import ResourceNotFoundException, UnauthorizedAccessException

    result = await db.execute(select(Notification).where(Notification.id == notification_id))
    notif = result.scalar_one_or_none()
    if not notif:
        raise ResourceNotFoundException("Notification", str(notification_id))
    if notif.user_id != user_id:
        raise UnauthorizedAccessException()

    notif.is_read = True
    notif.read_at = datetime.now(UTC)
    await db.commit()
    await _invalidate_unread_cache(str(user_id))
    return notif


async def mark_all_read(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """Mark all unread notifications as read. Returns count of updated rows."""
    result = await db.execute(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        )
    )
    notifications = result.scalars().all()
    now = datetime.now(UTC)
    count = 0
    for n in notifications:
        n.is_read = True
        n.read_at = now
        count += 1

    await db.commit()
    await _invalidate_unread_cache(str(user_id))
    return count


async def check_all_alerts(
    db: AsyncSession,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> dict:
    """
    Run all alert checks. Called on dashboard load.
    Returns summary of checks run and notifications created.
    """
    checks_run = 0
    created = 0

    # 1. Budget alerts
    n = await _check_budget_alerts(db, user_id, trace_id)
    checks_run += 1
    created += n

    # 2. FD maturity alerts
    n = await _check_fd_maturity(db, user_id, trace_id)
    checks_run += 1
    created += n

    # 3. Min balance alerts
    n = await _check_min_balance(db, user_id, trace_id)
    checks_run += 1
    created += n

    if created > 0:
        await _invalidate_unread_cache(str(user_id))

    log_event(
        logger,
        "alert_checks_run",
        trace_id=trace_id,
        user_id=str(user_id),
        checks_run=checks_run,
        notifications_created=created,
    )

    return {"checks_run": checks_run, "notifications_created": created}


# ── Internal alert checks ──────────────────────────────────────────────────────


async def _check_budget_alerts(
    db: AsyncSession,
    user_id: uuid.UUID,
    trace_id: str,
) -> int:
    """Check all active budgets. Create WARNING or EXCEEDED notifications."""
    today = date.today()
    start, end = get_month_range(today.year, today.month)
    start_date, end_date = start.date(), end.date()

    budgets = await db.execute(
        select(Budget, Category)
        .join(Category, Category.id == Budget.category_id)
        .where(Budget.user_id == user_id, Budget.is_active.is_(True))
    )
    created = 0
    for budget, category in budgets.all():
        spent_result = await db.scalar(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.user_id == user_id,
                Expense.category_id == budget.category_id,
                Expense.is_deleted.is_(False),
                Expense.expense_date >= start_date,
                Expense.expense_date <= end_date,
            )
        )
        spent = float(spent_result or 0)
        pct = spent / float(budget.limit_amount) * 100 if budget.limit_amount > 0 else 0

        if pct < budget.alert_threshold_percent:
            continue

        notif_type = "BUDGET_EXCEEDED" if spent >= float(budget.limit_amount) else "BUDGET_ALERT"

        # Idempotency: skip if same alert exists in last 24hrs
        if await _recent_duplicate_exists(db, user_id, notif_type, str(budget.id)):
            continue

        notification = Notification(
            user_id=user_id,
            notification_type=notif_type,
            title=f"{'Budget Exceeded' if notif_type == 'BUDGET_EXCEEDED' else 'Budget Alert'}: {category.name}",
            message=f"You've used {pct:.0f}% of your {category.name} budget this month.",
            metadata_={
                "budget_id": str(budget.id),
                "category_name": category.name,
                "pct_used": round(pct, 1),
                "limit": str(budget.limit_amount),
                "spent": str(spent),
            },
        )
        db.add(notification)
        created += 1

    if created:
        await db.commit()
    return created


async def _check_fd_maturity(
    db: AsyncSession,
    user_id: uuid.UUID,
    trace_id: str,
) -> int:
    """Create alerts for FDs maturing in the next 30 days."""
    from app.models.enums import FDStatus

    today = date.today()
    alert_window = today + timedelta(days=30)

    fds = await db.execute(
        select(FixedDeposit).where(
            FixedDeposit.user_id == user_id,
            FixedDeposit.status == FDStatus.ACTIVE,
            FixedDeposit.maturity_date <= alert_window,
            FixedDeposit.maturity_date >= today,
        )
    )
    created = 0
    for fd in fds.scalars().all():
        if await _recent_duplicate_exists(db, user_id, "FD_MATURITY", str(fd.id)):
            continue

        days_left = (fd.maturity_date - today).days
        notification = Notification(
            user_id=user_id,
            notification_type="FD_MATURITY",
            title=f"FD Matures in {days_left} days",
            message=(
                f"Your FD of ₹{fd.principal_amount} matures on "
                f"{fd.maturity_date.strftime('%d %b %Y')}. "
                f"Expected amount: ₹{fd.maturity_amount}."
            ),
            metadata_={
                "fd_id": str(fd.id),
                "account_id": str(fd.account_id),
                "maturity_date": fd.maturity_date.isoformat(),
                "days_left": days_left,
                "maturity_amount": str(fd.maturity_amount),
            },
        )
        db.add(notification)
        created += 1

    if created:
        await db.commit()
    return created


async def _check_min_balance(
    db: AsyncSession,
    user_id: uuid.UUID,
    trace_id: str,
) -> int:
    """Alert when account balance is within 20% of minimum balance."""
    accounts = await db.execute(
        select(Account).where(
            Account.user_id == user_id,
            Account.is_active.is_(True),
            Account.min_balance.isnot(None),
            Account.min_balance > 0,
        )
    )
    created = 0
    for account in accounts.scalars().all():
        threshold = float(account.min_balance or 0) * 1.2  # 20% above min
        if float(account.current_balance) > threshold:
            continue

        if await _recent_duplicate_exists(db, user_id, "MIN_BALANCE", str(account.id)):
            continue

        notification = Notification(
            user_id=user_id,
            notification_type="MIN_BALANCE",
            title=f"Low Balance: {account.account_name}",
            message=(
                f"{account.account_name} balance (₹{account.current_balance}) "
                f"is near the minimum required (₹{account.min_balance})."
            ),
            metadata_={
                "account_id": str(account.id),
                "bank_name": account.bank_name,
                "current_balance": str(account.current_balance),
                "min_balance": str(account.min_balance),
            },
        )
        db.add(notification)
        created += 1

    if created:
        await db.commit()
    return created


async def _recent_duplicate_exists(
    db: AsyncSession,
    user_id: uuid.UUID,
    notif_type: str,
    resource_id: str,
) -> bool:
    """True if same alert was created for this resource in the last 24 hours."""
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    result = await db.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.notification_type == notif_type,
            Notification.created_at >= cutoff,
        )
    )
    return (result or 0) > 0


async def _invalidate_unread_cache(user_id: str) -> None:
    cache_key = f"financeflow:notifications:{user_id}:unread_count"
    await redis_client.delete(cache_key)