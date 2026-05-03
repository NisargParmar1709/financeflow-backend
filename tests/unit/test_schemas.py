"""
tests/unit/test_schemas.py — Schema Validation Tests

Tests all business rules enforced at the Pydantic schema layer.
These tests need NO database, NO Redis, NO Clerk — pure Python.

WHY SCHEMA TESTS MATTER:
  Schema validation is the first line of defense (Video 9).
  If a bad value slips past the schema, it reaches the service layer
  and causes harder-to-debug errors (DB constraint violations, logic errors).
  Schema tests catch these before any service code runs.

COVERAGE:
  - ExpenseCreate: amount, date, payment_mode/account cross-field rule
  - AccountCreate: last-4-digit security rule
  - BudgetCreate: alert threshold range
  - StatementEntryCreate: debit XOR credit constraint
  - PaginationMeta: derived fields math
  - GroupExpenseCreate: split type validations
  - DueCreate: basic validation
  - CategoryCreate: hex color validation
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.enums import PaymentMode, BudgetPeriod, SplitType, DueType
from app.schemas.common import PaginationMeta, success_response, deleted_response
from app.schemas.expense_schema import ExpenseCreate, ExpenseUpdate, ExpenseFilter
from app.schemas.account_schema import AccountCreate, BalanceUpdate, StatementEntryCreate
from app.schemas.budget_schema import BudgetCreate, BudgetUpdate
from app.schemas.group_schema import GroupExpenseCreate, SplitInput, DueCreate
from app.schemas.category_schema import CategoryCreate, CategoryUpdate

VALID_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ── PaginationMeta ─────────────────────────────────────────────────────────────

class TestPaginationMeta:
    def test_basic_calculation(self):
        meta = PaginationMeta.build(page=1, limit=20, total=45)
        assert meta.pages == 3
        assert meta.has_next is True
        assert meta.has_prev is False

    def test_last_page(self):
        meta = PaginationMeta.build(page=3, limit=20, total=45)
        assert meta.has_next is False
        assert meta.has_prev is True

    def test_single_page(self):
        meta = PaginationMeta.build(page=1, limit=20, total=10)
        assert meta.pages == 1
        assert meta.has_next is False
        assert meta.has_prev is False

    def test_exact_fit(self):
        meta = PaginationMeta.build(page=2, limit=10, total=20)
        assert meta.pages == 2
        assert meta.has_next is False
        assert meta.has_prev is True

    def test_zero_total(self):
        meta = PaginationMeta.build(page=1, limit=20, total=0)
        assert meta.pages == 0
        assert meta.has_next is False
        assert meta.has_prev is False

    def test_middle_page(self):
        meta = PaginationMeta.build(page=5, limit=10, total=100)
        assert meta.pages == 10
        assert meta.has_next is True
        assert meta.has_prev is True


# ── Common Response Helpers ────────────────────────────────────────────────────

class TestResponseHelpers:
    def test_success_response_structure(self):
        result = success_response(data={"id": "abc"}, message="Created")
        assert result["success"] is True
        assert result["message"] == "Created"
        assert result["data"] == {"id": "abc"}
        assert "meta" not in result

    def test_success_response_with_meta(self):
        meta = PaginationMeta.build(page=1, limit=10, total=5)
        result = success_response(data=[], meta=meta)
        assert result["meta"]["total"] == 5
        assert result["meta"]["page"] == 1

    def test_deleted_response(self):
        result = deleted_response("Expense deleted")
        assert result["success"] is True
        assert result["message"] == "Expense deleted"


# ── ExpenseCreate ──────────────────────────────────────────────────────────────

class TestExpenseCreate:
    def _valid(self, **overrides):
        base = {
            "amount": Decimal("150.00"),
            "category_id": VALID_UUID,
            "payment_mode": PaymentMode.CASH,
        }
        return {**base, **overrides}

    def test_valid_cash_expense(self):
        e = ExpenseCreate(**self._valid())
        assert e.amount == Decimal("150.00")
        assert e.payment_mode == PaymentMode.CASH
        assert e.account_id is None

    def test_valid_upi_with_account(self):
        e = ExpenseCreate(**self._valid(
            payment_mode=PaymentMode.UPI,
            account_id=VALID_UUID,
        ))
        assert e.account_id == VALID_UUID

    def test_upi_without_account_fails(self):
        with pytest.raises(ValidationError) as exc_info:
            ExpenseCreate(**self._valid(payment_mode=PaymentMode.UPI))
        assert "account_id" in str(exc_info.value).lower() or "non-cash" in str(exc_info.value).lower()

    def test_card_without_account_fails(self):
        with pytest.raises(ValidationError):
            ExpenseCreate(**self._valid(payment_mode=PaymentMode.DEBIT_CARD))

    def test_future_date_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ExpenseCreate(**self._valid(expense_date=date.today() + timedelta(days=1)))
        assert "future" in str(exc_info.value).lower()

    def test_today_date_accepted(self):
        e = ExpenseCreate(**self._valid(expense_date=date.today()))
        assert e.expense_date == date.today()

    def test_past_date_accepted(self):
        e = ExpenseCreate(**self._valid(expense_date=date(2024, 6, 15)))
        assert e.expense_date == date(2024, 6, 15)

    def test_very_old_date_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ExpenseCreate(**self._valid(expense_date=date(2019, 12, 31)))
        assert "2020" in str(exc_info.value)

    def test_zero_amount_rejected(self):
        with pytest.raises(ValidationError):
            ExpenseCreate(**self._valid(amount=Decimal("0")))

    def test_negative_amount_rejected(self):
        with pytest.raises(ValidationError):
            ExpenseCreate(**self._valid(amount=Decimal("-50")))

    def test_amount_over_max_rejected(self):
        with pytest.raises(ValidationError):
            ExpenseCreate(**self._valid(amount=Decimal("9999999")))

    def test_recurring_without_period_fails(self):
        with pytest.raises(ValidationError) as exc_info:
            ExpenseCreate(**self._valid(is_recurring=True))
        assert "recurrence_period" in str(exc_info.value).lower()

    def test_recurring_with_period_accepted(self):
        e = ExpenseCreate(**self._valid(is_recurring=True, recurrence_period="MONTHLY"))
        assert e.is_recurring is True
        assert e.recurrence_period == "MONTHLY"

    def test_description_max_length(self):
        with pytest.raises(ValidationError):
            ExpenseCreate(**self._valid(description="x" * 501))

    def test_default_date_is_today(self):
        e = ExpenseCreate(**self._valid())
        assert e.expense_date == date.today()


# ── AccountCreate ──────────────────────────────────────────────────────────────

class TestAccountCreate:
    def _valid(self, **overrides):
        base = {
            "account_name": "SBI Savings",
            "bank_name": "State Bank of India",
            "account_last4": "1234",
        }
        return {**base, **overrides}

    def test_valid_account(self):
        a = AccountCreate(**self._valid())
        assert a.account_last4 == "1234"

    def test_full_account_number_stripped_to_last4(self):
        """SECURITY RULE: only last 4 digits stored regardless of input."""
        a = AccountCreate(**self._valid(account_last4="123456789012"))
        assert a.account_last4 == "9012"

    def test_exactly_4_digits_accepted(self):
        a = AccountCreate(**self._valid(account_last4="5678"))
        assert a.account_last4 == "5678"

    def test_fewer_than_4_digits_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            AccountCreate(**self._valid(account_last4="12"))
        assert "4 digits" in str(exc_info.value).lower() or "last 4" in str(exc_info.value).lower()

    def test_account_number_with_spaces_stripped(self):
        a = AccountCreate(**self._valid(account_last4="1234 5678"))
        assert a.account_last4 == "5678"


# ── StatementEntryCreate ───────────────────────────────────────────────────────

class TestStatementEntryCreate:
    def _valid_debit(self, **overrides):
        base = {
            "transaction_date": "15 Jan 2025",
            "description": "ATM withdrawal",
            "debit_amount": Decimal("500.00"),
            "balance_after": Decimal("4500.00"),
        }
        return {**base, **overrides}

    def _valid_credit(self, **overrides):
        base = {
            "transaction_date": "15 Jan 2025",
            "description": "UPI credit",
            "credit_amount": Decimal("1000.00"),
            "balance_after": Decimal("6000.00"),
        }
        return {**base, **overrides}

    def test_valid_debit_entry(self):
        e = StatementEntryCreate(**self._valid_debit())
        assert e.debit_amount == Decimal("500.00")
        assert e.credit_amount is None

    def test_valid_credit_entry(self):
        e = StatementEntryCreate(**self._valid_credit())
        assert e.credit_amount == Decimal("1000.00")
        assert e.debit_amount is None

    def test_both_debit_and_credit_rejected(self):
        """DB constraint: exactly one of debit/credit."""
        with pytest.raises(ValidationError) as exc_info:
            StatementEntryCreate(**self._valid_debit(credit_amount=Decimal("100")))
        assert "exactly one" in str(exc_info.value).lower()

    def test_neither_debit_nor_credit_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            StatementEntryCreate(
                transaction_date="15 Jan 2025",
                description="test",
                balance_after=Decimal("1000"),
            )
        assert "exactly one" in str(exc_info.value).lower()


# ── BudgetCreate ───────────────────────────────────────────────────────────────

class TestBudgetCreate:
    def _valid(self, **overrides):
        base = {
            "category_id": VALID_UUID,
            "limit_amount": Decimal("3000.00"),
            "period": BudgetPeriod.MONTHLY,
        }
        return {**base, **overrides}

    def test_valid_budget(self):
        b = BudgetCreate(**self._valid())
        assert b.limit_amount == Decimal("3000.00")
        assert b.alert_threshold_percent == 80  # default

    def test_zero_limit_rejected(self):
        with pytest.raises(ValidationError):
            BudgetCreate(**self._valid(limit_amount=Decimal("0")))

    def test_alert_threshold_below_10_rejected(self):
        with pytest.raises(ValidationError):
            BudgetCreate(**self._valid(alert_threshold_percent=9))

    def test_alert_threshold_above_99_rejected(self):
        with pytest.raises(ValidationError):
            BudgetCreate(**self._valid(alert_threshold_percent=100))

    def test_alert_threshold_boundaries(self):
        b1 = BudgetCreate(**self._valid(alert_threshold_percent=10))
        assert b1.alert_threshold_percent == 10
        b2 = BudgetCreate(**self._valid(alert_threshold_percent=99))
        assert b2.alert_threshold_percent == 99


# ── CategoryCreate ─────────────────────────────────────────────────────────────

class TestCategoryCreate:
    def test_valid_hex_color(self):
        c = CategoryCreate(name="Transport", color="#2E90FA")
        assert c.color == "#2E90FA"

    def test_hex_color_lowercase_normalized(self):
        c = CategoryCreate(name="Food", color="#f04438")
        assert c.color == "#F04438"

    def test_invalid_hex_color_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            CategoryCreate(name="Food", color="red")
        assert "hex" in str(exc_info.value).lower()

    def test_short_hex_rejected(self):
        with pytest.raises(ValidationError):
            CategoryCreate(name="Food", color="#FFF")

    def test_no_hash_prefix_rejected(self):
        with pytest.raises(ValidationError):
            CategoryCreate(name="Food", color="F04438")

    def test_none_color_accepted(self):
        c = CategoryCreate(name="Food", color=None)
        assert c.color is None

    def test_name_required(self):
        with pytest.raises(ValidationError):
            CategoryCreate(name="")


# ── GroupExpenseCreate ─────────────────────────────────────────────────────────

class TestGroupExpenseCreate:
    def _valid(self, **overrides):
        base = {
            "total_amount": Decimal("900.00"),
            "paid_by_member_id": VALID_UUID,
            "description": "Dinner",
            "split_type": SplitType.EQUAL,
        }
        return {**base, **overrides}

    def test_valid_equal_split(self):
        g = GroupExpenseCreate(**self._valid())
        assert g.split_type == SplitType.EQUAL
        assert g.splits == []

    def test_exact_split_without_splits_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            GroupExpenseCreate(**self._valid(split_type=SplitType.EXACT))
        assert "splits" in str(exc_info.value).lower()

    def test_percentage_split_without_splits_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            GroupExpenseCreate(**self._valid(split_type=SplitType.PERCENTAGE))
        assert "splits" in str(exc_info.value).lower()

    def test_exact_split_with_splits_accepted(self):
        splits = [
            SplitInput(member_id=VALID_UUID, amount=Decimal("450.00")),
            SplitInput(member_id=uuid.uuid4(), amount=Decimal("450.00")),
        ]
        g = GroupExpenseCreate(**self._valid(split_type=SplitType.EXACT, splits=splits))
        assert len(g.splits) == 2

    def test_future_date_rejected(self):
        with pytest.raises(ValidationError):
            GroupExpenseCreate(**self._valid(expense_date=date.today() + timedelta(days=1)))

    def test_zero_amount_rejected(self):
        with pytest.raises(ValidationError):
            GroupExpenseCreate(**self._valid(total_amount=Decimal("0")))


# ── DueCreate ──────────────────────────────────────────────────────────────────

class TestDueCreate:
    def test_valid_i_owe(self):
        d = DueCreate(
            due_type=DueType.I_OWE,
            person_name="Raj",
            amount=Decimal("500.00"),
            description="Birthday gift",
        )
        assert d.due_type == DueType.I_OWE
        assert d.amount == Decimal("500.00")

    def test_valid_they_owe(self):
        d = DueCreate(
            due_type=DueType.THEY_OWE,
            person_name="Priya",
            amount=Decimal("200.00"),
            description="Lunch",
        )
        assert d.due_type == DueType.THEY_OWE

    def test_zero_amount_rejected(self):
        with pytest.raises(ValidationError):
            DueCreate(
                due_type=DueType.I_OWE,
                person_name="Raj",
                amount=Decimal("0"),
                description="test",
            )

    def test_empty_person_name_rejected(self):
        with pytest.raises(ValidationError):
            DueCreate(
                due_type=DueType.I_OWE,
                person_name="",
                amount=Decimal("100"),
                description="test",
            )