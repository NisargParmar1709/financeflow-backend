"""
app/services/analytics_service.py — Analytics Service

RESPONSIBILITIES:
  get_dashboard_kpis          — main dashboard numbers (cached 1hr)
  get_spending_by_category    — pie chart data (cached 1hr)
  get_monthly_trend           — 12-month bar chart (cached 24hr)
  get_daily_pattern           — day-of-week averages (cached 24hr)
  get_payment_mode_split      — payment method breakdown (cached 1hr)
  get_yearly_summary          — full year overview (cached 24hr)
  get_net_worth               — account + FD - dues (cached 30min)
  get_income_source_breakdown — income by source (cached 1hr)

CACHE PATTERN (Doc4 — Section 11.2):
  Every function follows the same cache-aside pattern:
    1. Build cache key
    2. Try Redis GET → return if hit
    3. Query PostgreSQL → compute result
    4. SET in Redis with TTL
    5. Return result

  Redis down → falls back to DB silently (never crashes the request).
  All monetary values stored as strings in cache (Decimal precision).

ALL THESE ARE READ-ONLY — no create/update/delete here.
Cache invalidation happens in expense_service and income_service on mutation.
"""

import uuid
import calendar
from decimal import Decimal
from datetime import date
from sqlalchemy import select, func, and_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import Expense
from app.models.income import Income
from app.models.category import Category
from app.models.account import Account, FixedDeposit
from app.models.due import Due
from app.models.budget import Budget
from app.models.enums import FDStatus, DueType
from app.cache.redis_client import redis_client
from app.cache.keys import CacheKeys
from app.utils.formatting import get_month_range
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

# TTLs (seconds)
_TTL_1H   = 3600
_TTL_30M  = 1800
_TTL_24H  = 86400


# ── Dashboard KPIs ─────────────────────────────────────────────────────────────

