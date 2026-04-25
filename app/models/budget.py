"""
app/models/budget.py — Budget Table (Doc1 — Section 4.3)

Per-category spending limits. Checked on every expense creation.

UNIQUE CONSTRAINT (Doc1):
  One active budget per user per category per period.
  A user cannot have two active MONTHLY Food budgets simultaneously.
  The UNIQUE INDEX enforces this at the DB level:
    WHERE is_active = TRUE AND subcategory_id IS NULL

  Why partial index (WHERE is_active = TRUE):
    When a budget is deactivated (is_active = FALSE), the user should be
    able to create a new budget for the same category. A standard UNIQUE
    constraint would block this. A partial index only applies to active budgets.

BUDGET CHECK IN expense_service.py:
  On every POST /api/v1/expenses:
    1. Find active budget for expense.category_id and current period
    2. Sum all expenses for that category in the current period
    3. If sum + new_amount > budget.limit_amount: raise BudgetExceededException
    4. If sum + new_amount > 80% of limit: create a WARNING notification

  The 80% threshold is configurable via budget.alert_threshold_percent.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from sqlalchemy import Boolean, ForeignKey, Numeric, Date, Enum, Index, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import BudgetPeriod

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.category import Category, Subcategory


class Budget(TimestampMixin, Base):
    __tablename__ = "budgets"

    __table_args__ = (
        # Partial unique index: one active budget per user/category/period
        # Defined in migration as CREATE UNIQUE INDEX idx_budgets_unique
        # ON budgets(user_id, category_id, period) WHERE is_active = TRUE
        Index("idx_budgets_user_cat", "user_id", "category_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    subcategory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subcategories.id", ondelete="CASCADE"),
        nullable=True,
        # If set: budget applies to just this subcategory (e.g. Food > Chai)
        # If null: budget applies to the whole category (all Food)
    )

    limit_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    period: Mapped[BudgetPeriod] = mapped_column(
        Enum(BudgetPeriod, name="budget_period_enum", create_type=False),
        nullable=False,
        server_default=BudgetPeriod.MONTHLY.value,
    )

    # Alert when spending reaches X% of limit (default 80%)
    alert_threshold_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="80",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False,
    )

    # Optional: if set, budget only applies to this date range
    # Useful for "Diwali special budget" or "semester budget"
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="budgets", lazy="noload")
    category: Mapped["Category"] = relationship("Category", back_populates="budgets", lazy="noload")