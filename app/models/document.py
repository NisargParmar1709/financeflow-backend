"""
app/models/document.py — Document & StatementEntry Tables (Doc1 — Section 3.4 & 3.5)

Document: stores Cloudinary metadata (URL, public_id). Actual file is on CDN.
StatementEntry: individual passbook/bank statement rows (manual or OCR).
"""

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING
from sqlalchemy import String, Boolean, ForeignKey, Text, Enum, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import DocType, EntryType

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.account import Account


class Document(TimestampMixin, Base):
    __tablename__ = "documents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    doc_type: Mapped[DocType] = mapped_column(
        Enum(DocType, name="doc_type_enum", create_type=False),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Cloudinary references — URL for display, public_id for deletion
    cloudinary_url: Mapped[str] = mapped_column(Text, nullable=False)
    cloudinary_public_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
    )

    account: Mapped["Account | None"] = relationship(
        "Account", back_populates="documents", lazy="noload",
    )


class StatementEntry(TimestampMixin, Base):
    """
    Individual passbook / bank statement row.
    CONSTRAINT: exactly one of debit_amount or credit_amount must be non-null.
    This is enforced by a CHECK constraint in the Alembic migration.
    """
    __tablename__ = "statement_entries"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    transaction_date: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    debit_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    credit_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    entry_type: Mapped[EntryType] = mapped_column(
        Enum(EntryType, name="entry_type_enum", create_type=False),
        nullable=False, server_default=EntryType.MANUAL.value,
    )
    reference_number: Mapped[str | None] = mapped_column(String(100), nullable=True)

    account: Mapped["Account"] = relationship(
        "Account", back_populates="statement_entries", lazy="noload",
    )