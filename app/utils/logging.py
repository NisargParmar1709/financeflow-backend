"""
app/utils/logging.py — Structured Logging System

WHY STRUCTURED LOGGING (Video 18 — Observability):
  Two formats — same data, different presentation:
  Dev:        human-readable  "2024-01-15 14:30:22 | INFO | app.services | Created expense"
  Production: JSON per line   {"timestamp":"...","level":"INFO","message":"Created expense",...}

  JSON logs can be ingested by log aggregators (Datadog, Grafana Loki, AWS CloudWatch).
  You can then query: "show me all BudgetExceeded events for user X in the last hour".
  That is impossible with plain text logs.

THREE LAYERS OF LOGGING (Video 18 — Logs, Metrics, Traces):
  1. HTTP layer   → RequestLoggingMiddleware logs every request/response with trace_id
  2. Auth layer   → AuthGuardMiddleware logs every auth success/failure with trace_id
  3. Business layer → log_event() logs domain events (expense_created, budget_exceeded)

TRACE ID PATTERN:
  trace_id is generated once per request in RequestLoggingMiddleware.
  Every log line for that request includes the same trace_id.
  Result: grep one trace_id → see the entire journey of one request.

USAGE (top of every file that logs):
    from app.utils.logging import get_logger
    logger = get_logger(__name__)
    logger.info("Expense created", extra={"expense_id": str(expense.id)})

  For business events in services:
    from app.utils.logging import log_event
    log_event(logger, "expense_created", trace_id=trace_id,
              user_id=user_id, expense_id=str(expense.id), amount=str(amount))
"""

import logging
import sys
import json
import time
import uuid
from typing import Any


class _JsonFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects.
    Uses only stdlib — no external dependency needed.

    WHY JSON IN PRODUCTION:
      Log aggregators (Datadog, CloudWatch, Grafana) parse JSON natively.
      You can filter logs by field: event="expense_created" AND user_id="abc".
      Impossible with plain text unless you write custom parsers.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "app": "financeflow-backend",
        }
        # Include any extra fields the caller passed via extra={}
        standard_keys = logging.LogRecord("", 0, "", 0, "", [], None).__dict__.keys()
        for key, val in record.__dict__.items():
            if key not in standard_keys and not key.startswith("_"):
                log_obj[key] = val

        if record.exc_info:
            log_obj["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, default=str)


class _DevFormatter(logging.Formatter):
    """
    Human-readable formatter for development.

    WHY NOT just %(message)s:
      Python's standard Formatter only exposes built-in LogRecord attributes
      via % placeholders. Fields passed through extra={} are attached to the
      LogRecord as arbitrary attributes — they are INVISIBLE to the standard
      formatter unless you explicitly read them.

      This formatter reads the extra fields and appends them to the log line
      so trace_id, user_id, event, etc. are visible in the terminal.

    EXAMPLE OUTPUT:
      2026-05-01 02:11:20 | INFO     | request_logger | → GET /api/v1/expenses
                          | trace_id=req_a3f92c1b user_id=user_2abc event=request_start

      2026-05-01 02:11:20 | WARNING  | request_logger | ← GET /api/v1/expenses 400 (12.3ms)
                          | trace_id=req_a3f92c1b status_code=400 duration_ms=12.3

    STANDARD_KEYS:
      LogRecord always has these built-in attributes. We skip them when
      printing extra fields — otherwise the output is flooded with internal
      Python logging internals (pathname, lineno, thread, process, etc.)
    """

    # Built-in LogRecord keys we never print as "extra" fields
    _STANDARD_KEYS: frozenset[str] = frozenset(
        logging.LogRecord("", 0, "", 0, "", [], None).__dict__.keys()
    ) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        # Format the base line: timestamp | level | logger | message
        record.asctime = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        base = (
            f"{record.asctime} | {record.levelname:<8} | "
            f"{record.name} | {record.getMessage()}"
        )

        # Collect extra fields (anything not a standard LogRecord key)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in self._STANDARD_KEYS and not k.startswith("_")
        }

        if extras:
            # Format: key=value pairs, space-separated, on the same line
            extra_str = "  |  " + "  ".join(f"{k}={v}" for k, v in extras.items())
            return base + extra_str

        # Append exception info if present
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)

        return base


