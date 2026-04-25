"""
app/models/enums.py - postgreSQL Enum Definations

WHY ENUMS (From Doc1 - Section 1):
    Enums enforce data intergrity at the DATABASE level.
    Any INSERT with 'GOOGLEPAY' (invalid) is rejectedd by Postgres before
    our application code even runs. This is the first line of defense.

    Without enus, someone could insert "gpay" into payment_mode and the
    DB wouldd happily store it. Then your analytics query breaks because it
    looks for 'UPI' but the row says 'gpay'. Silent data corruption.

HOW SQLALCHEMY ENUMS WORK:
    SQLAlchemy's Enum() maps Python enum mebers to Postgres TYPE values.
    When you store PaymentMode.UPI, the DB stores the string "UPI".
    When you read back "UPI", SQLAlchemy converts it to PaymentMode.UPI.

    The `name=` parameter is the PostgreSQL TYPE name (must match SQL migration).
    The `create_type=False` means Alembi creates the type in the migration,
    not SQLAlchemy at runtime - gives us full control over the SQL.

USAGE IN MODELS:
    from app.models.enums import PaymentMode
    from sqlalchemy import Enum

    payment_mode: Mapped[PaymentMode] = mapped_column(
        Enum(PaymentMode, name="payment_mode_enum", create_type=False),
        nullable=False,
    )

USAGE IN SERVICES/SCHEMAS:
    from app.models.enums import PaymentMode
    if payment.mode == PaymentMode.UPI:
        ...
"""

import enum


# ── 1.1 Payment Mode ──────────────────────────────────────────────────────────

class PaymentMode(str, enum.Enum):
    """
    How a payment was made.

    Why `str, enum.Enum` instenad of just `enum.Enum`:
        str mixin makes the enum JSON-serializable automatically.
        PaymentMode.UPI == "UPI" is True.
        Paydantic can serialize it to "UPI" in respoense without extra config.
        Without str mixin: Pydantic would serialize as {"value": "UPI", "name": "UPI"}

    Indian context notes:
        UPI covers: GPay, PhonePe, Paytm UPI, BHIM — all are UPI at the protocol level
        WALLET covers: Paytm wallet balance, Amazon Pay balance (distinct from UPI)
    """

    CASH = "CASH"
    UPI = "UPI"                # GPay, PhonePe, Paytm UPI, BHIM
    DEBIT_CARD = "DEBIT_CARD"
    CREDIT_CARD = "CREDIT_CARD"
    NET_BANKING = "NET_BANKING"
    CHEQUE = "CHEQUE"
    BANK_TRANSFER = "BANK_TRANSFER"  # NEFT / RTGS / IMPS
    WALLET = "WALLET"          # Paytm wallet, Amazon Pay balance

# ── 1.2 Income Source ─────────────────────────────────────────────────────────

class IncomeSource(str, enum.Enum):
    """
    Where money came from.

    LOAN is included as an income source because when a student borrows
    ₹500 from a friend, it shows up as cash in hand (income). The offset
    is tracked separately as a 'due' (they owe the friend ₹500).
    This design correctly tracks both sides of the transaction.
    """
    PARENT = "PARENT"          # Money from family — most common for students
    SCHOLARSHIP = "SCHOLARSHIP"
    FREELANCE = "FREELANCE"
    PART_TIME = "PART_TIME"
    GIFT = "GIFT"              # Birthday, festival (Diwali, Eid) money
    LOAN = "LOAN"              # Borrowed — creates a corresponding due entry
    REFUND = "REFUND"          # App refund, vendor refund
    INTEREST = "INTEREST"      # FD interest, savings account interest
    OTHER = "OTHER"


# ── 1.3 Bank Account Type ─────────────────────────────────────────────────────

