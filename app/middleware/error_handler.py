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
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import IntegrityError, OperationalError
from pydantic import ValidationError

from app.config import settings

logger = logging.getLogger(__name__)


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
    body = {
        "error": True,
        "code": code,
        "message": message,
    }
    if details is not None:
        body["details"] = details

    return JSONResponse(status_code=status_code, content=body)


# ── Exception Handlers ─────────────────────────────────────────────────────────

async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Handles HTTPExceptions raised explicitly in routers/services.
    Examples: raise HTTPException(status_code=404, detail="Expense not found")
    """
    logger.warning(
        f"HTTPException: {exc.status_code} {exc.detail} | "
        f"path={request.url.path} method={request.method}"
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

    FastAPI raises RequestValidationError when incoming data doesn't match
    the Pydantic schema. Example: required field missing, wrong type.

    We format the errors into a clean list so the frontend can highlight
    the specific fields that failed.

    VALIDATION CONCEPT (Video 9):
      This is the "syntactic validation" layer — Pydantic checks types,
      required fields, and field constraints before the code even runs.
    """
    # Transform Pydantic's internal error format into something usable
    field_errors = []
    for error in exc.errors():
        field_errors.append({
            "field": " → ".join(str(loc) for loc in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        })

    logger.warning(
        f"Validation error on {request.url.path}: {field_errors}"
    )

    return error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="VALIDATION_ERROR",
        message="Request data validation failed",
        details=field_errors,
    )


async def integrity_error_handler(
    request: Request, exc: IntegrityError
) -> JSONResponse:
    """
    Handles database constraint violations.

    Examples of when this fires:
      - INSERT with a duplicate email (UNIQUE constraint)
      - INSERT with a foreign key that doesn't exist
      - DELETE of a record that has children (FK constraint)

    WHY WE CATCH THIS SPECIFICALLY:
      Without this handler, a duplicate email error becomes a 500 Internal
      Server Error. With it, we can return a meaningful 409 Conflict.

    SECURITY: We log the full DB error internally but only return a generic
    message to the client — DB error messages can expose schema details.
    """
    logger.error(
        f"DB IntegrityError on {request.url.path}: {exc.orig}",
        exc_info=True,
    )

    # Check for specific constraint patterns to give better messages
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


async def operational_error_handler(
    request: Request, exc: OperationalError
) -> JSONResponse:
    """
    Handles database connection/operational failures.
    Example: DB server is unreachable, connection pool exhausted.

    This is a 503 Service Unavailable — the server is up but DB is not.
    """
    logger.critical(
        f"DB OperationalError — database may be unreachable: {exc}",
        exc_info=True,
    )
    return error_response(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="DATABASE_UNAVAILABLE",
        message="Database is temporarily unavailable. Please try again.",
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    The final safety net — catches EVERYTHING that fell through.

    This is the last middleware in the error handling chain.
    Any exception not caught above lands here.

    PRODUCTION vs DEVELOPMENT behavior:
      - Development: return the actual error message (helps debugging)
      - Production: return generic "unexpected error" (prevents info leakage)

    Always logs the full traceback server-side.
    """
    # Full traceback to server logs — never sent to client
    logger.critical(
        f"Unhandled exception on {request.url.path}: {exc}\n"
        f"{traceback.format_exc()}"
    )

    # In development: reveal the error message for easier debugging
    # In production: hide it — attackers read 500 error messages
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
    Ordering matters: more specific handlers are registered first.
    FastAPI matches the most specific exception type first.
    """
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(OperationalError, operational_error_handler)
    # Catch-all — must be last
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