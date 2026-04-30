"""
app/schemas/income_schema.py — Income Request & Response Schemas

COVERS (Doc2 — Section 3.3):
  GET  /incomes                → IncomeFilter → list[IncomeResponse]
  POST /incomes                → IncomeCreate → IncomeResponse
  GET  /incomes/{id}           → IncomeResponse
  PATCH /incomes/{id}          → IncomeUpdate → IncomeResponse
  DELETE /incomes/{id}         → deleted_response()
  GET  /incomes/summary        → IncomeSummaryResponse

STUDENT CONTEXT:
  Most income for our user Nisarg comes from parents via UPI.
  'sender_name' is how the student labels it ("Papa", "Maa", "HDFC Scholarship").
  This helps the AI assistant give contextual insights:
  "100% of your income this month came from your parents."
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.models.enums import IncomeSource, PaymentMode


_MIN_INCOME_DATE = date(2020, 1, 1)


# ── Request: Create ────────────────────────────────────────────────────────────

class IncomeCreate(BaseModel):
    """POST /incomes body."""

    amount: Decimal = Field(..., gt=0, le=Decimal("9999999.99"))
    source: IncomeSource
    income_date: date = Field(default_factory=date.today)

    # Human-readable label for who sent the money
    sender_name: str | None = Field(
        None,
        max_length=255,
        description="e.g. 'Papa', 'Government', 'TCS Scholarship'",
    )
    description: str = Field(
        "",
        max_length=500,
        description="Optional note, e.g. 'Monthly allowance for March'",
    )
    notes: str | None = None

    # Which account received the money (for balance tracking)
    account_id: uuid.UUID | None = None

    # How it was received
    payment_mode: PaymentMode | None = None

    # Screenshot of UPI payment for record-keeping
    screenshot_url: str | None = None
    screenshot_public_id: str | None = None

    # If source=LOAN, optionally link to the due entry
    due_id: uuid.UUID | None = None

    is_recurring: bool = False

    @field_validator("income_date")
    @classmethod
    def date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("Income date cannot be in the future")
        if v < _MIN_INCOME_DATE:
            raise ValueError("Income date is too far in the past (before 2020-01-01)")
        return v


# ── Request: Update ────────────────────────────────────────────────────────────

class IncomeUpdate(BaseModel):
    """PATCH /incomes/{id} — partial update."""

    amount: Decimal | None = Field(None, gt=0)
    source: IncomeSource | None = None
    income_date: date | None = None
    sender_name: str | None = Field(None, max_length=255)
    description: str | None = Field(None, max_length=500)
    notes: str | None = None
    account_id: uuid.UUID | None = None
    payment_mode: PaymentMode | None = None
    screenshot_url: str | None = None
    screenshot_public_id: str | None = None
    is_recurring: bool | None = None

    @field_validator("income_date")
    @classmethod
    def date_not_future(cls, v: date | None) -> date | None:
        if v is None:
            return v
        if v > date.today():
            raise ValueError("Income date cannot be in the future")
        return v


# ── Request: Filters ───────────────────────────────────────────────────────────

class IncomeFilter(BaseModel):
    """Query parameters for GET /incomes."""

    from_date: date | None = None
    to_date: date | None = None
    source: IncomeSource | None = None
    account_id: uuid.UUID | None = None
    search: str | None = Field(None, max_length=100)
    page: int = Field(1, ge=1)
    limit: int = Field(20, ge=1, le=100)


# ── Response Schemas ───────────────────────────────────────────────────────────

class IncomeResponse(BaseModel):
    """Single income record as returned by the API."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    amount: Decimal
    source: IncomeSource
    income_date: date
    sender_name: str | None
    description: str
    notes: str | None
    account_id: uuid.UUID | None
    payment_mode: PaymentMode | None
    screenshot_url: str | None
    is_recurring: bool
    created_at: datetime
    updated_at: datetime


# ── Summary ────────────────────────────────────────────────────────────────────

class IncomeSourceBreakdown(BaseModel):
    """One row in the income source breakdown."""

    source: IncomeSource
    total_amount: Decimal
    transaction_count: int
    pct_of_total: float


class IncomeSummaryResponse(BaseModel):
    """GET /incomes/summary — monthly income totals by source."""

    year: int
    month: int
    total_amount: Decimal
    transaction_count: int
    by_source: list[IncomeSourceBreakdown]