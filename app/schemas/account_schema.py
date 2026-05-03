"""
app/schemas/account_schema.py — Bank Account, Branch, FD, Service, Statement Schemas

COVERS (Doc2 — Section 3.5, 3.6, 3.7):
  /accounts          → AccountCreate, AccountUpdate, AccountResponse, AccountDetail
  /accounts/{id}/services    → ServiceCreate, ServiceResponse
  /accounts/{id}/fds         → FDCreate, FDUpdate, FDResponse
  /accounts/{id}/statements  → StatementEntryCreate, StatementEntryResponse
  /accounts/{id}/documents   → DocumentResponse (upload handled separately)
  /branches          → BranchCreate, BranchResponse

CRITICAL SECURITY RULE (Doc1 — Section 3.1):
  account_last4 is ONLY 4 digits.
  The validator below strips all non-digit characters and takes the last 4.
  If fewer than 4 digits are provided → ValidationError.

  WHY: Storing full account numbers (even encrypted) is a regulatory risk.
  Last 4 digits are enough to identify "which account ending in 4521".
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import AccountType, AccountService, FDStatus, DocType, EntryType


# ── Branch ─────────────────────────────────────────────────────────────────────

class BranchCreate(BaseModel):
    """POST /branches — user creates a bank branch entry."""

    bank_name: str = Field(..., max_length=100)
    branch_name: str = Field(..., max_length=150)
    ifsc_code: str | None = Field(None, description="11-char IFSC code, e.g. SBIN0000477")
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    address: str | None = None
    phone: str | None = Field(None, max_length=15)

    @field_validator("ifsc_code")
    @classmethod
    def validate_ifsc(cls, v: str | None) -> str | None:
        """IFSC format: 4 letters + '0' + 6 alphanumeric chars = 11 total."""
        if v is None:
            return v
        v = v.upper().strip()
        if len(v) != 11:
            raise ValueError("IFSC code must be exactly 11 characters")
        if not v[:4].isalpha():
            raise ValueError("First 4 characters of IFSC must be letters")
        if v[4] != "0":
            raise ValueError("5th character of IFSC must be '0'")
        return v


class BranchResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    bank_name: str
    branch_name: str
    ifsc_code: str | None
    city: str | None
    state: str | None
    address: str | None
    phone: str | None
    created_at: datetime


# ── Account ────────────────────────────────────────────────────────────────────

class AccountCreate(BaseModel):
    """POST /accounts — create a new bank account."""

    account_name: str = Field(..., max_length=150, description="User's display name, e.g. 'SBI Savings'")
    bank_name: str = Field(..., max_length=100)
    account_last4: str = Field(..., description="Last 4 digits of account number ONLY")
    account_type: AccountType = AccountType.SAVINGS

    current_balance: Decimal = Field(Decimal("0.00"), ge=0)
    min_balance: Decimal | None = Field(None, ge=0)

    branch_id: uuid.UUID | None = None
    is_primary: bool = False
    opened_date: date | None = None

    @field_validator("account_last4")
    @classmethod
    def validate_last4(cls, v: str) -> str:
        """
        SECURITY RULE: extract only the last 4 digits.
        If user pastes full account number '123456789012', we strip to '9012'.
        If fewer than 4 digits → validation error.
        """
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) < 4:
            raise ValueError("Please provide at least the last 4 digits of your account number")
        return digits[-4:]  # Always last 4


class AccountUpdate(BaseModel):
    """PATCH /accounts/{id} — partial update."""

    account_name: str | None = Field(None, max_length=150)
    bank_name: str | None = Field(None, max_length=100)
    account_type: AccountType | None = None
    min_balance: Decimal | None = Field(None, ge=0)
    branch_id: uuid.UUID | None = None
    is_primary: bool | None = None
    opened_date: date | None = None
    is_active: bool | None = None


class BalanceUpdate(BaseModel):
    """POST /accounts/{id}/balance — manual balance update."""

    balance: Decimal = Field(..., ge=0, description="New current balance in INR")
    as_of_date: date = Field(
        default_factory=date.today,
        description="Date when this balance was observed",
    )

    @field_validator("as_of_date")
    @classmethod
    def date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("Balance date cannot be in the future")
        return v


class AccountResponse(BaseModel):
    """Account as returned by GET /accounts (list view)."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    account_name: str
    bank_name: str
    account_last4: str
    account_type: AccountType
    current_balance: Decimal
    min_balance: Decimal | None
    is_primary: bool
    is_active: bool
    opened_date: date | None
    created_at: datetime
    updated_at: datetime


