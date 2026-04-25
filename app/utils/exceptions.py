"""
app/utils/exceptions.py — Custom Exception Hierarchy

WHY THIS FILE EXISTS (Doc2 — Section 2.1):
  Python's built-in Exception is too generic. Raising Exception("not found")
  forces every caller to parse the string to understand what went wrong.

  Instead, we have a typed hierarchy:
    FinanceFlowException          ← base: all app errors
      ├── ResourceNotFoundException   ← 404: record not in DB
      ├── UnauthorizedAccessException ← 403: BOLA/IDOR (Video 20 — Security)
      ├── BudgetExceededException     ← 400: business rule violation
      ├── ValidationException         ← 400: semantic validation failure
      ├── DuplicateResourceException  ← 409: unique constraint
      └── ExternalServiceException    ← 503: Gemini/Cloudinary down

BENEFITS OF A TYPED HIERARCHY:
  1. The global error handler (error_handler.py) catches FinanceFlowException
     and reads .status_code, .code, .message — no string parsing needed
  2. Services raise specific exceptions: raise ResourceNotFoundException("Expense", id)
     Routes don't need try/except — the middleware handles it
  3. The frontend receives a consistent machine-readable 'code' field
     so it can switch on the code and show the right UI state

SECURITY NOTE (Video 20 — Authorization):
  UnauthorizedAccessException is raised for BOLA (Broken Object Level Auth)
  — when user A tries to access user B's expense. We raise 403, not 404.
  Some APIs return 404 to "hide" the resource exists. We use 403 because:
    - 404 misleads the user into thinking the resource doesn't exist
    - 403 is honest: it exists, you just can't see it
    - This is a design choice — document it so future devs don't "fix" it
"""


class FinanceFlowException(Exception):
    """
    Base exception for all application errors.

    All custom exceptions inherit from this. The global error handler in
    app/middleware/error_handler.py catches FinanceFlowException and
    formats it into the standard JSON error response.

    Attributes:
        message: Human-readable error message (safe to show users)
        code: Machine-readable code (frontend switches on this)
        status_code: HTTP status to return
        details: Optional extra context (field names, values, limits)
    """

    def __init__(
        self,
        message: str,
        code: str,
        status_code: int = 400,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}


# ── 404 Not Found ──────────────────────────────────────────────────────────────

class ResourceNotFoundException(FinanceFlowException):
    """
    Raised when a requested database record does not exist.

    USAGE:
        expense = await db.get(Expense, expense_id)
        if not expense:
            raise ResourceNotFoundException("Expense", str(expense_id))
 
    Frontend receives:
        { "code": "NOT_FOUND", "message": "Expense not found",
          "details": { "resource": "Expense", "id": "exp_123" } }
    """

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            message=f"{resource} not found",
            code="NOT_FOUND",
            status_code=404,
            details={"resource": resource, "id": resource_id},
        )


# ── 403 Forbidden (BOLA / IDOR protection) ────────────────────────────────────

class UnauthorizedAccessException(FinanceFlowException):
    """
    Raised when a user tries to access another user's resource.

    WHY THIS EXISTS (Video 20 — BOLA/IDOR):
      BOLA = Broken Object Level Authorization
      Scenario: User A has expense ID "exp_abc". User B knows that ID and
      sends GET /api/v1/expenses/exp_abc. Without this check, User B sees
      User A's private financial data.

      The correct check in every service that fetches by ID:
        expense = await db.get(Expense, expense_id)
        if expense.user_id != current_user_id:
            raise UnauthorizedAccessException()

    NEVER just check if the record exists without checking ownership.
    """

    def __init__(self) -> None:
        super().__init__(
            message="You do not have access to this resource",
            code="FORBIDDEN",
            status_code=403,
        )


# ── 400 Business Rule Violations ──────────────────────────────────────────────

