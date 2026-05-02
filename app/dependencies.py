"""
app/dependencies.py — Shared FastAPI Dependencies

WHY THIS FILE EXISTS:
  FastAPI uses Depends() to inject shared logic into route handlers.
  Rather than repeating auth + DB lookup in every single router,
  we define it once here and inject it where needed.

  Any route that needs the authenticated user adds:
    current_user: User = Depends(get_current_user)

AUTH FLOW (Doc2 — Section 1.4):
  1. AuthGuardMiddleware (runs before every request) validates the Clerk JWT
     and injects request.state.clerk_user_id
  2. get_current_user() reads clerk_user_id from request.state
  3. Queries our users table to get the full User ORM object
  4. Returns it to the route handler

WHY NOT VALIDATE JWT HERE:
  JWT validation is done once in AuthGuardMiddleware — not here.
  This dependency only does the DB lookup. Separating these concerns means:
  - Middleware handles security (fast, before routing)
  - Dependency handles data loading (slower, only when needed)
  - Public routes skip this dependency entirely
"""

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.user import User
from app.services.auth_service import get_user_by_clerk_id
from app.utils.exceptions import UnauthorizedAccessException
from app.utils.logging import get_logger

logger = get_logger(__name__)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency that returns the authenticated User ORM object.

    Reads clerk_user_id from request.state (set by AuthGuardMiddleware).
    Fetches the corresponding User row from our database.

    Usage in any protected router:
        @router.get("/expenses")
        async def list_expenses(
            current_user: User = Depends(get_current_user),
            db: AsyncSession = Depends(get_db),
        ):
            ...

    Raises:
        UnauthorizedAccessException (401) if clerk_user_id is missing from
        request.state — means AuthGuardMiddleware was bypassed somehow.

        ResourceNotFoundException (404) if the User row doesn't exist yet
        — means the Clerk webhook hasn't fired or failed silently.
    """
    clerk_user_id: str | None = getattr(request.state, "clerk_user_id", None)

    if not clerk_user_id:
        # This should never happen if AuthGuardMiddleware is running correctly.
        # Logging as error because it means something is wrong with middleware setup.
        logger.error(
            "get_current_user called with no clerk_user_id in request.state — "
            "AuthGuardMiddleware may not be running for this route",
            extra={
                "trace_id": getattr(request.state, "trace_id", "no-trace"),
                "path": request.url.path,
            },
        )
        raise UnauthorizedAccessException()

    user = await get_user_by_clerk_id(db, clerk_user_id)

    # Inject user_id into request.state so middleware logs can include it
    # in subsequent log lines (e.g. request_end log will now show user_id)
    request.state.user_id = str(user.id)

    return user


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """
    Like get_current_user but returns None instead of raising for public routes
    that optionally show extra data when logged in.

    Example: a public health endpoint that shows extra detail if authenticated.
    """
    clerk_user_id: str | None = getattr(request.state, "clerk_user_id", None)
    if not clerk_user_id:
        return None
    try:
        return await get_user_by_clerk_id(db, clerk_user_id)
    except Exception:
        return None