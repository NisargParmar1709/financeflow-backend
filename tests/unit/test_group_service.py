"""
tests/unit/test_group_service.py — Group Split Validation Tests

The split validation logic is pure computation — no DB needed.
Tests verify EQUAL/PERCENTAGE/EXACT splits are handled correctly.
"""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.group_schema import GroupExpenseCreate, SplitInput
from app.models.enums import SplitType


MEMBER_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
MEMBER_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
MEMBER_C = uuid.UUID("00000000-0000-0000-0000-000000000003")


def make_group_expense(**overrides) -> dict:
    base = {
        "total_amount": Decimal("900.00"),
        "paid_by_member_id": MEMBER_A,
        "description": "Hotel booking",
        "split_type": SplitType.EQUAL,
    }
    return {**base, **overrides}


class TestGroupExpenseSplitSchema:
    """
    Schema-level validation tests.
    Service-level split math is tested separately once
    we can mock group members.
    """

    def test_equal_split_no_splits_required(self):
        g = GroupExpenseCreate(**make_group_expense(split_type=SplitType.EQUAL))
        assert g.split_type == SplitType.EQUAL
        assert g.splits == []

    def test_exact_split_requires_splits(self):
        with pytest.raises(ValidationError) as exc_info:
            GroupExpenseCreate(**make_group_expense(split_type=SplitType.EXACT))
        errors = str(exc_info.value).lower()
        assert "splits" in errors

    def test_percentage_split_requires_splits(self):
        with pytest.raises(ValidationError) as exc_info:
            GroupExpenseCreate(**make_group_expense(split_type=SplitType.PERCENTAGE))
        errors = str(exc_info.value).lower()
        assert "splits" in errors

    def test_exact_split_with_two_members(self):
        splits = [
            SplitInput(member_id=MEMBER_A, amount=Decimal("450.00")),
            SplitInput(member_id=MEMBER_B, amount=Decimal("450.00")),
        ]
        g = GroupExpenseCreate(
            **make_group_expense(split_type=SplitType.EXACT, splits=splits)
        )
        assert len(g.splits) == 2
        assert g.splits[0].amount == Decimal("450.00")

    def test_percentage_split_with_members(self):
        splits = [
            SplitInput(member_id=MEMBER_A, percentage=Decimal("60")),
            SplitInput(member_id=MEMBER_B, percentage=Decimal("40")),
        ]
        g = GroupExpenseCreate(
            **make_group_expense(split_type=SplitType.PERCENTAGE, splits=splits)
        )
        assert len(g.splits) == 2

    def test_zero_total_rejected(self):
        with pytest.raises(ValidationError):
            GroupExpenseCreate(**make_group_expense(total_amount=Decimal("0")))

    def test_negative_total_rejected(self):
        with pytest.raises(ValidationError):
            GroupExpenseCreate(**make_group_expense(total_amount=Decimal("-100")))

    def test_split_input_negative_amount_rejected(self):
        with pytest.raises(ValidationError):
            SplitInput(member_id=MEMBER_A, amount=Decimal("-50"))

    def test_split_input_percentage_over_100_rejected(self):
        with pytest.raises(ValidationError):
            SplitInput(member_id=MEMBER_A, percentage=Decimal("101"))

    def test_split_input_zero_percentage_rejected(self):
        with pytest.raises(ValidationError):
            SplitInput(member_id=MEMBER_A, percentage=Decimal("0"))


class TestSplitInputValidation:
    def test_amount_field_for_exact(self):
        s = SplitInput(member_id=MEMBER_A, amount=Decimal("300.00"))
        assert s.amount == Decimal("300.00")
        assert s.percentage is None

    def test_percentage_field_for_percentage_split(self):
        s = SplitInput(member_id=MEMBER_A, percentage=Decimal("33.33"))
        assert s.percentage == Decimal("33.33")
        assert s.amount is None

    def test_member_id_required(self):
        with pytest.raises(ValidationError):
            SplitInput(amount=Decimal("100.00"))