async def get_dashboard_kpis(
    db: AsyncSession,
    user_id: uuid.UUID,
    year: int,
    month: int,
    trace_id: str = "no-trace",
) -> dict:
    """
    Primary dashboard data. Cached 1 hour.
    Invalidated whenever any expense or income changes for this month.
    """
    cache_key = f"financeflow:analytics:{user_id}:dashboard:{year}:{month:02d}"
    cached = await redis_client.get_json(cache_key)
    if cached:
        log_event(logger, "dashboard_cache_hit", trace_id=trace_id,
                  user_id=str(user_id), year=year, month=month)
        return cached

    start, end = get_month_range(year, month)
    start_d, end_d = start.date(), end.date()

    # Current month totals
    exp_result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.user_id == user_id,
            Expense.is_deleted.is_(False),
            Expense.expense_date >= start_d,
            Expense.expense_date <= end_d,
        )
    )
    expense_total = Decimal(str(exp_result.scalar()))

    inc_result = await db.execute(
        select(func.coalesce(func.sum(Income.amount), 0)).where(
            Income.user_id == user_id,
            Income.is_deleted.is_(False),
            Income.income_date >= start_d,
            Income.income_date <= end_d,
        )
    )
    income_total = Decimal(str(inc_result.scalar()))
    net_savings = income_total - expense_total
    savings_rate = (
        round(float(net_savings) / float(income_total) * 100, 1)
        if income_total > 0 else 0.0
    )

    # Last month for comparison
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    prev_start, prev_end = get_month_range(prev_year, prev_month)
    prev_start_d, prev_end_d = prev_start.date(), prev_end.date()

    prev_exp = await db.scalar(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.user_id == user_id,
            Expense.is_deleted.is_(False),
            Expense.expense_date >= prev_start_d,
            Expense.expense_date <= prev_end_d,
        )
    )
    prev_inc = await db.scalar(
        select(func.coalesce(func.sum(Income.amount), 0)).where(
            Income.user_id == user_id,
            Income.is_deleted.is_(False),
            Income.income_date >= prev_start_d,
            Income.income_date <= prev_end_d,
        )
    )
    prev_exp_d = Decimal(str(prev_exp or 0))
    prev_inc_d = Decimal(str(prev_inc or 0))

    expense_vs_last = _pct_change(prev_exp_d, expense_total)
    income_vs_last  = _pct_change(prev_inc_d, income_total)

    # Top spending category this month
    top_cat = await db.execute(
        select(Category.name, func.sum(Expense.amount).label("total"))
        .join(Expense, Expense.category_id == Category.id)
        .where(
            Expense.user_id == user_id,
            Expense.is_deleted.is_(False),
            Expense.expense_date >= start_d,
            Expense.expense_date <= end_d,
        )
        .group_by(Category.name)
        .order_by(func.sum(Expense.amount).desc())
        .limit(1)
    )
    top = top_cat.first()

    # Budget alerts (at/above threshold)
    from app.services.budget_service import get_alerts_only
    budget_alerts = await get_alerts_only(db, user_id)

    # Due summary
    due_rows = await db.execute(
        select(Due.due_type, func.coalesce(func.sum(Due.amount), 0))
        .where(Due.user_id == user_id, Due.is_settled.is_(False))
        .group_by(Due.due_type)
    )
    due_map = {r.due_type: Decimal(str(r[1])) for r in due_rows.all()}
    i_owe    = due_map.get(DueType.I_OWE, Decimal("0"))
    they_owe = due_map.get(DueType.THEY_OWE, Decimal("0"))

    result = {
        "year": year,
        "month": month,
        "income_total": str(income_total),
        "expense_total": str(expense_total),
        "net_savings": str(net_savings),
        "savings_rate_pct": savings_rate,
        "income_vs_last_month_pct": income_vs_last,
        "expense_vs_last_month_pct": expense_vs_last,
        "top_category_name": top.name if top else None,
        "top_category_amount": str(top.total) if top else None,
        "budget_alerts": budget_alerts,
        "due_summary": {"i_owe": str(i_owe), "they_owe": str(they_owe)},
    }

    await redis_client.set_json(cache_key, result, ttl_seconds=_TTL_1H)
    return result


# ── Spending by Category ───────────────────────────────────────────────────────

async def get_spending_by_category(
    db: AsyncSession,
    user_id: uuid.UUID,
    year: int,
    month: int,
) -> list[dict]:
    """Category breakdown for pie chart. Cached 1 hour."""
    cache_key = CacheKeys.analytics_category_breakdown(str(user_id), year, month)
    cached = await redis_client.get_json(cache_key)
    if cached:
        return cached

    start, end = get_month_range(year, month)
    start_d, end_d = start.date(), end.date()

    rows = await db.execute(
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
            Expense.expense_date >= start_d,
            Expense.expense_date <= end_d,
        )
        .group_by(Category.id, Category.name, Category.icon, Category.color)
        .order_by(func.sum(Expense.amount).desc())
    )
    all_rows = rows.all()
    grand_total = sum(Decimal(str(r.total)) for r in all_rows)

    result = [
        {
            "category_id": str(r.id),
            "category_name": r.name,
            "icon": r.icon,
            "color": r.color,
            "total_amount": str(r.total),
            "transaction_count": r.count,
            "pct_of_total": round(
                float(r.total) / float(grand_total) * 100, 2
            ) if grand_total > 0 else 0.0,
        }
        for r in all_rows
    ]

    await redis_client.set_json(cache_key, result, ttl_seconds=_TTL_1H)
    return result


# ── Monthly Trend (12-month bar chart) ────────────────────────────────────────

