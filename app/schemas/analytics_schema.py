"""
app/schemas/analytics_schema.py — Analytics Response Shapes

COVERS (Doc2 — Section 3.10):
  GET /analytics/dashboard          → DashboardKPI
  GET /analytics/spending-by-category → list[CategorySpendRow]
  GET /analytics/monthly-trend      → list[MonthlyTrend]
  GET /analytics/daily-pattern      → list[DailyPattern]
  GET /analytics/payment-mode-split → list[PaymentModeSplit]
  GET /analytics/yearly             → YearlySummary
  GET /analytics/accounts           → list[AccountAnalytics]
  GET /analytics/income-sources     → list[IncomeSourceRow]
  GET /analytics/net-worth          → NetWorth

ALL ANALYTICS ENDPOINTS ARE READ-ONLY — no Create/Update schemas needed.
All are cached in Redis. Cache TTLs are documented in each service function.

Why these are pure response schemas (no request body):
  Analytics are derived from the user's own data.
  The only inputs are time-range parameters in the URL query string.
  See AnalyticsPeriodFilter below for the shared query param schema.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import PaymentMode

# ── Shared Query Params ────────────────────────────────────────────────────────


class AnalyticsPeriodFilter(BaseModel):
    """
    Shared query parameters for analytics endpoints that take a period.
    Used by the router's Depends() injection.
    """

    month: int = Field(..., ge=1, le=12, description="Month number 1-12")
    year: int = Field(..., ge=2020, le=2100, description="4-digit year")


class AnalyticsDateRangeFilter(BaseModel):
    """For endpoints that take a flexible date range instead of month/year."""

    from_date: date
    to_date: date


# ── Dashboard ──────────────────────────────────────────────────────────────────


class BudgetAlertItem(BaseModel):
    """One budget alert shown on the dashboard."""

    budget_id: str
    category_name: str
    category_icon: str | None
    limit_amount: Decimal
    spent_so_far: Decimal
    spent_pct: float
    status: str  # "WARNING" | "EXCEEDED"


class DashboardDueSummary(BaseModel):
    """Compact due summary embedded in DashboardKPI."""

    i_owe: Decimal
    they_owe: Decimal


class DashboardKPI(BaseModel):
    """
    GET /analytics/dashboard — primary dashboard data.

    Cached 1 hour. Invalidated when any expense or income changes.
    All monetary values in INR (Decimal for precision).
    """

    year: int
    month: int

    # Core KPIs
    income_total: Decimal
    expense_total: Decimal
    net_savings: Decimal
    savings_rate_pct: float  # (net_savings / income_total) * 100

    # vs last month (% change, can be negative)
    income_vs_last_month_pct: float
    expense_vs_last_month_pct: float

    # Highlights
    top_category_name: str | None
    top_category_amount: Decimal | None

    # Active budget alerts (at or over threshold)
    budget_alerts: list[BudgetAlertItem] = []

    # Compact due summary
    due_summary: DashboardDueSummary


# ── Spending by Category ───────────────────────────────────────────────────────


class CategorySpendRow(BaseModel):
    """One row in the spending-by-category breakdown (pie chart data)."""

    category_id: str
    category_name: str
    icon: str | None
    color: str | None
    total_amount: Decimal
    transaction_count: int
    pct_of_total: float


# ── Monthly Trend (12-month bar chart) ────────────────────────────────────────


class MonthlyTrend(BaseModel):
    """
    One month in the income vs expense trend chart.
    Returns 12 rows for a full year.
    """

    month_number: int  # 1-12
    month_name: str  # "Jan", "Feb", ...
    income: Decimal
    expense: Decimal
    net_savings: Decimal


# ── Daily Pattern (day-of-week averages) ──────────────────────────────────────


class DailyPattern(BaseModel):
    """Average spending per day-of-week for a given month."""

    day: int  # 1 = Monday, 7 = Sunday
    day_name: str  # "Monday", "Tuesday", ...
    total: Decimal
    avg: Decimal


# ── Payment Mode Split (pie chart) ────────────────────────────────────────────


class PaymentModeSplit(BaseModel):
    """How spending is split across payment methods."""

    payment_mode: PaymentMode
    total_amount: Decimal
    pct_of_total: float


# ── Yearly Summary ─────────────────────────────────────────────────────────────


class MonthlyBreakdownRow(BaseModel):
    """One month row inside YearlySummary."""

    month: int
    month_name: str
    income: Decimal
    expense: Decimal
    savings: Decimal
    savings_rate_pct: float


class YearlySummary(BaseModel):
    """GET /analytics/yearly — full year overview."""

    year: int
    annual_income: Decimal
    annual_expense: Decimal
    annual_savings: Decimal
    avg_monthly_expense: Decimal
    monthly_breakdown: list[MonthlyBreakdownRow]
    top_categories: list[CategorySpendRow]


# ── Account Analytics ──────────────────────────────────────────────────────────


class AccountAnalytics(BaseModel):
    """Per-account analytics summary for the year."""

    account_id: str
    bank_name: str
    account_last4: str
    current_balance: Decimal
    fd_total: Decimal
    total_debited_ytd: Decimal
    total_credited_ytd: Decimal


# ── Income Source Breakdown ────────────────────────────────────────────────────


class IncomeSourceRow(BaseModel):
    """One source in the income breakdown."""

    source: str  # IncomeSource enum value
    total_amount: Decimal
    pct_of_total: float


# ── Net Worth ──────────────────────────────────────────────────────────────────


class NetWorth(BaseModel):
    """
    GET /analytics/net-worth — snapshot of financial position.

    Net worth = account_balances_total + fd_total - dues_payable
    Cached 30 minutes.
    """

    account_balances_total: Decimal
    fd_total: Decimal
    dues_receivable: Decimal  # They owe you
    dues_payable: Decimal  # You owe them
    net_worth: Decimal