"""
app/schemas/__init__.py — Schema Registry

Re-exports every public schema so routers and services can import cleanly:

    from app.schemas import ExpenseCreate, ExpenseResponse

instead of:

    from app.schemas.expense_schema import ExpenseCreate, ExpenseResponse

RULE: When you add a new schema file, add its public classes here.
"""

# ── Common ─────────────────────────────────────────────────────────────────────
from app.schemas.common import (  # noqa: F401
    PaginationMeta,
    SuccessResponse,
    success_response,
    deleted_response,
)

# ── User ───────────────────────────────────────────────────────────────────────
from app.schemas.user_schema import (  # noqa: F401
    ClerkWebhookPayload,
    ClerkWebhookData,
    NotificationPrefs,
    UserUpdate,
    OnboardingComplete,
    UserResponse,
)

# ── Category ───────────────────────────────────────────────────────────────────
from app.schemas.category_schema import (  # noqa: F401
    CategoryCreate,
    CategoryUpdate,
    CategoryBrief,
    CategoryResponse,
    SubcategoryCreate,
    SubcategoryUpdate,
    SubcategoryBrief,
    SubcategoryResponse,
)

# ── Expense ────────────────────────────────────────────────────────────────────
from app.schemas.expense_schema import (  # noqa: F401
    ExpenseCreate,
    ExpenseUpdate,
    ExpenseFilter,
    ExpenseResponse,
    CategorySpend,
    MonthlySummaryResponse,
)

# ── Income ─────────────────────────────────────────────────────────────────────
from app.schemas.income_schema import (  # noqa: F401
    IncomeCreate,
    IncomeUpdate,
    IncomeFilter,
    IncomeResponse,
    IncomeSourceBreakdown,
    IncomeSummaryResponse,
)

# ── Account ────────────────────────────────────────────────────────────────────
from app.schemas.account_schema import (  # noqa: F401
    BranchCreate,
    BranchResponse,
    AccountCreate,
    AccountUpdate,
    AccountResponse,
    AccountDetail,
    BalanceUpdate,
    ServiceCreate,
    ServiceUpdate,
    ServiceResponse,
    FDCreate,
    FDUpdate,
    FDResponse,
    StatementEntryCreate,
    StatementEntryResponse,
    DocumentResponse,
)

# ── Budget ─────────────────────────────────────────────────────────────────────
from app.schemas.budget_schema import (  # noqa: F401
    BudgetCreate,
    BudgetUpdate,
    BudgetResponse,
    BudgetWithStatus,
)

# ── Groups & Dues ──────────────────────────────────────────────────────────────
from app.schemas.group_schema import (  # noqa: F401
    GroupCreate,
    GroupUpdate,
    GroupResponse,
    GroupDetail,
    MemberCreate,
    MemberResponse,
    GroupExpenseCreate,
    GroupExpenseResponse,
    SplitInput,
    SplitResponse,
    SettleSplit,
    MemberBalance,
    DueCreate,
    DueUpdate,
    DueSettle,
    DueFilter,
    DueResponse,
    DueSummaryResponse,
)

# ── Analytics ──────────────────────────────────────────────────────────────────
from app.schemas.analytics_schema import (  # noqa: F401
    AnalyticsPeriodFilter,
    AnalyticsDateRangeFilter,
    DashboardKPI,
    BudgetAlertItem,
    CategorySpendRow,
    MonthlyTrend,
    DailyPattern,
    PaymentModeSplit,
    YearlySummary,
    AccountAnalytics,
    IncomeSourceRow,
    NetWorth,
)

# ── Notifications & AI ─────────────────────────────────────────────────────────
from app.schemas.notification_schema import (  # noqa: F401
    NotificationResponse,
    NotificationCheckResult,
    NotificationFilter,
    AIInsightRequest,
    AIInsightResponse,
    AIChatRequest,
    AIChatResponse,
    AIChatSessionResponse,
)