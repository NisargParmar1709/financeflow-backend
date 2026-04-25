"""
tests/unit/test_formatting.py — Unit Tests: Formatting Utilities

WHAT WE TEST HERE:
  Pure functions from app/utils/formatting.py — no DB, no Redis, no HTTP.
  These run in milliseconds and need zero infrastructure.

WHY TEST FORMATTING FIRST:
  Formatting bugs are silent and embarrassing:
  ₹1,50,000 displayed as ₹150000 or ₹1500.0 breaks user trust.
  INR formatting has edge cases (Indian numbering is non-standard).

TESTING PHILOSOPHY (from Video 2 — Backend Engineer mindset):
  Test the "why" not just the "what":
  - Test edge cases (zero, negative, large numbers)
  - Test the exact behavior that would break user experience
  - Don't write tests that just confirm the function runs
"""

import pytest
from decimal import Decimal

from app.utils.formatting import (
    format_inr,
    parse_amount,
    build_pagination_meta,
    calculate_offset,
    _apply_indian_numbering,
)


# ── INR Formatting Tests ───────────────────────────────────────────────────────

class TestFormatINR:
    """Tests for the ₹ currency formatter with Indian numbering system."""

    def test_small_amount(self):
        """Amounts under 1000 have no comma."""
        assert format_inr(Decimal("150.50")) == "₹150.50"

    def test_thousands(self):
        """1000-9999 range — standard comma after 3 digits."""
        assert format_inr(Decimal("1500.00")) == "₹1,500.00"

    def test_indian_lakh(self):
        """
        1,50,000 — This is the key Indian numbering test.
        Western: 150,000 | Indian: 1,50,000
        """
        assert format_inr(Decimal("150000.00")) == "₹1,50,000.00"

    def test_indian_crore(self):
        """1,00,00,000 — one crore."""
        assert format_inr(Decimal("10000000.00")) == "₹1,00,00,000.00"

    def test_zero(self):
        """Zero amount — common edge case for new users."""
        assert format_inr(Decimal("0.00")) == "₹0.00"

    def test_paise_rounding(self):
        """Amounts with more than 2 decimal places are rounded (ROUND_HALF_UP)."""
        assert format_inr(Decimal("99.995")) == "₹100.00"
        assert format_inr(Decimal("99.994")) == "₹99.99"

    def test_integer_input(self):
        """Accepts plain integers (common from user input)."""
        assert format_inr(1500) == "₹1,500.00"

    def test_float_input(self):
        """Accepts floats — converted to Decimal internally."""
        result = format_inr(99.5)
        assert result == "₹99.50"

    def test_large_amount(self):
        """10 lakh — typical salary/large expense range."""
        assert format_inr(Decimal("1000000.00")) == "₹10,00,000.00"


# ── Amount Parsing Tests ───────────────────────────────────────────────────────

class TestParseAmount:
    """Tests for converting user input to Decimal."""

    def test_string_input(self):
        assert parse_amount("1500.50") == Decimal("1500.50")

    def test_integer_input(self):
        assert parse_amount(1500) == Decimal("1500.00")

    def test_float_precision(self):
        """Floats can have precision issues — parse_amount must handle them."""
        result = parse_amount(0.1 + 0.2)  # = 0.30000000000000004 in Python
        assert result == Decimal("0.30")

    def test_invalid_input_raises(self):
        """Non-numeric strings should raise ValueError, not crash silently."""
        with pytest.raises(ValueError, match="Invalid amount value"):
            parse_amount("not_a_number")

    def test_negative_amount(self):
        """Negative amounts for refunds/reversals."""
        assert parse_amount("-500.00") == Decimal("-500.00")


# ── Pagination Tests ───────────────────────────────────────────────────────────

class TestPagination:
    """Tests for pagination metadata builder."""

    def test_first_page(self):
        meta = build_pagination_meta(total_count=50, page=1, limit=20)
        assert meta["total"] == 50
        assert meta["page"] == 1
        assert meta["total_pages"] == 3
        assert meta["has_next"] is True
        assert meta["has_prev"] is False   # No previous on page 1

    def test_last_page(self):
        meta = build_pagination_meta(total_count=50, page=3, limit=20)
        assert meta["has_next"] is False   # No next on last page
        assert meta["has_prev"] is True

    def test_middle_page(self):
        meta = build_pagination_meta(total_count=100, page=3, limit=10)
        assert meta["has_next"] is True
        assert meta["has_prev"] is True
        assert meta["total_pages"] == 10

    def test_empty_results(self):
        """No records — total_pages should be 0, not error."""
        meta = build_pagination_meta(total_count=0, page=1, limit=20)
        assert meta["total_pages"] == 0
        assert meta["has_next"] is False
        assert meta["has_prev"] is False

    def test_exact_page_boundary(self):
        """When total is exactly divisible by limit."""
        meta = build_pagination_meta(total_count=40, page=2, limit=20)
        assert meta["total_pages"] == 2
        assert meta["has_next"] is False

    def test_offset_calculation(self):
        """SQL OFFSET = (page - 1) * limit."""
        assert calculate_offset(page=1, limit=20) == 0
        assert calculate_offset(page=2, limit=20) == 20
        assert calculate_offset(page=3, limit=20) == 40
        assert calculate_offset(page=5, limit=10) == 40