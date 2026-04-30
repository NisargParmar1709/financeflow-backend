"""
app/schemas/common.py — Shared Response Envelopes & Pagination

WHY THIS FILE EXISTS (Doc2 — Section 5.2 & 12.1):
  Every API response in FinanceFlow MUST follow the same envelope shape.
  Frontend team relies on this contract — breaking it breaks the integration.

  RULE: Routers never return raw Pydantic objects directly.
        Always wrap in success_response() or error_response().

  SUCCESS SHAPE:
    {
      "success": true,
      "message": "OK",
      "data": { ...resource... },
      "meta": { "page": 1, "total": 42, ... }   ← only for list endpoints
    }

  ERROR SHAPE (set by error_handler.py, not here):
    {
      "success": false,
      "error": "BUDGET_EXCEEDED",     ← machine-readable code
      "message": "Budget exceeded...", ← user-readable text
      "details": { ... }
    }

PAGINATION CONTRACT (Doc2 — Section 12.1):
  All list endpoints return `meta` with:
    page, limit, total, pages, has_next, has_prev

  Frontend reads meta.total to show "42 transactions found".
  Frontend reads meta.has_next to decide whether to show "Load more".
"""

from __future__ import annotations

import math
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ── Pagination Meta ────────────────────────────────────────────────────────────

class PaginationMeta(BaseModel):
    """
    Attached to every paginated list response.

    Usage in a router:
        expenses, total = await expense_service.list(...)
        return success_response(
            data=expenses,
            meta=PaginationMeta.build(page=1, limit=20, total=total),
        )
    """

    page: int = Field(..., description="Current page number (1-indexed)")
    limit: int = Field(..., description="Items per page")
    total: int = Field(..., description="Total number of matching records")
    pages: int = Field(..., description="Total number of pages")
    has_next: bool = Field(..., description="True if there is a next page")
    has_prev: bool = Field(..., description="True if there is a previous page")

    @classmethod
    def build(cls, *, page: int, limit: int, total: int) -> "PaginationMeta":
        """
        Compute all derived fields from the three raw values.

        Example:
            meta = PaginationMeta.build(page=2, limit=20, total=45)
            # → pages=3, has_next=True, has_prev=True
        """
        pages = math.ceil(total / limit) if limit > 0 else 0
        return cls(
            page=page,
            limit=limit,
            total=total,
            pages=pages,
            has_next=page < pages,
            has_prev=page > 1,
        )


# ── Generic Success Response ───────────────────────────────────────────────────

class SuccessResponse(BaseModel, Generic[T]):
    """
    Standard success envelope. Generic over the data type T.

    Example (single resource):
        SuccessResponse[ExpenseResponse](data=expense)

    Example (list):
        SuccessResponse[list[ExpenseResponse]](data=expenses, meta=pagination)

    Using Generic[T] gives us IDE autocomplete on .data and correct
    OpenAPI schema generation — no need for Any types.
    """

    success: bool = True
    message: str = "OK"
    data: T
    meta: PaginationMeta | None = None


# ── Helper functions ───────────────────────────────────────────────────────────
# Use these in routers instead of constructing SuccessResponse directly.
# They reduce boilerplate and make intent obvious.

def success_response(
    data: Any,
    message: str = "OK",
    meta: PaginationMeta | None = None,
) -> dict:
    """
    Build a standard success response dict.

    Returns a plain dict (not a Pydantic model) so FastAPI can serialize it
    without needing `response_model` annotations on every route.

    Usage in router:
        return success_response(data=expense, message="Expense created")
    """
    result: dict[str, Any] = {
        "success": True,
        "message": message,
        "data": data,
    }
    if meta is not None:
        result["meta"] = meta.model_dump()
    return result


def deleted_response(message: str = "Deleted successfully") -> dict:
    """
    Standard response for DELETE operations that return no data.

    Usage:
        return deleted_response("Expense deleted")
    """
    return {"success": True, "message": message}