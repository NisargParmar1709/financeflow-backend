"""
app/middleware/error_handler.py — Global Error Handler

WHY THIS FILE EXISTS (Video 16 — Fault Tolerant Systems):
  No matter how carefully we code, exceptions will happen:
    - DB connection drops
    - Third-party API (Gemini, Cloudinary) times out
    - A query violates a database constraint
    - A bug in our own code

  Without a global handler, FastAPI returns a 500 with a full Python
  stack trace to the client — exposing internal architecture, file paths,
  and potentially sensitive data to attackers.

  This file:
    1. Catches every exception type we care about
    2. Logs the FULL error server-side (for debugging)
    3. Returns a CLEAN, generic error response to the client
    4. Never leaks stack traces in production

CONSISTENT ERROR RESPONSE FORMAT:
  Every error in FinanceFlow returns this JSON shape:
  {
    "error": true,
    "code": "VALIDATION_ERROR",   ← machine-readable code for frontend
    "message": "Human readable",  ← shown to users
    "details": {...}              ← optional extra context (validation fields)
  }

  Why consistent format: The frontend has one error interceptor in Axios
  that handles all errors. If error shapes differ, the frontend needs
  special cases for each endpoint — fragile and messy.

SECURITY (Video 20 — Backend Security):
  In production (APP_ENV=production), error details are hidden.
  Only in development do we expose the full exception message.
  Attackers use error messages to understand system internals.
"""

import logging
import traceback

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import settings
from app.utils.exceptions import FinanceFlowException

logger = logging.getLogger(__name__)


# ── Helper: extract trace_id safely ───────────────────────────────────────────


def _trace_id(request: Request) -> str:
    """
    Reads trace_id from request.state (set by RequestLoggingMiddleware).
    Falls back to 'no-trace' if middleware hasn't run yet (e.g. startup errors).
    Including trace_id in EVERY error log means you can grep one ID and find
    the full request → auth → error chain in one shot.
    """
    return getattr(request.state, "trace_id", "no-trace")


# ── Standard Error Response Builder ───────────────────────────────────────────


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict | list | None = None,
) -> JSONResponse:
    """
    Creates a standardized error JSON response.

    Args:
        status_code: HTTP status code (400, 401, 403, 404, 422, 500...)
        code: Machine-readable error code for frontend switch statements
        message: Human-readable message (can be shown to users)
        details: Optional extra data (e.g., validation field errors)
    """
    body: dict = {
        "error": True,
        "code": code,
        "message": message,
    }
    if details is not None:
        body["details"] = details

    return JSONResponse(status_code=status_code, content=body)


# ── Exception Handlers ─────────────────────────────────────────────────────────