async def get_monthly_trend(
    db: AsyncSession,
    user_id: uuid.UUID,
    year: int,
) -> list[dict]:
    """12-month income vs expense chart. Cached 24 hours."""
    cache_key = f"financeflow:analytics:{user_id}:monthly_trend:{year}"
    cached = await redis_client.get_json(cache_key)
    if cached:
        return cached

    # Expenses by month
    exp_rows = await db.execute(
        select(
            func.extract("month", Expense.expense_date).label("month"),
            func.coalesce(func.sum(Expense.amount), 0).label("total"),
        )
        .where(
            Expense.user_id == user_id,
            Expense.is_deleted.is_(False),
            func.extract("year", Expense.expense_date) == year,
        )
        .group_by(func.extract("month", Expense.expense_date))
    )
    exp_by_month = {int(r.month): Decimal(str(r.total)) for r in exp_rows.all()}

    # Income by month
    inc_rows = await db.execute(
        select(
            func.extract("month", Income.income_date).label("month"),
            func.coalesce(func.sum(Income.amount), 0).label("total"),
        )
        .where(
            Income.user_id == user_id,
            Income.is_deleted.is_(False),
            func.extract("year", Income.income_date) == year,
        )
        .group_by(func.extract("month", Income.income_date))
    )
    inc_by_month = {int(r.month): Decimal(str(r.total)) for r in inc_rows.all()}

    month_names = ["Jan","Feb","Mar","Apr","May","Jun",
                   "Jul","Aug","Sep","Oct","Nov","Dec"]
    result = []
    for m in range(1, 13):
        income  = inc_by_month.get(m, Decimal("0"))
        expense = exp_by_month.get(m, Decimal("0"))
        result.append({
            "month_number": m,
            "month_name":   month_names[m - 1],
            "income":       str(income),
            "expense":      str(expense),
            "net_savings":  str(income - expense),
        })

    await redis_client.set_json(cache_key, result, ttl_seconds=_TTL_24H)
    return result


# ── Daily Pattern (day-of-week averages) ──────────────────────────────────────

async def get_daily_pattern(
    db: AsyncSession,
    user_id: uuid.UUID,
    year: int,
    month: int,
) -> list[dict]:
    """Day-of-week spending averages for the given month. Cached 24 hours."""
    cache_key = f"financeflow:analytics:{user_id}:daily_pattern:{year}:{month:02d}"
    cached = await redis_client.get_json(cache_key)
    if cached:
        return cached

    start, end = get_month_range(year, month)
    start_d, end_d = start.date(), end.date()

    # PostgreSQL: EXTRACT(DOW) returns 0=Sunday … 6=Saturday
    rows = await db.execute(
        select(
            func.extract("dow", Expense.expense_date).label("dow"),
            func.coalesce(func.sum(Expense.amount), 0).label("total"),
            func.count(Expense.id).label("count"),
        )
        .where(
            Expense.user_id == user_id,
            Expense.is_deleted.is_(False),
            Expense.expense_date >= start_d,
            Expense.expense_date <= end_d,
        )
        .group_by(func.extract("dow", Expense.expense_date))
    )
    # Remap: Sunday=0 → 7 so Monday=1…Sunday=7
    day_names = {1:"Monday",2:"Tuesday",3:"Wednesday",4:"Thursday",
                 5:"Friday",6:"Saturday",7:"Sunday"}
    by_dow: dict[int, dict] = {}
    for r in rows.all():
        dow = int(r.dow)
        if dow == 0:
            dow = 7
        total = Decimal(str(r.total))
        avg = total / r.count if r.count > 0 else Decimal("0")
        by_dow[dow] = {
            "day": dow,
            "day_name": day_names[dow],
            "total": str(total),
            "avg": str(avg.quantize(Decimal("0.01"))),
        }

    result = [
        by_dow.get(d, {
            "day": d, "day_name": day_names[d],
            "total": "0", "avg": "0",
        })
        for d in range(1, 8)
    ]

    await redis_client.set_json(cache_key, result, ttl_seconds=_TTL_24H)
    return result


# ── Payment Mode Split ─────────────────────────────────────────────────────────

