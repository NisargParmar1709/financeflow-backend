"""
app/models/expense.py — Expense Table

WHY THIS IS THE MOST IMPORTANT MODEL (Doc1 — Section 4.1):
  Every rupee a user spends creates one row here.
  A user making 5 transactions/day for 1 year = 1,825 rows just for them.
  At 1000 users that's 1.8 million rows in year 1. Performance matters.

COMPOSITE INDEXES (from Doc1 — Section 4.1):
  These are CRITICAL for query performance. Without them, every
  "show my expenses this month" query is a full table scan.

  Index 1: (user_id, expense_date DESC)
    → Primary pattern: show user's expenses sorted by date
    → Used by: GET /api/v1/expenses?sort=date

  Index 2: (user_id, category_id, expense_date DESC)
    → Category breakdown analytics
    → Used by: GET /api/v1/analytics/category-breakdown

  Index 3: (user_id, payment_mode)
    → Payment mode analysis (how much via UPI vs cash)
    → Used by: GET /api/v1/analytics/payment-modes

  Index 4: (user_id, expense_date DESC) WHERE expense_date >= last 2 years
    → Partial index: most queries are for recent data
    → Smaller index = faster lookup for 95% of queries

NOTE: These indexes are defined in the Alembic migration file, not here.
      SQLAlchemy Index() objects in the model are an alternative — we use
      __table_args__ for the composite ones that need DESC ordering.

RECEIPT PHOTO:
  receipt_url is a Cloudinary CDN URL.
  The actual file lives on Cloudinary. We store only the URL reference.
  On expense deletion: the service also calls Cloudinary delete API.

RECURRING EXPENSE:
  is_recurring=True marks expenses that repeat on a schedule.
  The scheduler (Celery or APScheduler) reads recurring expenses and
  creates new expense rows automatically each period.
  (Implementation: workers/recurring_worker.py — future step)
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from sqlalchemy import (
    String, Boolean, ForeignKey, Numeric, Date,
    Text, Enum, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import PaymentMode, EntryType

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.category import Category, Subcategory
    from app.models.account import Account
    from app.models.group import GroupExpense


class Expense(TimestampMixin, Base):
    __tablename__ = "expenses"

    # ── Composite Indexes (performance critical) ───────────────────────────────
    __table_args__ = (
        # Primary query pattern: user's expenses in date range, sorted
        Index("idx_expenses_user_date", "user_id", "expense_date"),
        # Category breakdown
        Index("idx_expenses_user_cat", "user_id", "category_id", "expense_date"),
        # Payment mode analysis
        Index("idx_expenses_user_mode", "user_id", "payment_mode"),
    )

    # ── Foreign Keys ───────────────────────────────────────────────────────────
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        # RESTRICT: cannot delete a category that has expenses
        # User must deactivate the category instead
        nullable=False,
        index=True,
    )
    subcategory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subcategories.id", ondelete="SET NULL"),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        # SET NULL: if account is deleted, expense stays but loses account link
    )
    group_expense_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("group_expenses.id", ondelete="SET NULL"),
        nullable=True,
        # Links this personal expense to the group expense it originated from
    )

    # ── Core Fields ────────────────────────────────────────────────────────────
    amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        # Numeric(15, 2): up to 9,999,999,999,999.99
        # Student expenses won't exceed this. Ever.
    )
    expense_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        # Date (not DateTime): we track the date of expense, not the exact time
        # A user may log a yesterday's expense today — Date is more accurate
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    payment_mode: Mapped[PaymentMode] = mapped_column(
        Enum(PaymentMode, name="payment_mode_enum", create_type=False),
        nullable=False,
    )

    # ── Receipt ────────────────────────────────────────────────────────────────
    # Cloudinary URL. Null until user uploads a photo.
    receipt_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cloudinary public_id for deletion (separate from URL)
    receipt_public_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Entry Tracking ─────────────────────────────────────────────────────────
    entry_type: Mapped[EntryType] = mapped_column(
        Enum(EntryType, name="entry_type_enum", create_type=False),
        nullable=False,
        server_default=EntryType.MANUAL.value,
    )

    # ── Recurring Expense ──────────────────────────────────────────────────────
    is_recurring: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    # "MONTHLY", "WEEKLY", "DAILY" — null if not recurring
    recurrence_period: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ── Soft Delete ────────────────────────────────────────────────────────────
    # We soft-delete expenses — they remain in DB for audit/analytics
    # but are hidden from normal queries with WHERE is_deleted = FALSE
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    user: Mapped["User"] = relationship(
        "User", back_populates="expenses", lazy="noload",
    )
    category: Mapped["Category"] = relationship(
        "Category", back_populates="expenses", lazy="noload",
    )
    subcategory: Mapped["Subcategory | None"] = relationship(
        "Subcategory", back_populates="expenses", lazy="noload",
    )