def setup_logging() -> None:
    """
    Configure root logger once at app startup (called from main.py).
    All modules that call get_logger(__name__) inherit this config automatically.

    LEVEL STRATEGY:
      Development: DEBUG — see everything including SQL queries
      Production:  INFO  — only meaningful events, not internal debug noise

    NOISY LIBRARIES:
      sqlalchemy.pool, httpx, clerk_backend_api all emit DEBUG logs for every
      connection/request. We silence them to WARNING to keep logs clean.
      SQL queries are still visible in dev via sqlalchemy.engine at INFO.
    """
    from app.config import settings

    root_level = logging.DEBUG if settings.is_development else logging.INFO
    logging.root.setLevel(root_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(root_level)

    if settings.is_production:
        # JSON for log aggregators (Datadog, CloudWatch, Grafana)
        handler.setFormatter(_JsonFormatter())
    else:
        # Human-readable with extra fields visible in terminal
        handler.setFormatter(_DevFormatter())

    # Silence noisy third-party libraries
    for noisy in ["sqlalchemy.pool", "httpx", "httpcore", "clerk_backend_api", "uvicorn.access"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Enable SQL query logging only in development
    if settings.is_development:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)

    logging.root.handlers.clear()
    logging.root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """
    Returns a named logger. Always pass __name__ from the calling module.

    The name becomes the "logger" field in JSON logs, so you can filter
    logs by module: logger="app.services.expense_service"

    Example:
        logger = get_logger(__name__)  # → "app.services.expense_service"
    """
    return logging.getLogger(name)


def generate_trace_id() -> str:
    """
    Generates a short, unique ID for one request lifecycle.

    Format: 'req_a3f92c1b'  (prefix + 8 hex chars = easy to read, copy, grep)

    WHY SHORT: Full UUIDs (36 chars) are hard to copy from logs. 8 hex chars
    give 4 billion unique values — more than enough for request tracing.
    """
    return f"req_{uuid.uuid4().hex[:8]}"


# ── Business Event Logger ──────────────────────────────────────────────────────

def log_event(
    logger: logging.Logger,
    event: str,
    *,
    trace_id: str = "no-trace",
    level: str = "info",
    **fields: Any,
) -> None:
    """
    Emit a structured business event log line.

    WHY THIS EXISTS:
      Without a standard helper, each developer writes their own log format:
        logger.info(f"Expense {expense_id} created by {user_id}")  # person A
        logger.info(f"Created expense: {user_id} -> {expense_id}")  # person B

      Both are readable but neither is machine-queryable.
      With log_event(), every business event has the same structure:
        {"event": "expense_created", "trace_id": "req_a3f92c1b",
         "user_id": "...", "expense_id": "...", "amount": "150.00"}

      Log aggregators can now answer: "how many expenses were created today?"
      or "show me all events for user X in the last hour".

    BUSINESS EVENTS TO LOG (from design docs):
      Auth:         login_success, login_failure, webhook_received
      Expenses:     expense_created, expense_updated, expense_deleted
      Budget:       budget_alert_triggered, budget_exceeded
      Accounts:     account_created, balance_updated, fd_maturity_alert
      Groups:       group_expense_created, split_settled
      AI:           insight_generated, insight_served_from_cache, chat_message
      Notifications: notification_created, notifications_read

    ARGS:
      logger:   The module-level logger (get_logger(__name__))
      event:    Snake_case event name (e.g. "expense_created")
      trace_id: From request.state.trace_id — links event to its HTTP request
      level:    "debug" | "info" | "warning" | "error" (default: "info")
      **fields: Any additional structured data (user_id, amount, etc.)

    USAGE IN SERVICES:
      from app.utils.logging import get_logger, log_event
      logger = get_logger(__name__)

      # After successfully creating an expense:
      log_event(logger, "expense_created",
                trace_id=trace_id,
                user_id=str(user_id),
                expense_id=str(expense.id),
                amount=str(expense.amount),
                category=expense.category.name,
                payment_mode=expense.payment_mode.value)

      # When budget is exceeded:
      log_event(logger, "budget_exceeded",
                trace_id=trace_id,
                level="warning",
                user_id=str(user_id),
                category=category_name,
                limit=str(limit),
                attempted=str(attempted_amount))
    """
    log_fn = getattr(logger, level, logger.info)
    log_fn(
        event,
        extra={"event": event, "trace_id": trace_id, **fields},
    )


# ── Request Logger ─────────────────────────────────────────────────────────────

class RequestLogger:
    """
    Logs structured request start/end events with timing.

    Used by RequestLoggingMiddleware. Each request produces exactly two
    log lines — start and end — making it easy to detect incomplete requests
    (start with no end = crash or timeout during processing).
    """

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def log_request_start(
        self,
        method: str,
        path: str,
        client_ip: str,
        trace_id: str,
        user_id: str | None = None,
    ) -> float:
        """
        Logs the incoming request and returns the start time for duration calc.
        Returns monotonic time — use only for duration, never for wall-clock time.
        """
        self._logger.info(
            f"→ {method} {path}",
            extra={
                "event": "request_start",
                "trace_id": trace_id,
                "method": method,
                "path": path,
                "client_ip": client_ip,
                "user_id": user_id or "anonymous",
            },
        )
        return time.monotonic()

    def log_request_end(
        self,
        method: str,
        path: str,
        status_code: int,
        start_time: float,
        trace_id: str,
        user_id: str | None = None,
    ) -> None:
        """
        Logs the completed request with duration and status code.

        Log level is determined by status code:
          2xx → info    (success, expected)
          4xx → warning (client error, investigate if high volume)
          5xx → error   (server problem, investigate immediately)

        Also emits a separate "slow_request" warning if duration > 500ms.
        Slow requests are the first signal of DB index problems or N+1 queries.
        """
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)

        log_fn = (
            self._logger.error if status_code >= 500
            else self._logger.warning if status_code >= 400
            else self._logger.info
        )
        log_fn(
            f"← {method} {path} {status_code} ({duration_ms}ms)",
            extra={
                "event": "request_end",
                "trace_id": trace_id,
                "method": method,
                "path": path,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "user_id": user_id or "anonymous",
            },
        )

        # Separate slow request warning — easy to alert on in Grafana/Datadog
        if duration_ms > 500:
            self._logger.warning(
                f"Slow request: {method} {path} took {duration_ms}ms",
                extra={
                    "event": "slow_request",
                    "trace_id": trace_id,
                    "method": method,
                    "path": path,
                    "duration_ms": duration_ms,
                },
            )