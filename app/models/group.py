"""
app/models/group.py — Group Expenses (Doc1 — Section 5)

TABLES:
  Group         → A named group (trip, flat, event)
  GroupMember   → Who's in the group (may not be app users)
  GroupExpense  → One shared expense for the group
  GroupSplit    → How much each member owes for that expense

DESIGN DECISIONS:
  GroupMember.user_id is nullable because:
    "I went to Goa with 4 friends. Only 2 of them use FinanceFlow."
    The other 2 are tracked by name only. If they join later, user_id
    can be backfilled via PATCH /groups/{id}/members/{id}/link-user.

  VALIDATION RULE (in group_service.py, NOT in the model):
    For EQUAL split: each split.amount = total / member_count
    For PERCENTAGE split: SUM of split.percentage = 100.00
    For EXACT split: SUM of split.amount = group_expense.total_amount
    These are enforced in the service layer, documented here for clarity.

  WHY NOT A DB TRIGGER FOR SPLIT VALIDATION:
    Triggers that validate aggregate conditions (SUM across rows) are complex,
    hard to test, and break in unexpected ways. Service layer validation is
    cleaner, testable, and gives better error messages.
    The doc says: "Enforced at application layer — add in expense_service.py"
"""

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING
from sqlalchemy import String, Boolean, ForeignKey, Numeric, Text, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import SplitType, PaymentMode

if TYPE_CHECKING:
    from app.models.user import User


class Group(TimestampMixin, Base):
    __tablename__ = "groups"

    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False,
    )

    members: Mapped[list["GroupMember"]] = relationship(
        "GroupMember", back_populates="group", cascade="all, delete-orphan", lazy="noload",
    )
    expenses: Mapped[list["GroupExpense"]] = relationship(
        "GroupExpense", back_populates="group", cascade="all, delete-orphan", lazy="noload",
    )


class GroupMember(TimestampMixin, Base):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # user_id is nullable — member may not be an app user
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Name is always stored (in case user_id is null, this is the identifier)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(15), nullable=True)
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    group: Mapped["Group"] = relationship("Group", back_populates="members", lazy="noload")


class GroupExpense(TimestampMixin, Base):
    __tablename__ = "group_expenses"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    paid_by_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_members.id", ondelete="RESTRICT"),
        # RESTRICT: cannot delete a member who paid an expense
        nullable=False,
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    split_type: Mapped[SplitType] = mapped_column(
        Enum(SplitType, name="split_type_enum", create_type=False),
        nullable=False, server_default=SplitType.EQUAL.value,
    )
    payment_mode: Mapped[PaymentMode | None] = mapped_column(
        Enum(PaymentMode, name="payment_mode_enum", create_type=False),
        nullable=True,
    )
    is_settled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    group: Mapped["Group"] = relationship("Group", back_populates="expenses", lazy="noload")
    splits: Mapped[list["GroupSplit"]] = relationship(
        "GroupSplit", back_populates="group_expense", cascade="all, delete-orphan", lazy="noload",
    )


class GroupSplit(TimestampMixin, Base):
    """One row per member per group expense — tracks who owes how much."""
    __tablename__ = "group_splits"

    group_expense_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_expenses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_members.id", ondelete="CASCADE"),
        nullable=False,
    )
    # For PERCENTAGE split: this member's share percentage
    percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    # For EXACT/EQUAL split: this member's exact amount owed
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    is_settled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    settled_at: Mapped[str | None] = mapped_column(String(50), nullable=True)

    group_expense: Mapped["GroupExpense"] = relationship(
        "GroupExpense", back_populates="splits", lazy="noload",
    )