async def get_payment_mode_split(
    db: AsyncSession,
    user_id: uuid.UUID,
    year: int,
    month: int,
) -> list[dict]:
    """How spending is split across payment methods. Cached 1 hour."""
    cache_key = f"financeflow:analytics:{user_id}:payment_mode:{year}:{month:02d}"
    cached = await redis_client.get_json(cache_key)
    if cached:
        return cached

    start, end = get_month_range(year, month)
    start_d, end_d = start.date(), end.date()

    rows = await db.execute(
        select(
            Expense.payment_mode,
            func.coalesce(func.sum(Expense.amount), 0).label("total"),
        )
        .where(
            Expense.user_id == user_id,
            Expense.is_deleted.is_(False),
            Expense.expense_date >= start_d,
            Expense.expense_date <= end_d,
        )
        .group_by(Expense.payment_mode)
        .order_by(func.sum(Expense.amount).desc())
    )
    all_rows = rows.all()
    grand_total = sum(Decimal(str(r.total)) for r in all_rows)

    result = [
        {
            "payment_mode": r.payment_mode.value,
            "total_amount": str(r.total),
            "pct_of_total": round(
                float(r.total) / float(grand_total) * 100, 2
            ) if grand_total > 0 else 0.0,
        }
        for r in all_rows
    ]

    await redis_client.set_json(cache_key, result, ttl_seconds=_TTL_1H)
    return result


# ── Yearly Summary ─────────────────────────────────────────────────────────────

async def get_yearly_summary(
    db: AsyncSession,
    user_id: uuid.UUID,
    year: int,
) -> dict:
    """Full year overview. Cached 24 hours."""
    cache_key = f"financeflow:analytics:{user_id}:yearly:{year}"
    cached = await redis_client.get_json(cache_key)
    if cached:
        return cached

    monthly_trend = await get_monthly_trend(db, user_id, year)
    annual_income  = sum(Decimal(m["income"])  for m in monthly_trend)
    annual_expense = sum(Decimal(m["expense"]) for m in monthly_trend)
    annual_savings = annual_income - annual_expense

    months_with_data = [m for m in monthly_trend if Decimal(m["expense"]) > 0]
    avg_monthly = (
        annual_expense / len(months_with_data)
        if months_with_data else Decimal("0")
    )

    monthly_breakdown = []
    for m in monthly_trend:
        income  = Decimal(m["income"])
        expense = Decimal(m["expense"])
        savings = income - expense
        rate    = round(float(savings) / float(income) * 100, 1) if income > 0 else 0.0
        monthly_breakdown.append({
            "month": m["month_number"],
            "month_name": m["month_name"],
            "income": str(income),
            "expense": str(expense),
            "savings": str(savings),
            "savings_rate_pct": rate,
        })

    # Top categories for the full year
    top_cats = await db.execute(
        select(
            Category.id, Category.name, Category.icon, Category.color,
            func.sum(Expense.amount).label("total"),
            func.count(Expense.id).label("count"),
        )
        .join(Expense, Expense.category_id == Category.id)
        .where(
            Expense.user_id == user_id,
            Expense.is_deleted.is_(False),
            func.extract("year", Expense.expense_date) == year,
        )
        .group_by(Category.id, Category.name, Category.icon, Category.color)
        .order_by(func.sum(Expense.amount).desc())
        .limit(5)
    )
    top_categories = [
        {
            "category_id": str(r.id),
            "category_name": r.name,
            "icon": r.icon,
            "color": r.color,
            "total_amount": str(r.total),
            "transaction_count": r.count,
            "pct_of_total": round(
                float(r.total) / float(annual_expense) * 100, 2
            ) if annual_expense > 0 else 0.0,
        }
        for r in top_cats.all()
    ]

    result = {
        "year": year,
        "annual_income": str(annual_income),
        "annual_expense": str(annual_expense),
        "annual_savings": str(annual_savings),
        "avg_monthly_expense": str(avg_monthly.quantize(Decimal("0.01"))),
        "monthly_breakdown": monthly_breakdown,
        "top_categories": top_categories,
    }

    await redis_client.set_json(cache_key, result, ttl_seconds=_TTL_24H)
    return result