class BudgetExceededException(FinanceFlowException):
    """
    Raised when adding an expense would exceed the user's budget for that category.

    Returns enough context for the frontend to show a specific UI:
      "You've exceeded your ₹3,000 Food budget by ₹250"

    USAGE:
        if projected_spend > budget.limit_amount:
            raise BudgetExceededException(
                category=category.name,
                limit=float(budget.limit_amount),
                spent=float(projected_spend)
            )
    """

    def __init__(self, category: str, limit: float, spent: float) -> None:
        super().__init__(
            message=f"Monthly budget exceeded for {category}",
            code="BUDGET_EXCEEDED",
            status_code=400,
            details={
                "category": category,
                "limit": limit,
                "spent": spent,
                "exceeded_by": round(spent - limit, 2),
            },
        )


class ValidationException(FinanceFlowException):
    """
    Business logic validation failures (beyond what Pydantic catches).

    Pydantic handles TYPE and FORMAT validation (is this a valid email?).
    ValidationException handles SEMANTIC validation (is this date in the past?
    Can a user have two active budgets for the same category?).

    USAGE:
        if expense_date > date.today():
            raise ValidationException(
                message="Expense date cannot be in the future",
                field="expense_date"
            )
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(
            message=message,
            code="VALIDATION_ERROR",
            status_code=400,
            details={"field": field} if field else {},
        )


# ── 409 Conflict ──────────────────────────────────────────────────────────────

class DuplicateResourceException(FinanceFlowException):
    """
    Raised when a creation request would violate a UNIQUE constraint.

    We catch this BEFORE hitting the DB when possible (check-then-insert),
    but also catch IntegrityError from SQLAlchemy as a fallback.

    USAGE:
        existing = await db.execute(select(Budget).where(
            Budget.user_id == user_id,
            Budget.category_id == category_id,
            Budget.period == period,
            Budget.is_active == True,
        ))
        if existing.scalar_one_or_none():
            raise DuplicateResourceException(
                "Budget", "An active budget for this category and period already exists"
            )
    """

    def __init__(self, resource: str, reason: str) -> None:
        super().__init__(
            message=reason,
            code="DUPLICATE_ERROR",
            status_code=409,
            details={"resource": resource},
        )


# ── 503 External Service ───────────────────────────────────────────────────────

class ExternalServiceException(FinanceFlowException):
    """
    Raised when a third-party service (Gemini, Cloudinary, Resend) fails.

    WHY NOT 500: 500 means OUR server is broken. When Gemini's API is down,
    our server is working fine — the external dependency failed. 503 is the
    correct HTTP status: "Service Unavailable" (temporarily).

    The frontend should show: "AI features are temporarily unavailable"
    and allow the user to complete their task without the AI feature.

    USAGE:
        try:
            result = await gemini_client.generate_content(...)
        except Exception as e:
            logger.error("Gemini API failed", extra={"error": str(e)})
            raise ExternalServiceException("Gemini AI")
    """

    def __init__(self, service: str) -> None:
        super().__init__(
            message=f"{service} is temporarily unavailable. Please try again.",
            code="EXTERNAL_SERVICE_ERROR",
            status_code=503,
            details={"service": service},
        )


# ── 402 Limit Reached ─────────────────────────────────────────────────────────

class LimitReachedException(FinanceFlowException):
    """
    Raised when user hits an application-level limit.

    Examples:
      - MAX_DOCS_PER_ACCOUNT (50) documents uploaded to one account
      - Free tier: max 5 bank accounts
      - Max 10 active group expenses

    USAGE:
        doc_count = await count_documents(db, account_id)
        if doc_count >= settings.MAX_DOCS_PER_ACCOUNT:
            raise LimitReachedException(
                "Documents per account", settings.MAX_DOCS_PER_ACCOUNT
            )
    """

    def __init__(self, resource: str, limit: int) -> None:
        super().__init__(
            message=f"Maximum {resource} limit of {limit} reached",
            code="LIMIT_REACHED",
            status_code=400,
            details={"resource": resource, "limit": limit},
        )