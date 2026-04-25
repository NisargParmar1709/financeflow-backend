"""
app/utils/logging.py — Structured Logging System

Dev:        human-readable  "2024-01-15 14:30:22 | INFO | app.services | Created expense"
Production: JSON per line   {"timestamp":"...","level":"INFO","message":"Created expense",...}

USAGE (top of every file that logs):
    from app.utils.logging import get_logger
    logger = get_logger(__name__)
    logger.info("Expense created", extra={"expense_id": str(expense.id)})
"""

import logging
import sys
import json
import time
import uuid


class _JsonFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects.
    Uses only stdlib — no external dependency needed.
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


def setup_logging() -> None:
    """
    Configure root logger once at app startup (called from main.py).
    All modules that call get_logger(__name__) inherit this config automatically.
    """
    # Import here to avoid circular import at module level
    from app.config import settings

    root_level = logging.DEBUG if settings.is_development else logging.INFO
    logging.root.setLevel(root_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(root_level)

    if settings.is_production:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

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
    Returns a named logger. Pass __name__ from the calling module.

    Example:
        logger = get_logger(__name__)  # → "app.services.expense_service"
    """
    return logging.getLogger(name)


def generate_trace_id() -> str:
    """Short unique ID for request tracing: 'req_a3f92c1b'"""
    return f"req_{uuid.uuid4().hex[:8]}"


class RequestLogger:
    """Logs structured request start/end events with timing."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def log_request_start(
        self, method: str, path: str, client_ip: str, trace_id: str, user_id: str | None = None
    ) -> float:
        self._logger.info(
            f"→ {method} {path}",
            extra={"trace_id": trace_id, "method": method, "path": path,
                   "client_ip": client_ip, "user_id": user_id or "anonymous",
                   "event": "request_start"},
        )
        return time.monotonic()

    def log_request_end(
        self, method: str, path: str, status_code: int,
        start_time: float, trace_id: str, user_id: str | None = None
    ) -> None:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        log_fn = (self._logger.error if status_code >= 500
                  else self._logger.warning if status_code >= 400
                  else self._logger.info)
        log_fn(
            f"← {method} {path} {status_code} ({duration_ms}ms)",
            extra={"trace_id": trace_id, "method": method, "path": path,
                   "status_code": status_code, "duration_ms": duration_ms,
                   "user_id": user_id or "anonymous", "event": "request_end"},
        )
        if duration_ms > 500:
            self._logger.warning(
                f"Slow request: {method} {path} took {duration_ms}ms",
                extra={"trace_id": trace_id, "duration_ms": duration_ms, "event": "slow_request"},
            )