"""
app/schemas/group_schema.py — Group Expense & Dues Schemas

COVERS (Doc2 — Section 3.8, 3.9):
  /groups                    → GroupCreate, GroupUpdate, GroupResponse, GroupDetail
  /groups/{id}/members       → MemberCreate, MemberResponse
  /groups/{id}/expenses      → GroupExpenseCreate, GroupExpenseResponse
  /groups/{id}/balances      → MemberBalance
  /groups/{id}/expenses/{eid}/splits/{sid}/settle → SettleSplit
  /dues                      → DueCreate, DueUpdate, DueResponse, DueSummary

SPLIT VALIDATION RULES (Doc4 — Section 7.4):
  EQUAL:       auto-calculated as total / count. No per-member amounts needed.
  PERCENTAGE:  splits[].percentage must sum to exactly 100.00
  EXACT:       splits[].amount must sum to exactly total_amount.
  These are enforced in group_service.py, not here — the schema collects
  the raw input and the service validates the aggregate constraint.

GROUP MEMBER DESIGN (Doc1 — Section 5.2):
  Members don't need to be app users. GroupMember.user_id is nullable.
  You can add "Raj" by name alone. If Raj joins the app later, his
  account can be linked via PATCH /groups/{id}/members/{mid}/link-user.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import SplitType, PaymentMode


# ── Group ──────────────────────────────────────────────────────────────────────

class GroupCreate(BaseModel):
    """POST /groups — create a group (trip, flat, event)."""

    name: str = Field(..., min_length=1, max_length=150)
    description: str | None = Field(None, max_length=500)

    # Initial members to add (besides the creator, who is auto-added)
    members: list["MemberCreate"] = []


class GroupUpdate(BaseModel):
    """PATCH /groups/{id}."""

    name: str | None = Field(None, min_length=1, max_length=150)
    description: str | None = None
    is_active: bool | None = None


class MemberCreate(BaseModel):
    """Add a member to a group. user_id is optional — non-app users OK."""

    name: str = Field(..., min_length=1, max_length=150)
    phone: str | None = Field(None, max_length=15)
    user_id: uuid.UUID | None = None  # Link to app user if known


class MemberResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    group_id: uuid.UUID
    user_id: uuid.UUID | None
    name: str
    phone: str | None
    is_admin: bool
    joined_at: datetime


class GroupResponse(BaseModel):
    """Returned by GET /groups (list)."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime

    # Computed by service
    member_count: int = 0
    your_net_balance: Decimal = Decimal("0.00")
    # Positive = others owe you. Negative = you owe others.


class GroupDetail(BaseModel):
    """Returned by GET /groups/{id} — full detail with members and expenses."""

    id: uuid.UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    members: list[MemberResponse] = []
    expenses: list["GroupExpenseResponse"] = []


# ── Group Expenses ─────────────────────────────────────────────────────────────

class SplitInput(BaseModel):
    """
    One member's split contribution.
    Used inside GroupExpenseCreate when split_type is EXACT or PERCENTAGE.
    """

    member_id: uuid.UUID
    amount: Decimal | None = Field(None, ge=0, description="For EXACT split")
    percentage: Decimal | None = Field(None, gt=0, le=100, description="For PERCENTAGE split")


class GroupExpenseCreate(BaseModel):
    """POST /groups/{id}/expenses — add a shared expense."""

    total_amount: Decimal = Field(..., gt=0)
    paid_by_member_id: uuid.UUID
    description: str = Field(..., min_length=1, max_length=500)
    split_type: SplitType = SplitType.EQUAL
    payment_mode: PaymentMode | None = None
    expense_date: date = Field(default_factory=date.today)
    notes: str | None = None

    # Required for EXACT and PERCENTAGE splits.
    # For EQUAL: leave empty — service auto-calculates.
    splits: list[SplitInput] = []

    @field_validator("expense_date")  # type: ignore[misc]
    @classmethod
    def date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("Expense date cannot be in the future")
        return v

    @model_validator(mode="after")
    def splits_required_for_non_equal(self) -> "GroupExpenseCreate":
        """EXACT and PERCENTAGE splits need per-member amounts."""
        if self.split_type in (SplitType.EXACT, SplitType.PERCENTAGE) and not self.splits:
            raise ValueError(
                f"splits are required when split_type is {self.split_type.value}. "
                "Provide per-member amounts or percentages."
            )
        return self


class SplitResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    group_expense_id: uuid.UUID
    member_id: uuid.UUID
    amount: Decimal
    percentage: Decimal | None
    is_settled: bool
    settled_at: str | None


class GroupExpenseResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    group_id: uuid.UUID
    paid_by_member_id: uuid.UUID
    description: str
    total_amount: Decimal
    split_type: SplitType
    payment_mode: PaymentMode | None
    expense_date: date
    is_settled: bool
    notes: str | None
    splits: list[SplitResponse] = []
    created_at: datetime


class SettleSplit(BaseModel):
    """PATCH /groups/splits/{split_id}/settle"""

    settlement_note: str | None = Field(
        None,
        max_length=255,
        description="How it was settled, e.g. 'GPay @8pm'",
    )


class MemberBalance(BaseModel):
    """
    One member's net balance in a group.
    Returned by GET /groups/{id}/balances.

    Positive net_balance = others owe this member.
    Negative net_balance = this member owes others.
    """

    member_id: uuid.UUID
    member_name: str
    user_id: uuid.UUID | None
    net_balance: Decimal
    total_paid: Decimal
    total_owed: Decimal


# ── Forward references ─────────────────────────────────────────────────────────
GroupDetail.model_rebuild()


# ── Dues ───────────────────────────────────────────────────────────────────────

from app.models.enums import DueType  # noqa: E402 (after group section)


class DueCreate(BaseModel):
    """POST /dues — add a bilateral due (I owe / they owe)."""

    due_type: DueType
    person_name: str = Field(..., min_length=1, max_length=150)
    person_phone: str | None = Field(None, max_length=15)
    amount: Decimal = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=500)
    due_date: date | None = None


class DueUpdate(BaseModel):
    """PATCH /dues/{id} — partial update."""

    amount: Decimal | None = Field(None, gt=0)
    description: str | None = Field(None, max_length=500)
    due_date: date | None = None
    person_phone: str | None = Field(None, max_length=15)


class DueSettle(BaseModel):
    """POST /dues/{id}/settle — mark a due as settled."""

    settlement_note: str | None = Field(None, max_length=255)


class DueFilter(BaseModel):
    """Query params for GET /dues."""

    due_type: DueType | None = None
    is_settled: bool = False  # Default: show only unsettled dues
    page: int = Field(1, ge=1)
    limit: int = Field(20, ge=1, le=100)


class DueResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    due_type: DueType
    person_name: str
    person_phone: str | None
    amount: Decimal
    description: str
    due_date: date | None
    is_settled: bool
    settled_at: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class DueSummaryResponse(BaseModel):
    """GET /dues/summary — net due position for dashboard widget."""

    i_owe_total: Decimal
    they_owe_total: Decimal
    net_position: Decimal  # Positive = you're owed money. Negative = you owe money.
    unsettled_count: int