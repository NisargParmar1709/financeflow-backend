"""
app/models/account.py — Bank Account Models

TABLES IN THIS FILE (Doc1 — Section 3):
  Account        → Core bank account record (3.1)
  Branch         → Bank branch info, user-created (2.4)
  AccountService → Which banking services are active (3.2)
  FixedDeposit   → FD records linked to accounts (3.3)

CRITICAL SECURITY RULE (Doc1 — Section 3.1):
  account_last4 stores ONLY the last 4 digits of the account number.
  NEVER store the full account number. This is enforced at:
    1. This model: field name is 'account_last4', max length 4
    2. Schema layer: ExpenseCreate validator strips to last 4
    3. Code review: any PR that stores >4 digits is rejected
    4. This docstring: future developers see the rule immediately

WHY NOT STORE FULL ACCOUNT NUMBER:
  Even in encrypted form, full account numbers are a liability:
  - Regulatory: RBI guidelines prohibit storing full account numbers in unmasked form
  - Security: a DB breach exposing account numbers is a major incident
  - Unnecessary: last 4 digits is enough for the user to identify their account
    ("which account? the one ending in 4521")

FIXED DEPOSIT AUTO-CALCULATION:
  maturity_amount is stored (not calculated on read) because:
    - Interest rate is fixed at FD creation time
    - Postgres GENERATED COLUMN would recalculate on every read
    - We calculate once at creation and store — simpler and correct
  Formula: principal × (1 + rate/100 × tenure_days/365) for simple interest
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from sqlalchemy import (
    String, Boolean, ForeignKey, Numeric, Date,
    Integer, Text, Enum, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import AccountType, AccountService, FDStatus

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.document import Document
    from app.models.statement import StatementEntry
    from app.models.income import Income


class Branch(TimestampMixin, Base):
    """
    Bank branch records. User-created, purely informational.
    No connection to any bank API — user types in the details.
    """
    __tablename__ = "branches"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bank_name: Mapped[str] = mapped_column(String(100), nullable=False)
    branch_name: Mapped[str] = mapped_column(String(150), nullable=False)
    ifsc_code: Mapped[str | None] = mapped_column(
        String(11),  # IFSC is always exactly 11 chars
        nullable=True,
    )
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(15), nullable=True)

    # ── Relationships ──────────────────────────────────────────────────────────
    accounts: Mapped[list["Account"]] = relationship(
        "Account", back_populates="branch", lazy="noload",
    )


class Account(TimestampMixin, Base):
    """
    Core bank account record.

    SECURITY: account_last4 is max 4 chars. See module docstring.
    BALANCE: current_balance is maintained by the app, not synced from bank.
             Updated on every expense/income transaction linked to this account.
    """
    __tablename__ = "accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Account Identity ───────────────────────────────────────────────────────
    account_name: Mapped[str] = mapped_column(
        String(150), nullable=False,
        # e.g. "SBI Savings - Home Branch" — user's display name for the account
    )
    bank_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # ONLY last 4 digits. Enforced by schema validator.
    account_last4: Mapped[str] = mapped_column(
        String(4),
        nullable=False,
    )

    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, name="account_type_enum", create_type=False),
        nullable=False,
        server_default=AccountType.SAVINGS.value,
    )

    # ── Financial State ────────────────────────────────────────────────────────
    # Decimal(15, 2): supports up to 9,999,999,999,999.99 (15 digits, 2 decimal)
    # Why Decimal not Float: Float has precision errors for money
    current_balance: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        nullable=False,
        server_default="0.00",
    )
    min_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2), nullable=True,
        # Alert threshold: notify when balance drops below this
    )

    # ── Account Settings ───────────────────────────────────────────────────────
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
        # Primary account is the default for new expenses/incomes
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False,
    )

    # Account opening date — used for tenure calculations
    opened_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # ── Relationships ──────────────────────────────────────────────────────────
    branch: Mapped["Branch | None"] = relationship(
        "Branch", back_populates="accounts", lazy="noload",
    )
    services: Mapped[list["AccountServiceRecord"]] = relationship(
        "AccountServiceRecord",
        back_populates="account",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    fixed_deposits: Mapped[list["FixedDeposit"]] = relationship(
        "FixedDeposit",
        back_populates="account",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    documents: Mapped[list["Document"]] = relationship(
        "Document",
        back_populates="account",
        lazy="noload",
    )
    statement_entries: Mapped[list["StatementEntry"]] = relationship(
        "StatementEntry",
        back_populates="account",
        cascade="all, delete-orphan",
        lazy="noload",
    )


class AccountServiceRecord(TimestampMixin, Base):
    """
    Tracks which banking services are active on an account.
    Many-to-many: one account can have multiple services.
    One row per service per account.
    """
    __tablename__ = "account_services"

    __table_args__ = (
        # One record per service per account — no duplicates
        UniqueConstraint("account_id", "service_type", name="uq_account_service"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service_type: Mapped[AccountService] = mapped_column(
        Enum(AccountService, name="account_service_enum", create_type=False),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False,
    )
    activated_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Relationships ──────────────────────────────────────────────────────────
    account: Mapped["Account"] = relationship(
        "Account", back_populates="services", lazy="noload",
    )


class FixedDeposit(TimestampMixin, Base):
    """
    Fixed Deposit linked to a bank account.

    maturity_amount is pre-calculated at creation and stored.
    formula: principal × (1 + rate/100 × days/365) [simple interest]
    For compound interest (some banks): principal × (1 + rate/400)^(quarters)
    The service layer handles the formula — model just stores the result.
    """
    __tablename__ = "fixed_deposits"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── FD Details ─────────────────────────────────────────────────────────────
    fd_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    principal_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    interest_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False,  # e.g. 7.25 for 7.25% per annum
    )
    tenure_days: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    maturity_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Pre-calculated at creation. Do not update manually.
    maturity_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)

    status: Mapped[FDStatus] = mapped_column(
        Enum(FDStatus, name="fd_status_enum", create_type=False),
        nullable=False,
        server_default=FDStatus.ACTIVE.value,
        index=True,
    )

    # True if the bank auto-renews at maturity
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Relationships ──────────────────────────────────────────────────────────
    account: Mapped["Account"] = relationship(
        "Account", back_populates="fixed_deposits", lazy="noload",
    )