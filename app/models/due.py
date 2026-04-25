"""
app/models/due.py — Due Table (Doc1 — Section 5.5)
Bilateral debt tracking outside of groups.
"I owe Raj ₹500" or "Priya owes me ₹200".
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from sqlalchemy import String, Boolean, ForeignKey, Numeric, Date, Text, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import DueType

if TYPE_CHECKING:
    from app.models.user import User


class Due(TimestampMixin, Base):
    __tablename__ = "dues"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    due_type: Mapped[DueType] = mapped_column(
        Enum(DueType, name="due_type_enum", create_type=False),
        nullable=False,
    )

    # The other person — may not be an app user
    person_name: Mapped[str] = mapped_column(String(150), nullable=False)
    person_phone: Mapped[str | None] = mapped_column(String(15), nullable=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_settled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False, index=True,
    )
    settled_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="dues", lazy="noload")