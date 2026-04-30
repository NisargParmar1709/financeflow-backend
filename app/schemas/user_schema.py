"""
app/schemas/user_schema.py — User Request & Response Schemas

COVERS (Doc2 — Section 3.1):
  POST /auth/webhook/clerk     → ClerkWebhookPayload
  GET  /auth/me                → UserResponse
  PATCH /auth/me               → UserUpdate
  POST /auth/complete-onboarding → OnboardingComplete

DESIGN NOTES:
  - ClerkWebhookPayload maps Clerk's raw webhook event shape.
    We only extract the fields we care about — extra fields are ignored.
  - UserResponse is what the frontend receives on GET /auth/me.
    It NEVER includes clerk_user_id (internal concern, not for clients).
  - notification_prefs is typed as a dict — its keys are validated
    by a model_validator so typos in keys are caught at the schema layer.

SECURITY:
  - password is never stored or returned — Clerk handles it
  - clerk_user_id is in the DB but NEVER in any response schema
    (it's an internal join key, not a public identifier)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, model_validator


# ── Clerk Webhook ──────────────────────────────────────────────────────────────

class ClerkEmailAddress(BaseModel):
    """Single email address object inside Clerk's user.created payload."""

    id: str
    email_address: str


class ClerkWebhookData(BaseModel):
    """
    The 'data' field inside a Clerk webhook event.
    Clerk sends many fields — we only extract what we need.

    model_config extra="ignore" is critical here: Clerk's payload has dozens
    of fields we don't use. Without 'ignore', Pydantic would raise a
    ValidationError for every extra field.
    """

    model_config = {"extra": "ignore"}

    id: str  # Clerk user ID, e.g. "user_2abc123"
    email_addresses: list[ClerkEmailAddress]
    first_name: str | None = None
    last_name: str | None = None
    image_url: str | None = None

    @property
    def primary_email(self) -> str:
        """Return the first email address from the list."""
        return self.email_addresses[0].email_address if self.email_addresses else ""

    @property
    def full_name(self) -> str:
        """Combine first + last name, falling back to empty string."""
        parts = [p for p in [self.first_name, self.last_name] if p]
        return " ".join(parts)


class ClerkWebhookPayload(BaseModel):
    """
    Root shape of the Clerk webhook POST body.

    type tells us which event fired:
      'user.created'  → create user row in DB
      'user.updated'  → update email / name
      'user.deleted'  → soft-delete user
    """

    model_config = {"extra": "ignore"}

    type: str  # "user.created" | "user.updated" | "user.deleted"
    data: ClerkWebhookData


# ── Notification Preferences ───────────────────────────────────────────────────

class NotificationPrefs(BaseModel):
    """
    Typed wrapper for notification_prefs JSONB column.

    Stored as JSONB in DB, but validated here so typos in keys are caught.
    Default: most alerts on, email digests off (students check the app).
    """

    budget_alert: bool = True
    min_balance: bool = True
    due_reminder: bool = True
    weekly_summary: bool = False
    monthly_report: bool = True


# ── Request Schemas ────────────────────────────────────────────────────────────

class UserUpdate(BaseModel):
    """
    PATCH /auth/me — partial update of user profile.

    All fields optional — send only what you want to change.
    """

    full_name: str | None = Field(None, max_length=255)
    display_name: str | None = Field(None, max_length=100)
    notification_prefs: NotificationPrefs | None = None


class OnboardingComplete(BaseModel):
    """
    POST /auth/complete-onboarding

    Called once when the user finishes the setup wizard.
    After this, onboarding_completed = True and the wizard never shows again.
    """

    # 'seed_default_categories' removed — categories are always seeded on
    # user creation via the webhook handler. No choice needed here.
    pass


# ── Response Schemas ───────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    """
    Returned by GET /auth/me and PATCH /auth/me.

    NEVER includes: clerk_user_id (internal), password (doesn't exist),
    is_deleted (internal soft-delete flag).

    model_config from_attributes=True: allows building this from a
    SQLAlchemy User ORM object directly:
        UserResponse.model_validate(user_orm_object)
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    email: str
    full_name: str | None
    display_name: str | None
    avatar_url: str | None
    is_active: bool
    notification_prefs: dict[str, Any]
    created_at: datetime
    updated_at: datetime