"""
app/cache/keys.py — Redis Cache Key Builders

WHY THIS FILE EXISTS:
  Cache bugs are often caused by key typos: one service sets
  "financeflow:expenses:user_123" but another tries to get
  "financeflow:expense:user_123" (singular vs plural).
  The bug is invisible until a user sees stale data.

  Centralizing ALL key construction here means:
    - One place to look when debugging cache issues
    - Changing a key pattern only requires changing this file
    - Auto-complete in IDEs catches typos at dev time, not runtime

NAMING CONVENTION:
  "financeflow:{resource}:{user_id}:{sub_key}"

  Why "financeflow:" prefix: If this Redis instance is ever shared with
  another service (e.g., a job queue), namespacing prevents collisions.

TTL CONSTANTS (defined here, not hardcoded in service files):
  Each resource type has a different staleness tolerance.
  See individual constant docstrings for reasoning.
"""


# ── TTL Constants (seconds) ────────────────────────────────────────────────────

# Expense/income lists change frequently — user adds/edits often
TTL_LIST = 60  # 1 minute

# Analytics are expensive to compute (multiple aggregations) but acceptable
# to be 5 minutes stale — user won't notice slight delay in charts
TTL_ANALYTICS = 300  # 5 minutes

# User profile changes rarely (name, avatar, preferences)
TTL_USER_PROFILE = 3600  # 1 hour

# Budget overview changes on every transaction — keep fresh
TTL_BUDGET = 120  # 2 minutes

# Monthly summaries rarely change mid-day
TTL_MONTHLY_SUMMARY = 600  # 10 minutes


# ── Key Builders ────────────────────────────────────────────────────────────────
# All functions return strings. Use these in services, NEVER construct
# keys manually with f-strings in service/router files.


class CacheKeys:
    """
    Static factory methods for all Redis cache keys.

    Usage:
        from app.cache.keys import CacheKeys
        key = CacheKeys.expense_list(user_id="abc-123", page=1, limit=20)
    """

    # ── Expenses ──────────────────────────────────────────────────────────────

    @staticmethod
    def expense_list(user_id: str, page: int, limit: int) -> str:
        """
        Key for paginated expense list.
        Includes page + limit so different pages have independent caches.
        """
        return f"financeflow:expenses:{user_id}:list:p{page}:l{limit}"

    @staticmethod
    def expense_detail(user_id: str, expense_id: str) -> str:
        return f"financeflow:expenses:{user_id}:detail:{expense_id}"

    @staticmethod
    def expense_list_pattern(user_id: str) -> str:
        """
        Glob pattern for bulk deletion of ALL expense list pages for a user.
        Used when a new expense is added — all pages must be invalidated.

        Usage: await redis_client.client.keys(CacheKeys.expense_list_pattern(uid))
        """
        return f"financeflow:expenses:{user_id}:list:*"

    # ── Income ────────────────────────────────────────────────────────────────

    @staticmethod
    def income_list(user_id: str, page: int, limit: int) -> str:
        return f"financeflow:incomes:{user_id}:list:p{page}:l{limit}"

    @staticmethod
    def income_list_pattern(user_id: str) -> str:
        return f"financeflow:incomes:{user_id}:list:*"

    # ── Analytics ─────────────────────────────────────────────────────────────

    @staticmethod
    def analytics_monthly(user_id: str, year: int, month: int) -> str:
        """
        Key for monthly spending summary.
        Year + month means January 2024 and January 2025 have separate caches.
        """
        return f"financeflow:analytics:{user_id}:monthly:{year}:{month:02d}"

    @staticmethod
    def analytics_category_breakdown(user_id: str, year: int, month: int) -> str:
        return f"financeflow:analytics:{user_id}:category:{year}:{month:02d}"

    @staticmethod
    def analytics_pattern(user_id: str) -> str:
        """Bulk invalidation pattern — clears all analytics for a user."""
        return f"financeflow:analytics:{user_id}:*"

    # ── User Profile ──────────────────────────────────────────────────────────

    @staticmethod
    def user_profile(user_id: str) -> str:
        return f"financeflow:user:{user_id}:profile"

    # ── Budget ────────────────────────────────────────────────────────────────

    @staticmethod
    def budget_overview(user_id: str) -> str:
        return f"financeflow:budget:{user_id}:overview"

    @staticmethod
    def budget_detail(user_id: str, budget_id: str) -> str:
        return f"financeflow:budget:{user_id}:detail:{budget_id}"

    # ── Bank Accounts ─────────────────────────────────────────────────────────

    @staticmethod
    def account_list(user_id: str) -> str:
        return f"financeflow:accounts:{user_id}:list"

    @staticmethod
    def account_detail(user_id: str, account_id: str) -> str:
        return f"financeflow:accounts:{user_id}:detail:{account_id}"

    # ── Dues ──────────────────────────────────────────────────────────────────

    @staticmethod
    def dues_list(user_id: str) -> str:
        return f"financeflow:dues:{user_id}:list"

    @staticmethod
    def dues_summary(user_id: str) -> str:
        """Net position: total I owe vs total they owe."""
        return f"financeflow:dues:{user_id}:summary"

    # ── Categories ────────────────────────────────────────────────────────────

    @staticmethod
    def user_categories(user_id: str) -> str:
        """All categories visible to a user (system + custom). TTL: 7 days."""
        return f"financeflow:categories:{user_id}:list"

    # ── Rate Limiter ──────────────────────────────────────────────────────────

    @staticmethod
    def rate_limit(ip_address: str) -> str:
        """
        Key for IP-based rate limiting (Video 20 — Security).
        Incremented on every request. Expires after 60 seconds (sliding window).
        """
        return f"financeflow:ratelimit:{ip_address}"

    @staticmethod
    def rate_limit_ai(user_id: str) -> str:
        """
        Stricter rate limit specifically for AI endpoints (Gemini calls are expensive).
        Per-user rather than per-IP because authenticated users are trusted more.
        """
        return f"financeflow:ratelimit:ai:{user_id}"