async def financeflow_exception_handler(
    request: Request, exc: FinanceFlowException
) -> JSONResponse:
    """
    Handles all our custom FinanceFlowException subclasses.

    WHY THIS HANDLER IS FIRST:
      All business exceptions (BudgetExceededException, ResourceNotFoundException,
      etc.) inherit from FinanceFlowException. Registering this handler means they
      are caught BEFORE the generic Exception handler, producing clean 400/403/404
      responses instead of 500s.

    Log level by status:
      4xx → warning (client error, expected occasionally)
      5xx → error   (server problem, needs investigation)
    """
    trace_id = _trace_id(request)
    log_fn = logger.error if exc.status_code >= 500 else logger.warning
    log_fn(
        f"FinanceFlowException: [{exc.code}] {exc.message}",
        extra={
            "event": "app_exception",
            "trace_id": trace_id,
            "error_code": exc.code,
            "status_code": exc.status_code,
            "details": exc.details,
            "path": request.url.path,
            "method": request.method,
        },
    )
    return error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details or None,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Handles HTTPExceptions raised explicitly in routers/services.
    Examples: raise HTTPException(status_code=404, detail="Expense not found")
    """
    trace_id = _trace_id(request)
    logger.warning(
        f"HTTPException {exc.status_code}",
        extra={
            "event": "http_exception",
            "trace_id": trace_id,
            "status_code": exc.status_code,
            "detail": exc.detail,
            "path": request.url.path,
            "method": request.method,
        },
    )
    return error_response(
        status_code=exc.status_code,
        code=_status_code_to_error_code(exc.status_code),
        message=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Handles Pydantic validation errors from request body/query params.

    Formats errors into a clean list so the frontend can highlight
    the specific fields that failed.

    VALIDATION CONCEPT (Video 9):
      This is the "syntactic validation" layer — Pydantic checks types,
      required fields, and field constraints before the code even runs.
    """
    field_errors = [
        {
            "field": " → ".join(str(loc) for loc in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]

    trace_id = _trace_id(request)
    logger.warning(
        "Request validation failed",
        extra={
            "event": "validation_error",
            "trace_id": trace_id,
            "path": request.url.path,
            "field_errors": field_errors,
        },
    )

    return error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="VALIDATION_ERROR",
        message="Request data validation failed",
        details=field_errors,
    )


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """
    Handles database constraint violations.

    WHY 409 not 500: The server processed the request correctly — it's the
    data itself that violates a constraint (duplicate email, missing FK).
    SECURITY: Log the full DB error internally; return a generic message
    to the client — DB errors can reveal schema details to attackers.
    """
    trace_id = _trace_id(request)
    logger.error(
        "DB IntegrityError",
        extra={
            "event": "db_integrity_error",
            "trace_id": trace_id,
            "path": request.url.path,
            "orig": str(exc.orig),
        },
        exc_info=True,
    )

    orig_str = str(exc.orig).lower()
    if "unique" in orig_str:
        message = "A record with this value already exists"
        code = "DUPLICATE_ERROR"
    elif "foreign key" in orig_str:
        message = "Referenced record does not exist"
        code = "FOREIGN_KEY_ERROR"
    else:
        message = "Database constraint violation"
        code = "DB_CONSTRAINT_ERROR"

    return error_response(
        status_code=status.HTTP_409_CONFLICT,
        code=code,
        message=message,
    )


async def operational_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
    """
    Handles database connection/operational failures.
    Example: DB server unreachable, connection pool exhausted.
    """
    trace_id = _trace_id(request)
    logger.critical(
        "DB OperationalError — database may be unreachable",
        extra={
            "event": "db_operational_error",
            "trace_id": trace_id,
            "path": request.url.path,
            "error": str(exc),
        },
        exc_info=True,
    )
    return error_response(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="DATABASE_UNAVAILABLE",
        message="Database is temporarily unavailable. Please try again.",
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    The final safety net — catches EVERYTHING that fell through.

    Always logs the full traceback server-side.
    PRODUCTION: returns a generic message (prevents info leakage).
    DEVELOPMENT: includes the actual error message for easier debugging.
    """
    trace_id = _trace_id(request)
    logger.critical(
        f"Unhandled exception: {type(exc).__name__}: {exc}",
        extra={
            "event": "unhandled_exception",
            "trace_id": trace_id,
            "path": request.url.path,
            "method": request.method,
            "exception_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        },
    )

    message = str(exc) if settings.is_development else "An unexpected error occurred"

    return error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="INTERNAL_ERROR",
        message=message,
    )


# ── Registration ───────────────────────────────────────────────────────────────


def register_exception_handlers(app: FastAPI) -> None:
    """
    Registers all exception handlers with the FastAPI app.
    Called from app/main.py during app creation.

    ORDER MATTERS — FastAPI matches the most specific type first:
      1. FinanceFlowException → our custom business exceptions (most specific)
      2. HTTPException        → FastAPI's built-in HTTP errors
      3. RequestValidationError → Pydantic schema validation failures
      4. IntegrityError       → DB unique/FK constraint violations
      5. OperationalError     → DB connection failures
      6. Exception            → catch-all safety net (least specific, last)
    """
    app.add_exception_handler(FinanceFlowException, financeflow_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(OperationalError, operational_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)


# ── Helper ─────────────────────────────────────────────────────────────────────


def _status_code_to_error_code(status_code: int) -> str:
    """Maps HTTP status codes to machine-readable error code strings."""
    mapping = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMITED",
        500: "INTERNAL_ERROR",
        503: "SERVICE_UNAVAILABLE",
    }
    return mapping.get(status_code, "ERROR")