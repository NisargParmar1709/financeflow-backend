"""
tests/unit/test_exceptions.py — Custom Exception Tests

Every exception must carry:
  1. The correct HTTP status code
  2. A machine-readable error code string
  3. Any structured details the frontend needs

These tests prevent regressions where someone changes an exception
and breaks the frontend's error handling logic.
"""

import pytest
from app.utils.exceptions import (
    FinanceFlowException,
    ResourceNotFoundException,
    BudgetExceededException,
    UnauthorizedAccessException,
    ValidationException,
    ExternalServiceException,
    DuplicateResourceException,
)


class TestFinanceFlowException:
    def test_base_exception_attributes(self):
        exc = FinanceFlowException("Something failed", "SOME_ERROR", 400)
        assert exc.message == "Something failed"
        assert exc.code == "SOME_ERROR"
        assert exc.status_code == 400

    def test_default_details_is_empty_dict(self):
        exc = FinanceFlowException("msg", "CODE", 400)
        assert exc.details == {}

    def test_custom_details_stored(self):
        exc = FinanceFlowException("msg", "CODE", 400, details={"field": "amount"})
        assert exc.details["field"] == "amount"

    def test_is_exception(self):
        exc = FinanceFlowException("msg", "CODE", 400)
        assert isinstance(exc, Exception)


class TestResourceNotFoundException:
    def test_404_status(self):
        exc = ResourceNotFoundException("Expense", "exp-123")
        assert exc.status_code == 404

    def test_not_found_code(self):
        exc = ResourceNotFoundException("Expense", "exp-123")
        assert exc.code == "NOT_FOUND"

    def test_resource_and_id_in_details(self):
        exc = ResourceNotFoundException("Expense", "exp-123")
        assert exc.details["resource"] == "Expense"
        assert exc.details["id"] == "exp-123"

    def test_message_contains_resource(self):
        exc = ResourceNotFoundException("Budget", "bgt-456")
        assert "Budget" in exc.message

    def test_different_resources(self):
        for resource in ["Expense", "Income", "Account", "Budget", "Group"]:
            exc = ResourceNotFoundException(resource, "test-id")
            assert exc.details["resource"] == resource
            assert exc.status_code == 404


class TestBudgetExceededException:
    def test_400_status(self):
        exc = BudgetExceededException("Food", 3000.0, 3150.0)
        assert exc.status_code == 400

    def test_budget_exceeded_code(self):
        exc = BudgetExceededException("Food", 3000.0, 3150.0)
        assert exc.code == "BUDGET_EXCEEDED"

    def test_exceeded_by_calculation(self):
        exc = BudgetExceededException("Food", 3000.0, 3150.0)
        assert exc.details["exceeded_by"] == 150.0

    def test_limit_and_spent_in_details(self):
        exc = BudgetExceededException("Transport", 2000.0, 2500.0)
        assert exc.details["limit"] == 2000.0
        assert exc.details["spent"] == 2500.0

    def test_category_in_details(self):
        exc = BudgetExceededException("Groceries", 5000.0, 5001.0)
        assert exc.details["category"] == "Groceries"

    def test_category_in_message(self):
        exc = BudgetExceededException("Education", 1000.0, 1100.0)
        assert "Education" in exc.message

    def test_rounding_of_exceeded_by(self):
        exc = BudgetExceededException("Food", 3000.0, 3100.33)
        assert exc.details["exceeded_by"] == round(3100.33 - 3000.0, 2)


class TestUnauthorizedAccessException:
    def test_403_status(self):
        exc = UnauthorizedAccessException()
        assert exc.status_code == 403

    def test_forbidden_code(self):
        exc = UnauthorizedAccessException()
        assert exc.code == "FORBIDDEN"

    def test_has_message(self):
        exc = UnauthorizedAccessException()
        assert len(exc.message) > 0


class TestValidationException:
    def test_400_status(self):
        exc = ValidationException("Amount must be positive")
        assert exc.status_code == 400

    def test_validation_error_code(self):
        exc = ValidationException("Amount must be positive")
        assert exc.code == "VALIDATION_ERROR"

    def test_field_in_details_when_provided(self):
        exc = ValidationException("Amount must be positive", field="amount")
        assert exc.details["field"] == "amount"

    def test_no_field_when_not_provided(self):
        exc = ValidationException("Something is wrong")
        assert exc.details == {} or "field" not in exc.details

    def test_message_preserved(self):
        exc = ValidationException("Non-cash requires account_id")
        assert exc.message == "Non-cash requires account_id"


class TestExternalServiceException:
    def test_503_status(self):
        exc = ExternalServiceException("Gemini AI")
        assert exc.status_code == 503

    def test_external_service_error_code(self):
        exc = ExternalServiceException("Cloudinary")
        assert exc.code == "EXTERNAL_SERVICE_ERROR"

    def test_service_name_in_message(self):
        exc = ExternalServiceException("Resend")
        assert "Resend" in exc.message

    def test_different_services(self):
        for service in ["Gemini AI", "Cloudinary", "Resend", "Clerk"]:
            exc = ExternalServiceException(service)
            assert service in exc.message
            assert exc.status_code == 503


class TestDuplicateResourceException:
    def test_409_status(self):
        exc = DuplicateResourceException("Budget", "Already exists")
        assert exc.status_code == 409

    def test_duplicate_error_code(self):
        exc = DuplicateResourceException("Budget", "Already exists")
        assert exc.code == "DUPLICATE_ERROR"

    def test_message_preserved(self):
        exc = DuplicateResourceException("Category", "Name already taken")
        assert "Name already taken" in exc.message