class AccountType(str, enum.Enum):
    """
    Type of bank account.
    Most students have SAVINGS. Current accounts are for businesses.
    NRE/NRO are for NRI users — included for future expansion.
    """
    SAVINGS = "SAVINGS"
    CURRENT = "CURRENT"
    SALARY = "SALARY"
    NRE = "NRE"
    NRO = "NRO"

# ── 1.4 Budget Period ─────────────────────────────────────────────────────────

class BudgetPeriod(str, enum.Enum):
    """
    Time window for a budget.
    MONTHLY is most common — students track monthly allowance.
    WEEKLY is useful for controlling per-week coffee/snack budgets.
    """
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


# ── 1.5 Fixed Deposit Status ──────────────────────────────────────────────────

class FDStatus(str, enum.Enum):
    """
    Lifecycle state of a Fixed Deposit.

    State transitions:
      ACTIVE → MATURED (on maturity_date)
      ACTIVE → PREMATURE_CLOSED (if user breaks FD early)
      MATURED → RENEWED (if auto-renewal is on)
    """
    ACTIVE = "ACTIVE"
    MATURED = "MATURED"
    PREMATURE_CLOSED = "PREMATURE_CLOSED"
    RENEWED = "RENEWED"

# ── 1.6 Account Service ───────────────────────────────────────────────────────

class AccountService(str, enum.Enum):
    """
    Banking services that may be activated on an account.
    Used in account_services table to track which services each account has.
    """
    DEBIT_CARD = "DEBIT_CARD"
    ATM_CARD = "ATM_CARD"
    PASSBOOK = "PASSBOOK"
    CHEQUE_BOOK = "CHEQUE_BOOK"
    NET_BANKING = "NET_BANKING"
    MOBILE_BANKING = "MOBILE_BANKING"
    SMS_ALERTS = "SMS_ALERTS"
    EMAIL_ALERTS = "EMAIL_ALERTS"
    LOCKER = "LOCKER"

# ── 1.7 Document Type ─────────────────────────────────────────────────────────

class DocType(str, enum.Enum):
    """
    Type of document uploaded to Cloudinary.
    Stored in the documents table for filtering and display.
    """
    PASSBOOK_PAGE = "PASSBOOK_PAGE"
    KYC = "KYC"
    FORM = "FORM"
    CHEQUE = "CHEQUE"
    STATEMENT = "STATEMENT"
    OTHER = "OTHER"

# ── 1.7 Due Direction ─────────────────────────────────────────────────────────

class DueType(str, enum.Enum):
    """
    Direction of a due (bilateral debt tracking).

    I_OWE: "I owe Raj ₹500" → reduces my net position
    THEY_OWE: "Priya owes me ₹200" → increases my net position

    The 'net position' (total they_owe minus total i_owe) is shown
    on the dues dashboard as a summary metric.
    """
    I_OWE = "I_OWE"
    THEY_OWE = "THEY_OWE"

# ── 1.7 Group Split Type ──────────────────────────────────────────────────────

class SplitType(str, enum.Enum):
    """
    How a group expense is divided among members.

    EQUAL: Total / number of members (simplest, most common)
    PERCENTAGE: Each member pays X% (flexible for unequal contributions)
    EXACT: Each member's exact amount is specified (most flexible)

    Validation rule (in group_service.py):
      EQUAL: auto-calculated, no per-member amounts needed
      PERCENTAGE: percentages must sum to exactly 100
      EXACT: amounts must sum to exactly total_amount
    """
    EQUAL = "EQUAL"
    PERCENTAGE = "PERCENTAGE"
    EXACT = "EXACT"

# ── Entry Type ────────────────────────────────────────────────────────────────

class EntryType(str, enum.Enum):
    """
    How a statement entry was created.

    MANUAL: User typed it in
    SCANNED: Extracted by Gemini OCR from passbook photo
    IMPORTED: Future — CSV import from bank statement download
    """
    MANUAL = "MANUAL"
    SCANNED = "SCANNED"
    IMPORTED = "IMPORTED"