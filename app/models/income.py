"""
app/models/income.py — Income Table (Doc1 — Section 4.2)

Every money inflow is one row. Linked to the account that received
the money so account.current_balance can be updated accordingly.

INDEX STRATEGY:
  (user_id, income_date DESC) — primary list query
  (user_id, source)           — income source breakdown analytics
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from sqlalchemy import String, ForeignKey, Numeric, Date, Text, Enum, Index, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import IncomeSource, PaymentMode

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.account import Account


class Income(TimestampMixin, Base):
    __tablename__ = "incomes"

    __table_args__ = (
        Index("idx_incomes_user_date", "user_id", "income_date"),
        Index("idx_incomes_user_source", "user_id", "source"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        # Which account received this money — used for balance tracking
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    income_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[IncomeSource] = mapped_column(
        Enum(IncomeSource, name="income_source_enum", create_type=False),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    payment_mode: Mapped[PaymentMode | None] = mapped_column(
        Enum(PaymentMode, name="payment_mode_enum", create_type=False),
        nullable=True,
        # How was the money received (null if unknown/cash)
    )

    # For LOAN source: link to corresponding due entry
    due_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dues.id", ondelete="SET NULL"),
        nullable=True,
    )

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    user: Mapped["User"] = relationship("User", back_populates="incomes", lazy="noload")