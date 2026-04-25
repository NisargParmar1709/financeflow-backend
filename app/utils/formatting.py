"""
app/utils/formatting.py — Shared Helper Utilities

WHY THIS FILE EXISTS:
  Helpers that are used across multiple layers (services, schemas, routers)
  live here. The rule: if you find yourself copy-pasting the same function
  into two files, it belongs in utils/.

  These are PURE FUNCTIONS — no DB, no HTTP, no side effects.
  Pure functions are trivially testable (see tests/unit/test_formatting.py).

UTILS LAYER RULE (Video 10):
  Utils are not a dumping ground. A function belongs here only if:
    1. It's used in at least 2 different places
    2. It has zero dependency on any app layer (no DB session, no request)
    3. It's stateless and deterministic (same input → same output always)
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, date
import pytz

# India Standard Time — our app is India-focused
IST = pytz.timezone("Asia/Kolkata")


# ── Currency Formatting ────────────────────────────────────────────────────────

def format_inr(amount: Decimal | float | int) -> str:
    """
    Formats a decimal amount to Indian Rupee string with Indian numbering system.

    Indian numbering: 1,50,000 (not 150,000)
    The Indian system groups digits as: last 3, then groups of 2.

    Examples:
        1500.50   → "₹1,500.50"
        150000.00 → "₹1,50,000.00"
        99.5      → "₹99.50"

    Why Decimal not float: Floats have representation errors.
        0.1 + 0.2 == 0.30000000000000004 in Python.
        For money, always use Decimal with ROUND_HALF_UP (banker-friendly).
    """
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))

    # Round to 2 decimal places (paise)
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Split into integer and decimal parts
    str_amount = str(amount)
    if "." in str_amount:
        int_part, dec_part = str_amount.split(".")
    else:
        int_part, dec_part = str_amount, "00"

    # Apply Indian numbering format to the integer part
    int_formatted = _apply_indian_numbering(int_part)

    return f"₹{int_formatted}.{dec_part}"


def _apply_indian_numbering(num_str: str) -> str:
    """
    Converts "150000" → "1,50,000" using Indian grouping rules.

    Logic:
      - Last 3 digits form the first group: 000
      - Every 2 digits before that: 50, 1
      - Join with commas in reverse: 1,50,000
    """
    # Handle negative numbers
    negative = num_str.startswith("-")
    if negative:
        num_str = num_str[1:]

    if len(num_str) <= 3:
        return f"-{num_str}" if negative else num_str

    # Last 3 digits
    last_three = num_str[-3:]
    remaining = num_str[:-3]

    # Group remaining in pairs from right
    groups = []
    while remaining:
        groups.append(remaining[-2:])
        remaining = remaining[:-2]

    groups.reverse()
    result = ",".join(groups) + "," + last_three

    return f"-{result}" if negative else result


def parse_amount(value: str | float | int | Decimal) -> Decimal:
    """
    Safely converts any numeric input to Decimal for storage.

    Why: API clients might send "1500.50" (string), 1500.50 (float),
    or 1500 (int). We normalize all to Decimal before DB writes.
    """
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        raise ValueError(f"Invalid amount value: {value!r}")


# ── Date & Time Utilities ──────────────────────────────────────────────────────

def now_utc() -> datetime:
    """
    Returns current time in UTC (timezone-aware).

    Why not datetime.now(): datetime.now() returns a "naive" datetime
    (no timezone). Storing naive datetimes in PostgreSQL TIMESTAMPTZ
    causes inconsistencies. Always use timezone-aware datetimes.

    Why UTC for storage: Store in UTC, display in IST.
    Never store local time — timezone offsets change (DST, etc.)
    """
    return datetime.now(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    """
    Converts a UTC datetime to IST for display.

    Our DB always stores UTC. Frontend expects IST.
    This conversion happens in the response schema, not in the DB layer.
    """
    if dt.tzinfo is None:
        # Naive datetime — assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def format_display_date(dt: datetime) -> str:
    """
    Formats datetime to human-readable IST string for API responses.

    Example: "15 Jan 2024, 11:30 PM IST"
    """
    ist_dt = to_ist(dt)
    return ist_dt.strftime("%-d %b %Y, %I:%M %p IST")


def get_month_range(year: int, month: int) -> tuple[datetime, datetime]:
    """
    Returns (start_of_month, end_of_month) as UTC datetimes.

    Used for monthly analytics queries:
        SELECT * FROM expenses
        WHERE created_at BETWEEN start AND end
        AND user_id = :user_id

    Args:
        year: 4-digit year (2024)
        month: 1-12

    Returns:
        Tuple of (first_moment_of_month, last_moment_of_month) in UTC
    """
    import calendar

    # First moment of month in IST → convert to UTC for DB query
    start_ist = IST.localize(datetime(year, month, 1, 0, 0, 0))
    start_utc = start_ist.astimezone(timezone.utc)

    # Last day of month
    last_day = calendar.monthrange(year, month)[1]
    end_ist = IST.localize(datetime(year, month, last_day, 23, 59, 59, 999999))
    end_utc = end_ist.astimezone(timezone.utc)

    return start_utc, end_utc


# ── Pagination Utilities ───────────────────────────────────────────────────────

def build_pagination_meta(
    total_count: int,
    page: int,
    limit: int,
) -> dict:
    """
    Builds the pagination metadata object included in list API responses.

    From Video 11 (REST API Design):
      List APIs must return pagination metadata so the frontend can render
      page controls without making a separate "count" request.

    Returns:
        {
            "total": 143,
            "page": 2,
            "limit": 20,
            "total_pages": 8,
            "has_next": true,
            "has_prev": true
        }
    """
    import math
    total_pages = math.ceil(total_count / limit) if limit > 0 else 0

    return {
        "total": total_count,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }


def calculate_offset(page: int, limit: int) -> int:
    """
    Converts page number to SQL OFFSET value.

    SQL pagination uses OFFSET, not page numbers.
    Page 1, limit 20 → OFFSET 0 (skip 0 rows)
    Page 2, limit 20 → OFFSET 20 (skip first 20 rows)
    Page 3, limit 20 → OFFSET 40

    Why not cursor-based pagination: Cursor pagination is better for
    infinite scroll but harder to implement. Offset pagination is
    simpler and sufficient for FinanceFlow's data volumes.
    """
    return (page - 1) * limit