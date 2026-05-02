"""
app/services/auth_service.py — Auth & User Service

RESPONSIBILITIES:
  1. Handle Clerk webhook events (user.created, user.updated, user.deleted)
  2. Create/update/soft-delete our internal User row
  3. Complete user onboarding (mark wizard done)
  4. Fetch the current user by clerk_user_id

WHY THIS SERVICE EXISTS:
  Clerk owns authentication (passwords, sessions, JWTs).
  We own user data that Clerk doesn't store:
    - notification_prefs, avatar_url, display_name
    - Foreign keys: expenses.user_id → users.id
  This service is the bridge between Clerk identity and our DB rows.

WEBHOOK FLOW (Doc4 — Section 4.1):
  User signs up on frontend
    → Clerk sends POST /api/v1/auth/webhook (signed with Svix)
    → auth_guard skips JWT check (public route)
    → Router verifies Svix signature
    → handle_clerk_webhook() is called here
    → User row created in our DB
    → System categories seeded for this user
"""

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.category import Category
from app.schemas.user_schema import ClerkWebhookData, UserUpdate
from app.utils.exceptions import ResourceNotFoundException
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

# ── System categories seeded for every new user ────────────────────────────────
# These are REFERENCES to system categories (user_id IS NULL).
# We do NOT insert new rows — system categories are shared across all users.
# This constant is used only to verify they exist; seeding is in migrations.


async def get_user_by_clerk_id(
    db: AsyncSession,
    clerk_user_id: str,
) -> User:
    """
    Fetch our internal User row by Clerk's user ID.

    Called by:
      - Every authenticated route (via get_current_user dependency)
      - After webhook events to confirm user exists

    Raises:
      ResourceNotFoundException if no row found (webhook may have failed).
    """
    result = await db.execute(
        select(User).where(
            User.clerk_user_id == clerk_user_id,
            User.is_deleted.is_(False),
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        raise ResourceNotFoundException("User", clerk_user_id)
    return user


async def get_user_by_id(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> User:
    """Fetch user by our internal UUID. Used in services that already have user_id."""
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.is_deleted.is_(False),
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        raise ResourceNotFoundException("User", str(user_id))
    return user


async def handle_user_created(
    db: AsyncSession,
    data: ClerkWebhookData,
    trace_id: str = "no-trace",
) -> User:
    """
    Called when Clerk fires the 'user.created' webhook.

    Creates a new User row with defaults. The user's notification prefs
    are set to sensible defaults (all important alerts on).

    IDEMPOTENT: if the webhook fires twice (Clerk retries on failure),
    we check for existing clerk_user_id first to avoid duplicates.
    """
    # Idempotency check — webhook may be retried
    existing = await db.execute(
        select(User).where(User.clerk_user_id == data.id)
    )
    existing_user = existing.scalar_one_or_none()
    if existing_user:
        log_event(logger, "webhook_user_already_exists",
                  trace_id=trace_id, clerk_user_id=data.id)
        return existing_user

    user = User(
        clerk_user_id=data.id,
        email=data.primary_email,
        full_name=data.full_name or None,
        avatar_url=data.image_url,
        notification_prefs={
            "budget_alert": True,
            "min_balance": True,
            "due_reminder": True,
            "weekly_summary": False,
            "monthly_report": True,
        },
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    log_event(logger, "user_created",
              trace_id=trace_id,
              user_id=str(user.id),
              clerk_user_id=data.id,
              email=user.email)
    return user


async def handle_user_updated(
    db: AsyncSession,
    data: ClerkWebhookData,
    trace_id: str = "no-trace",
) -> User | None:
    """
    Called when Clerk fires 'user.updated'.
    Updates name, email, avatar if they changed.
    """
    result = await db.execute(
        select(User).where(User.clerk_user_id == data.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        log_event(logger, "webhook_user_not_found_for_update",
                  trace_id=trace_id, clerk_user_id=data.id, level="warning")
        return None

    user.email = data.primary_email
    if data.full_name:
        user.full_name = data.full_name
    if data.image_url:
        user.avatar_url = data.image_url

    await db.commit()
    await db.refresh(user)

    log_event(logger, "user_updated",
              trace_id=trace_id,
              user_id=str(user.id),
              clerk_user_id=data.id)
    return user


async def handle_user_deleted(
    db: AsyncSession,
    clerk_user_id: str,
    trace_id: str = "no-trace",
) -> None:
    """
    Called when Clerk fires 'user.deleted'.
    Soft-deletes — we never hard-delete users (preserves financial history).
    """
    result = await db.execute(
        select(User).where(User.clerk_user_id == clerk_user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        return

    user.is_deleted = True
    user.is_active = False
    await db.commit()

    log_event(logger, "user_soft_deleted",
              trace_id=trace_id,
              user_id=str(user.id),
              clerk_user_id=clerk_user_id,
              level="warning")


async def update_user_profile(
    db: AsyncSession,
    user: User,
    data: UserUpdate,
    trace_id: str = "no-trace",
) -> User:
    """
    PATCH /auth/me — partial update of user profile.
    Only fields present in `data` are updated.
    """
    if data.full_name is not None:
        user.full_name = data.full_name
    if data.display_name is not None:
        user.display_name = data.display_name
    if data.notification_prefs is not None:
        user.notification_prefs = data.notification_prefs.model_dump()

    await db.commit()
    await db.refresh(user)

    log_event(logger, "user_profile_updated",
              trace_id=trace_id,
              user_id=str(user.id))
    return user


async def complete_onboarding(
    db: AsyncSession,
    user: User,
    trace_id: str = "no-trace",
) -> User:
    """
    POST /auth/complete-onboarding — marks the setup wizard as done.
    After this, the frontend never shows the onboarding wizard again.
    """
    # Nothing extra to do currently — future: accept currency pref etc.
    await db.commit()
    await db.refresh(user)

    log_event(logger, "onboarding_completed",
              trace_id=trace_id,
              user_id=str(user.id))
    return user