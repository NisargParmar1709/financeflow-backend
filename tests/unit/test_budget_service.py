"""
tests/unit/test_budget_service.py — Budget Service Unit Tests

Tests the core budget logic:
  - SAFE / WARNING / EXCEEDED status computation
  - Correct percent calculation
  - Remaining amount calculation

All DB calls are mocked — we test the computation logic, not SQLAlchemy.
"""

import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.budget_service import _compute_status


class TestComputeStatus:
    """
    _compute_status() is the core of budget tracking.
    These tests lock in its behaviour so refactors don't silently break it.
    """

    def _make_budget(self, limit: Decimal, threshold: int = 80) -> MagicMock:
        b = MagicMock()
        b.limit_amount = limit
        b.alert_threshold_percent = threshold
        return b

    def test_safe_status_below_threshold(self):
        budget = self._make_budget(Decimal("3000"), threshold=80)
        result = _compute_status(budget, Decimal("1500"))
        assert result["status"] == "SAFE"
        assert result["spent_pct"] == 50.0

    def test_warning_status_at_threshold(self):
        budget = self._make_budget(Decimal("3000"), threshold=80)
        result = _compute_status(budget, Decimal("2400"))  # exactly 80%
        assert result["status"] == "WARNING"

    def test_warning_status_above_threshold_below_limit(self):
        budget = self._make_budget(Decimal("3000"), threshold=80)
        result = _compute_status(budget, Decimal("2700"))  # 90%
        assert result["status"] == "WARNING"

    def test_exceeded_status_at_limit(self):
        budget = self._make_budget(Decimal("3000"), threshold=80)
        result = _compute_status(budget, Decimal("3000"))  # exactly at limit
        assert result["status"] == "EXCEEDED"

    def test_exceeded_status_over_limit(self):
        budget = self._make_budget(Decimal("3000"), threshold=80)
        result = _compute_status(budget, Decimal("3500"))
        assert result["status"] == "EXCEEDED"

    def test_remaining_correct_safe(self):
        budget = self._make_budget(Decimal("3000"))
        result = _compute_status(budget, Decimal("1000"))
        assert Decimal(result["remaining"]) == Decimal("2000")

    def test_remaining_zero_when_exceeded(self):
        budget = self._make_budget(Decimal("3000"))
        result = _compute_status(budget, Decimal("3500"))
        assert Decimal(result["remaining"]) == Decimal("0")

    def test_spent_so_far_returned(self):
        budget = self._make_budget(Decimal("5000"))
        result = _compute_status(budget, Decimal("1250"))
        assert Decimal(result["spent_so_far"]) == Decimal("1250")

    def test_pct_calculation_precision(self):
        budget = self._make_budget(Decimal("3000"))
        result = _compute_status(budget, Decimal("1000"))
        assert result["spent_pct"] == pytest.approx(33.3, rel=0.1)

    def test_zero_spent(self):
        budget = self._make_budget(Decimal("3000"))
        result = _compute_status(budget, Decimal("0"))
        assert result["status"] == "SAFE"
        assert result["spent_pct"] == 0.0
        assert Decimal(result["remaining"]) == Decimal("3000")

    def test_custom_alert_threshold_50(self):
        """Budget with 50% alert threshold."""
        budget = self._make_budget(Decimal("2000"), threshold=50)
        result = _compute_status(budget, Decimal("1100"))  # 55%
        assert result["status"] == "WARNING"

    def test_custom_alert_threshold_90(self):
        """Budget with 90% alert threshold — only warns very late."""
        budget = self._make_budget(Decimal("2000"), threshold=90)
        result = _compute_status(budget, Decimal("1700"))  # 85% — below 90%
        assert result["status"] == "SAFE"

    def test_all_fields_present_in_result(self):
        budget = self._make_budget(Decimal("1000"))
        result = _compute_status(budget, Decimal("400"))
        required_keys = {"spent_so_far", "remaining", "spent_pct", "status"}
        assert required_keys.issubset(result.keys())

    def test_amounts_are_strings_for_json_serialization(self):
        """All Decimal values must be strings for JSON serialisation."""
        budget = self._make_budget(Decimal("1000"))
        result = _compute_status(budget, Decimal("400"))
        assert isinstance(result["spent_so_far"], str)
        assert isinstance(result["remaining"], str)