class AccountDetail(AccountResponse):
    """
    Full account detail — GET /accounts/{id}.
    Extends AccountResponse with related objects.
    Service populates these via selectinload() queries.
    """

    branch: BranchResponse | None = None
    services: list["ServiceResponse"] = []
    fixed_deposits: list["FDResponse"] = []
    recent_statements: list["StatementEntryResponse"] = []
    documents: list["DocumentResponse"] = []


# ── Account Services ───────────────────────────────────────────────────────────

class ServiceCreate(BaseModel):
    """POST /accounts/{id}/services — add a banking service."""

    service_type: AccountService
    is_active: bool = True
    activated_date: date | None = None
    notes: str | None = Field(None, max_length=500)


class ServiceUpdate(BaseModel):
    """PATCH /accounts/services/{service_id}."""

    is_active: bool | None = None
    activated_date: date | None = None
    notes: str | None = Field(None, max_length=500)


class ServiceResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    account_id: uuid.UUID
    service_type: AccountService
    is_active: bool
    activated_date: date | None
    notes: str | None
    created_at: datetime


# ── Fixed Deposits ─────────────────────────────────────────────────────────────

class FDCreate(BaseModel):
    """POST /accounts/{id}/fds — create a fixed deposit."""

    principal_amount: Decimal = Field(..., gt=0)
    interest_rate: Decimal = Field(..., gt=0, le=Decimal("50.00"), description="Annual rate, e.g. 7.25")
    tenure_days: int = Field(..., gt=0, description="Duration in days")
    start_date: date
    auto_renew: bool = False
    fd_number: str | None = Field(None, max_length=50, description="FD certificate number")
    notes: str | None = None

    @field_validator("start_date")
    @classmethod
    def date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("FD start date cannot be in the future")
        return v


class FDUpdate(BaseModel):
    """PATCH /fds/{id} — typically used to mark FD as matured/closed."""

    status: FDStatus | None = None
    actual_received: Decimal | None = Field(None, ge=0)
    auto_renew: bool | None = None
    notes: str | None = None

    
class FDResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    account_id: uuid.UUID
    fd_number: str | None
    principal_amount: Decimal
    interest_rate: Decimal
    tenure_days: int
    start_date: date
    maturity_date: date
    maturity_amount: Decimal
    actual_received: Decimal | None
    status: FDStatus
    auto_renew: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def days_to_maturity(self) -> int:
        """Computed field: how many days until maturity (can be negative if past)."""
        return (self.maturity_date - date.today()).days


# ── Statement Entries ──────────────────────────────────────────────────────────

class StatementEntryCreate(BaseModel):
    """
    POST /accounts/{id}/statements — manual passbook entry.

    CONSTRAINT: exactly one of debit_amount or credit_amount must be provided.
    This mirrors the DB CHECK constraint in the migration.
    """

    transaction_date: str = Field(..., description="Date as string, e.g. '15 Jan 2025' or '2025-01-15'")
    description: str = Field(..., max_length=500)
    debit_amount: Decimal | None = Field(None, ge=0)
    credit_amount: Decimal | None = Field(None, ge=0)
    balance_after: Decimal | None = Field(None, ge=0)
    reference_number: str | None = Field(None, max_length=100)
    entry_type: EntryType = EntryType.MANUAL

    @model_validator(mode="after")
    def exactly_one_of_debit_credit(self) -> "StatementEntryCreate":
        """Mirror the DB CHECK constraint at the schema layer for early feedback."""
        has_debit = self.debit_amount is not None
        has_credit = self.credit_amount is not None
        if has_debit == has_credit:  # both None or both provided
            raise ValueError("Provide exactly one of debit_amount or credit_amount, not both or neither")
        return self


class StatementEntryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    account_id: uuid.UUID
    transaction_date: str
    description: str
    debit_amount: Decimal | None
    credit_amount: Decimal | None
    balance_after: Decimal | None
    reference_number: str | None
    entry_type: EntryType
    created_at: datetime


# ── Documents ──────────────────────────────────────────────────────────────────

class DocumentResponse(BaseModel):
    """
    Document metadata returned after upload or listing.
    The actual file lives on Cloudinary — we only return the URL.
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    account_id: uuid.UUID | None
    doc_type: DocType
    title: str
    description: str | None
    cloudinary_url: str
    file_size_bytes: int | None
    mime_type: str | None
    is_processed: bool
    created_at: datetime


# ── Forward references ─────────────────────────────────────────────────────────
AccountDetail.model_rebuild()