# ── Net Worth ──────────────────────────────────────────────────────────────────

async def get_net_worth(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> dict:
    """Snapshot of financial position. Cached 30 minutes."""
    cache_key = f"financeflow:analytics:{user_id}:net_worth"
    cached = await redis_client.get_json(cache_key)
    if cached:
        return cached

    # Account balances
    acc_total = await db.scalar(
        select(func.coalesce(func.sum(Account.current_balance), 0)).where(
            Account.user_id == user_id, Account.is_active.is_(True)
        )
    )

    # FD total (active FDs only)
    fd_total = await db.scalar(
        select(func.coalesce(func.sum(FixedDeposit.principal_amount), 0)).where(
            FixedDeposit.user_id == user_id,
            FixedDeposit.status == FDStatus.ACTIVE,
        )
    )

    # Dues
    due_rows = await db.execute(
        select(Due.due_type, func.coalesce(func.sum(Due.amount), 0))
        .where(Due.user_id == user_id, Due.is_settled.is_(False))
        .group_by(Due.due_type)
    )
    due_map = {r.due_type: Decimal(str(r[1])) for r in due_rows.all()}
    dues_receivable = due_map.get(DueType.THEY_OWE, Decimal("0"))
    dues_payable    = due_map.get(DueType.I_OWE,    Decimal("0"))

    acc_total_d = Decimal(str(acc_total or 0))
    fd_total_d  = Decimal(str(fd_total or 0))
    net_worth   = acc_total_d + fd_total_d + dues_receivable - dues_payable

    result = {
        "account_balances_total": str(acc_total_d),
        "fd_total": str(fd_total_d),
        "dues_receivable": str(dues_receivable),
        "dues_payable": str(dues_payable),
        "net_worth": str(net_worth),
    }

    await redis_client.set_json(cache_key, result, ttl_seconds=_TTL_30M)
    return result


# ── Income Source Breakdown ────────────────────────────────────────────────────

async def get_income_source_breakdown(
    db: AsyncSession,
    user_id: uuid.UUID,
    year: int,
    month: int,
) -> list[dict]:
    """Income by source for a month. Cached 1 hour."""
    cache_key = f"financeflow:analytics:{user_id}:income_sources:{year}:{month:02d}"
    cached = await redis_client.get_json(cache_key)
    if cached:
        return cached

    start, end = get_month_range(year, month)
    start_d, end_d = start.date(), end.date()

    rows = await db.execute(
        select(
            Income.source,
            func.coalesce(func.sum(Income.amount), 0).label("total"),
        )
        .where(
            Income.user_id == user_id,
            Income.is_deleted.is_(False),
            Income.income_date >= start_d,
            Income.income_date <= end_d,
        )
        .group_by(Income.source)
        .order_by(func.sum(Income.amount).desc())
    )
    all_rows = rows.all()
    grand_total = sum(Decimal(str(r.total)) for r in all_rows)

    result = [
        {
            "source": r.source.value,
            "total_amount": str(r.total),
            "pct_of_total": round(
                float(r.total) / float(grand_total) * 100, 2
            ) if grand_total > 0 else 0.0,
        }
        for r in all_rows
    ]

    await redis_client.set_json(cache_key, result, ttl_seconds=_TTL_1H)
    return result


# ── Internal helpers ───────────────────────────────────────────────────────────

def _pct_change(old: Decimal, new: Decimal) -> float:
    """
    Percentage change from old to new value.
    Returns 0.0 if old is zero (avoid division by zero).
    Positive = increase, negative = decrease.
    """
    if old == 0:
        return 0.0
    return round(float((new - old) / old * 100), 1)