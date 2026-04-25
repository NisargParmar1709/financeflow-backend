"""
app/middleware/auth_guard.py — Authentication Guard

WHY THIS FILE EXISTS:
  This is where Clerk authentication meets our backend. Every protected
  request passes through here before reaching any router.

WHAT CLERK DOES (the black box):
  1. User logs in via Clerk on the frontend
  2. Clerk issues a signed JWT (RS256 — asymmetric signing)
  3. Frontend sends this JWT in the Authorization header

WHAT WE DO (manually, not trusting the black box):
  1. Extract the token from "Authorization: Bearer <token>"
  2. Fetch Clerk's public key (JWKS endpoint — JSON Web Key Set)
  3. Verify the JWT signature using that public key
     → If signature is invalid → token was tampered with → reject
     → If token is expired → reject
     → If token is valid → extract the payload (clerk_user_id, email, etc.)
  4. Inject the decoded user data into request.state

WHY WE UNDERSTAND THE FLOW MANUALLY (even though Clerk SDK does it):
  - You should never use a library without understanding what it does
  - JWT verification is 3 steps: decode header → fetch public key → verify signature
  - The SDK calls Clerk's JWKS endpoint: https://api.clerk.com/v1/jwks
  - RS256 means Clerk signs with private key, we verify with public key
  - We never need Clerk's private key — only the public key for verification

REQUEST CONTEXT (from Video 10):
  After verification, user data is stored in request.state.user
  This is the "request context" — a temporary storage that lives for
  the duration of one request and is accessible across all layers
  (middleware → router → service) without passing it as a parameter.

ROUTES THAT SKIP AUTH:
  - GET /api/v1/health (monitoring ping)
  - POST /api/v1/auth/webhook (Clerk webhook — has its own Svix signature check)
  - GET /docs, GET /redoc, GET /openapi.json (API documentation)
"""

import logging
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from clerk_backend_api import Clerk
from clerk_backend_api.models import ClerkErrors

from app.config import settings

logger = logging.getLogger(__name__)

# ── Routes that bypass JWT authentication ─────────────────────────────────────
# Why a set: O(1) lookup — faster than list for repeated checks
PUBLIC_ROUTES: set[str] = {
    "/api/v1/health",
    "/api/v1/auth/webhook",   # Clerk webhook uses Svix signature, not JWT
    "/docs",
    "/redoc",
    "/openapi.json",
}


class AuthGuardMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that validates Clerk JWTs on every request.

    EXECUTION ORDER (Video 10 — Middleware chain):
      Request → AuthGuardMiddleware → RateLimitMiddleware → Router → Service

    Why BaseHTTPMiddleware:
      FastAPI is built on Starlette. BaseHTTPMiddleware lets us intercept
      every request/response at the ASGI level — before FastAPI routing.
    """

    def __init__(self, app):
        super().__init__(app)
        # Initialize the Clerk SDK client ONCE at middleware init.
        # Why not per-request: SDK initialization is expensive (loads config).
        # The client is stateless after init, safe to reuse across requests.
        self._clerk = Clerk(bearer_auth=settings.CLERK_SECRET_KEY)

    async def dispatch(self, request: Request, call_next):
        """
        Called on EVERY incoming request.

        Flow:
          1. Check if route is public → skip auth
          2. Extract Bearer token from Authorization header
          3. Validate token with Clerk SDK (which does JWKS fetch + RS256 verify)
          4. Inject decoded user into request.state
          5. Call next middleware/router
        """

        # ── Step 1: Skip auth for public routes ───────────────────────────────
        if self._is_public_route(request.url.path):
            return await call_next(request)

        # ── Step 2: Extract the token ─────────────────────────────────────────
        token = self._extract_bearer_token(request)
        if not token:
            return self._unauthorized("Missing or malformed Authorization header. "
                                       "Expected: 'Authorization: Bearer <token>'")

        # ── Step 3: Validate JWT with Clerk ───────────────────────────────────
        # What the SDK does internally (so you understand the manual flow):
        #   a) Decode the JWT header (base64) → gets {"alg": "RS256", "kid": "key_id"}
        #   b) Fetch Clerk's JWKS: GET https://api.clerk.com/v1/jwks
        #   c) Find the matching key by "kid" (key ID)
        #   d) Verify signature using the RSA public key
        #   e) Check exp (expiry), iss (issuer), and other standard JWT claims
        #   f) Return the decoded payload or raise an error
        try:
            request_state = await self._clerk.authenticate_request(
                request,
                # Passing the raw Authorization header value
                # The SDK handles the JWKS fetch and RS256 verification
            )

            if not request_state.is_signed_in:
                return self._unauthorized("Invalid or expired JWT token")

            # ── Step 4: Inject user context ───────────────────────────────────
            # request.state is FastAPI's request-scoped storage.
            # After this line, any router or service can access:
            #   request.state.clerk_user_id → "user_2abc123..."
            #   request.state.user_email → "rahul@example.com"
            payload = request_state.payload
            request.state.clerk_user_id = payload.get("sub")  # "sub" = Clerk user ID
            request.state.user_email = payload.get("email", "")

            # Why not fetch DB user here: We don't need full user data on every
            # request. Services that need it will fetch it themselves with the
            # clerk_user_id. This keeps middleware fast and focused.

            logger.debug(f"Auth OK: {request.state.clerk_user_id} → {request.url.path}")

        except ClerkErrors as e:
            # Clerk SDK raises ClerkErrors for invalid/expired tokens
            logger.warning(f"JWT validation failed: {e}")
            return self._unauthorized("Invalid token")
        except Exception as e:
            # Unexpected errors (network timeout fetching JWKS, etc.)
            logger.error(f"Auth middleware unexpected error: {e}")
            return self._unauthorized("Authentication failed")

        # ── Step 5: Pass to next middleware/router ────────────────────────────
        return await call_next(request)

    # ── Helper Methods ────────────────────────────────────────────────────────

    def _is_public_route(self, path: str) -> bool:
        """
        Checks if the path should skip authentication.
        Also allows OPTIONS requests (preflight CORS — Video 5).
        """
        return path in PUBLIC_ROUTES

    def _extract_bearer_token(self, request: Request) -> str | None:
        """
        Parses "Authorization: Bearer eyJhbGc..." → "eyJhbGc..."

        WHY MANUAL PARSING:
          The Authorization header format is: "Bearer <token>"
          We split on space and take the second part.
          If the header is missing or malformed, return None gracefully.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        parts = auth_header.split(" ", 1)
        return parts[1] if len(parts) == 2 else None

    def _unauthorized(self, detail: str) -> JSONResponse:
        """
        Returns a 401 Unauthorized response.

        SECURITY NOTE (Video 8 — Authentication):
          We use a slightly vague message ("Invalid token") rather than
          "Token expired" or "User not found" — giving attackers less
          information about why authentication failed.

          The detailed reason is logged server-side for debugging, but
          the client only gets a generic message.
        """
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": detail},
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Standalone dependency for individual route protection ──────────────────────
# Some routes need to access the current user inside the route handler.
# Use this as a FastAPI dependency to get the clerk_user_id cleanly.

from fastapi import Depends


async def get_current_user_id(request: Request) -> str:
    """
    FastAPI dependency — extracts clerk_user_id from request.state.

    The AuthGuardMiddleware already validated the token and set this value.
    This dependency is just a clean accessor so route handlers don't need
    to know about request.state directly.

    Usage in routers:
        @router.get("/expenses")
        async def list_expenses(
            user_id: str = Depends(get_current_user_id),
            db: AsyncSession = Depends(get_db),
        ):
            ...
    """
    clerk_user_id = getattr(request.state, "clerk_user_id", None)
    if not clerk_user_id:
        # This shouldn't happen if middleware is wired correctly —
        # but fail loudly if it does rather than silently using wrong user data
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return clerk_user_id