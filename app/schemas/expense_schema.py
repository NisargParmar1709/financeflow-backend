"""
app/schemas/expense_schema.py — Expense Request & Response Schemas

COVERS (Doc2 — Section 3.2):
  GET  /expenses               → ExpenseFilter (query params) → list[ExpenseResponse]
  POST /expenses               → ExpenseCreate → ExpenseResponse
  GET  /expenses/{id}          → ExpenseResponse
  PATCH /expenses/{id}         → ExpenseUpdate → ExpenseResponse
  DELETE /expenses/{id}        → deleted_response()
  GET  /expenses/monthly-summary → MonthlySummaryResponse

VALIDATION RULES (Doc2 — Section 5.1):
  1. amount must be > 0 and ≤ 999999 (sanity cap for student app)
  2. expense_date cannot be in the future (you can't log tomorrow's expense)
  3. expense_date cannot be before 2020-01-01 (prevents typo dates like 2002)
  4. If payment_mode != CASH → account_id is REQUIRED (which card/account?)
     This is a cross-field rule — validated via model_validator.
  5. is_recurring=True → recurrence_period must be provided

The cross-field CASH/account rule:
  CASH payment means the user paid with physical cash — no bank account
  is involved, so account_id makes no sense.
  Any other payment mode (UPI, Card, etc.) means money left a specific
  account. We need account_id to update that account's balance.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import PaymentMode
from app.schemas.category_schema import CategoryBrief, SubcategoryBrief


# ── Constants ──────────────────────────────────────────────────────────────────

_MIN_EXPENSE_DATE = date(2020, 1, 1)
_MAX_AMOUNT = Decimal("999999.99")


# ── Request: Create ────────────────────────────────────────────────────────────

class ExpenseCreate(BaseModel):
    """
    POST /expenses body.

    Fields marked with * in Doc2 are required here (no default).
    Fields without * are optional — provide them for richer tracking.
    """

    amount: Decimal = Field(
        ...,
        gt=0,
        le=_MAX_AMOUNT,
        description="Expense amount in INR. Must be > 0.",
    )
    category_id: uuid.UUID
    payment_mode: PaymentMode

    # Defaults to today — student rarely backdates expenses
    expense_date: date = Field(default_factory=date.today)

    # Required for all non-CASH payments (validated in model_validator)
    account_id: uuid.UUID | None = None

    # Optional enrichment fields
    subcategory_id: uuid.UUID | None = None
    description: str = Field(
        "",
        max_length=500,
        description="What was bought. E.g. 'Dinner at Agashiye'",
    )
    notes: str | None = Field(None, description="Longer user note, not shown in lists")
    receipt_url: str | None = Field(None, description="Cloudinary secure_url of receipt photo")
    receipt_public_id: str | None = Field(None, description="Cloudinary public_id, used for deletion")

    # Recurring expense support
    is_recurring: bool = False
    recurrence_period: str | None = Field(
        None,
        description="DAILY | WEEKLY | MONTHLY. Required when is_recurring=True.",
    )

    @field_validator("expense_date")
    @classmethod
    def date_not_future(cls, v: date) -> date:
        """
        Doc2 rule: 'expense_date cannot be in the future'.
        Student might log yesterday's chai — that's fine.
        But logging tomorrow's rent in advance is an error.
        """
        if v > date.today():
            raise ValueError("Expense date cannot be in the future")
        if v < _MIN_EXPENSE_DATE:
            raise ValueError("Expense date is too far in the past (before 2020-01-01)")
        return v

    @model_validator(mode="after")
    def non_cash_requires_account(self) -> "ExpenseCreate":
        """
        Doc2 cross-field rule:
          'If payment_mode != CASH, account_id is required'

        Why: UPI, Card, Net Banking all debit a specific account.
             We need account_id to track which account was debited.
             CASH payments don't touch a bank account.
        """
        if self.payment_mode != PaymentMode.CASH and self.account_id is None:
            raise ValueError(
                "account_id is required for non-cash payment modes "
                f"(payment_mode={self.payment_mode.value})"
            )
        return self

    @model_validator(mode="after")
    def recurring_needs_period(self) -> "ExpenseCreate":
        """If marked as recurring, the period must be specified."""
        if self.is_recurring and not self.recurrence_period:
            raise ValueError("recurrence_period is required when is_recurring=True")
        return self


# ── Request: Update ────────────────────────────────────────────────────────────

class ExpenseUpdate(BaseModel):
    """
    PATCH /expenses/{id} body.

    All fields are optional — send only the fields to update.
    The service re-runs the budget check if amount or category changes.
    """

    amount: Decimal | None = Field(None, gt=0, le=_MAX_AMOUNT)
    category_id: uuid.UUID | None = None
    subcategory_id: uuid.UUID | None = None
    payment_mode: PaymentMode | None = None
    account_id: uuid.UUID | None = None
    expense_date: date | None = None
    description: str | None = Field(None, max_length=500)
    notes: str | None = None
    receipt_url: str | None = None
    receipt_public_id: str | None = None
    is_recurring: bool | None = None
    recurrence_period: str | None = None

    @field_validator("expense_date")
    @classmethod
    def date_not_future(cls, v: date | None) -> date | None:
        if v is None:
            return v
        if v > date.today():
            raise ValueError("Expense date cannot be in the future")
        if v < _MIN_EXPENSE_DATE:
            raise ValueError("Expense date is too far in the past (before 2020-01-01)")
        return v


# ── Request: Filters ───────────────────────────────────────────────────────────

class ExpenseFilter(BaseModel):
    """
    Query parameters for GET /expenses.

    All optional, all combinable. Passed as query string:
      GET /expenses?from_date=2025-01-01&to_date=2025-01-31&payment_mode=UPI

    Doc2: 'Filters are all optional and combinable. sum = total amount
    for the filter period.'
    """

    from_date: date | None = None
    to_date: date | None = None
    category_id: uuid.UUID | None = None
    subcategory_id: uuid.UUID | None = None
    payment_mode: PaymentMode | None = None
    account_id: uuid.UUID | None = None
    search: str | None = Field(None, max_length=100, description="Search in description")
    min_amount: Decimal | None = Field(None, ge=0)
    max_amount: Decimal | None = Field(None, gt=0)
    is_recurring: bool | None = None

    # Pagination
    page: int = Field(1, ge=1)
    limit: int = Field(20, ge=1, le=100)


# ── Response Schemas ───────────────────────────────────────────────────────────

class ExpenseResponse(BaseModel):
    """
    Single expense as returned by the API.

    Note: category and subcategory are embedded brief objects.
    The frontend can render an expense card without any extra API calls.
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    amount: Decimal
    description: str
    notes: str | None
    payment_mode: PaymentMode
    expense_date: date
    is_recurring: bool
    recurrence_period: str | None
    receipt_url: str | None
    account_id: uuid.UUID | None

    # Nested brief objects (populated by service via selection load)
    category: CategoryBrief
    subcategory: SubcategoryBrief | None

    created_at: datetime
    updated_at: datetime


# ── Monthly Summary ────────────────────────────────────────────────────────────

class CategorySpend(BaseModel):
    """One row in the monthly category breakdown."""

    category_id: uuid.UUID
    category_name: str
    icon: str | None
    color: str | None
    total_amount: Decimal
    transaction_count: int
    pct_of_total: float  # e.g. 34.5 means 34.5%


class MonthlySummaryResponse(BaseModel):
    """
    GET /expenses/monthly-summary response.

    Cached in Redis for 1 hour. Used by the dashboard and analytics page.
    """

    year: int
    month: int
    total_amount: Decimal
    transaction_count: int
    daily_average: Decimal
    by_category: list[CategorySpend]