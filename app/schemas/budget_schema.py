"""
app/schemas/budget_schema.py — Budget Request & Response Schemas

COVERS (Doc2 — Section 3.4):
  GET  /budgets              → list[BudgetWithStatus]
  POST /budgets              → BudgetCreate → BudgetWithStatus
  GET  /budgets/{id}         → BudgetWithStatus
  PATCH /budgets/{id}        → BudgetUpdate → BudgetWithStatus
  DELETE /budgets/{id}       → deleted_response() (soft-delete)
  GET  /budgets/status       → list[BudgetWithStatus] (all active + current spend)
  GET  /budgets/alerts       → list[BudgetWithStatus] (only those at/over threshold)

BUDGET STATUS LOGIC (Doc2 — Section 4.1 & Doc4 — Section 10.1):
  The service layer computes spent_so_far by SUMming current-period expenses.
  Status is one of:
    SAFE      → spent_pct < alert_threshold_percent
    WARNING   → spent_pct >= alert_threshold_percent (but not exceeded)
    EXCEEDED  → spent > limit

  BudgetWithStatus is the rich response that includes this real-time data.
  It is NOT cached — budget checks must always be real-time.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import BudgetPeriod
from app.schemas.category_schema import CategoryBrief


# ── Request Schemas ────────────────────────────────────────────────────────────

class BudgetCreate(BaseModel):
    """
    POST /budgets — create a new spending budget.

    Uniqueness rule (enforced in service, not here):
      Only one ACTIVE budget per user per category per period.
      Creating a second MONTHLY Food budget raises DuplicateResourceException.
    """

    category_id: uuid.UUID
    subcategory_id: uuid.UUID | None = None  # If set: subcategory-level budget
    limit_amount: Decimal = Field(..., gt=0, description="Budget limit in INR")
    period: BudgetPeriod = BudgetPeriod.MONTHLY

    # Alert when spending reaches this % of limit (default 80%)
    alert_threshold_percent: int = Field(80, ge=10, le=99)

    # Optional date range for the budget (None = indefinite)
    start_date: date | None = None
    end_date: date | None = None


class BudgetUpdate(BaseModel):
    """
    PATCH /budgets/{id} — partial update.

    Common update: change limit_amount (₹2000 → ₹3000) or alert threshold.
    Deactivation: set is_active=False (soft-delete).
    """

    limit_amount: Decimal | None = Field(None, gt=0)
    alert_threshold_percent: int | None = Field(None, ge=10, le=99)
    is_active: bool | None = None
    end_date: date | None = None


# ── Response Schemas ───────────────────────────────────────────────────────────

class BudgetResponse(BaseModel):
    """Raw budget row — no computed spend data."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    category_id: uuid.UUID
    subcategory_id: uuid.UUID | None
    limit_amount: Decimal
    period: BudgetPeriod
    alert_threshold_percent: int
    is_active: bool
    start_date: date | None
    end_date: date | None
    created_at: datetime
    updated_at: datetime


class BudgetWithStatus(BaseModel):
    """
    Budget + real-time spending data.

    The service computes spent_so_far, remaining, spent_pct, and status
    by querying the expenses table for the current period.
    This is the primary budget response shape — richer than BudgetResponse.
    """

    # Budget fields
    id: uuid.UUID
    category: CategoryBrief
    subcategory_id: uuid.UUID | None
    limit_amount: Decimal
    period: BudgetPeriod
    alert_threshold_percent: int
    is_active: bool
    start_date: date | None
    end_date: date | None

    # Real-time computed fields (set by service)
    spent_so_far: Decimal = Decimal("0.00")
    remaining: Decimal = Decimal("0.00")
    spent_pct: float = 0.0
    status: str = "SAFE"  # "SAFE" | "WARNING" | "EXCEEDED"

    created_at: datetime
    updated_at: datetime