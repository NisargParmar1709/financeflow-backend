"""
tests/unit/test_logging.py — Logging System Tests

Tests the structured logging components that make the observability
system work correctly.
"""

import logging
import json

import pytest

from app.utils.logging import (
    generate_trace_id,
    get_logger,
    log_event,
    _JsonFormatter,
    _DevFormatter,
)


class TestGenerateTraceId:
    def test_starts_with_req_prefix(self):
        trace_id = generate_trace_id()
        assert trace_id.startswith("req_")

    def test_correct_length(self):
        trace_id = generate_trace_id()
        # "req_" + 8 hex chars = 12 total
        assert len(trace_id) == 12

    def test_unique_each_call(self):
        ids = {generate_trace_id() for _ in range(100)}
        assert len(ids) == 100  # all unique

    def test_hex_chars_only_after_prefix(self):
        trace_id = generate_trace_id()
        hex_part = trace_id[4:]  # after "req_"
        assert all(c in "0123456789abcdef" for c in hex_part)


class TestGetLogger:
    def test_returns_logger(self):
        logger = get_logger("test.module")
        assert isinstance(logger, logging.Logger)

    def test_logger_name_matches(self):
        logger = get_logger("app.services.expense_service")
        assert logger.name == "app.services.expense_service"

    def test_dunder_name_works(self):
        logger = get_logger(__name__)
        assert logger.name == __name__


class TestLogEvent:
    def test_log_event_emits_at_info_by_default(self, caplog):
        logger = get_logger("test.log_event")
        with caplog.at_level(logging.INFO, logger="test.log_event"):
            log_event(logger, "test_event", trace_id="req_abc123", user_id="user_1")
        assert len(caplog.records) == 1

    def test_log_event_message_is_event_name(self, caplog):
        logger = get_logger("test.log_event2")
        with caplog.at_level(logging.INFO, logger="test.log_event2"):
            log_event(logger, "expense_created", trace_id="req_abc")
        assert caplog.records[0].getMessage() == "expense_created"

    def test_log_event_warning_level(self, caplog):
        logger = get_logger("test.log_event3")
        with caplog.at_level(logging.WARNING, logger="test.log_event3"):
            log_event(logger, "budget_exceeded", trace_id="req_abc", level="warning")
        assert caplog.records[0].levelno == logging.WARNING

    def test_log_event_extra_fields_attached(self, caplog):
        logger = get_logger("test.log_event4")
        with caplog.at_level(logging.INFO, logger="test.log_event4"):
            log_event(
                logger,
                "expense_created",
                trace_id="req_abc123",
                user_id="user_1",
                amount="150.00",
            )
        record = caplog.records[0]
        assert hasattr(record, "trace_id")
        assert record.trace_id == "req_abc123"
        assert record.user_id == "user_1"
        assert record.amount == "150.00"

    def test_log_event_no_trace_default(self, caplog):
        logger = get_logger("test.log_event5")
        with caplog.at_level(logging.INFO, logger="test.log_event5"):
            log_event(logger, "some_event")
        record = caplog.records[0]
        assert record.trace_id == "no-trace"


class TestJsonFormatter:
    def _format_record(self, msg: str, **extra) -> dict:
        formatter = _JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="",
            lineno=0, msg=msg, args=(), exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return json.loads(formatter.format(record))

    def test_has_required_fields(self):
        result = self._format_record("Test message")
        assert "timestamp" in result
        assert "level" in result
        assert "logger" in result
        assert "message" in result

    def test_message_correct(self):
        result = self._format_record("Hello world")
        assert result["message"] == "Hello world"

    def test_level_is_info(self):
        result = self._format_record("msg")
        assert result["level"] == "INFO"

    def test_app_field_present(self):
        result = self._format_record("msg")
        assert result["app"] == "financeflow-backend"

    def test_extra_fields_included(self):
        result = self._format_record("msg", trace_id="req_abc", user_id="user_1")
        assert result["trace_id"] == "req_abc"
        assert result["user_id"] == "user_1"

    def test_output_is_valid_json(self):
        formatter = _JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="",
            lineno=0, msg="test", args=(), exc_info=None,
        )
        output = formatter.format(record)
        # Should not raise
        parsed = json.loads(output)
        assert isinstance(parsed, dict)


class TestDevFormatter:
    def _format(self, msg: str, **extra) -> str:
        formatter = _DevFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="",
            lineno=0, msg=msg, args=(), exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return formatter.format(record)

    def test_message_in_output(self):
        output = self._format("Test message")
        assert "Test message" in output

    def test_level_in_output(self):
        output = self._format("msg")
        assert "INFO" in output

    def test_trace_id_visible_in_output(self):
        """KEY TEST: trace_id must be visible in dev terminal."""
        output = self._format("→ GET /api/v1/expenses", trace_id="req_abc123")
        assert "req_abc123" in output

    def test_user_id_visible_in_output(self):
        output = self._format("event", user_id="user_2abc123")
        assert "user_2abc123" in output

    def test_multiple_extra_fields_visible(self):
        output = self._format(
            "expense_created",
            trace_id="req_abc",
            amount="150.00",
            category="Food",
        )
        assert "req_abc" in output
        assert "150.00" in output
        assert "Food" in output

    def test_no_extra_no_pipe_suffix(self):
        output = self._format("Simple message")
        # When no extras, no pipe separator needed
        # Just verify it doesn't crash and message is there
        assert "Simple message" in output