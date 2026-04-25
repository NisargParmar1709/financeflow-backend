"""
app/models/user.py — User Table

WHY THIS TABLE EXISTS (Doc1 — Section 2.1):
  Clerk handles authentication. But our DB needs a 'users' table because:
    1. Foreign keys: expenses.user_id → users.id
       PostgreSQL FK must reference a real row in a real table.
       We can't FK reference Clerk's external system.
    2. App-specific data: notification_prefs, avatar_url, display_name
       are our data, not Clerk's.
    3. Query joins: "get all expenses for this user" requires user.id
       to exist in our DB.

HOW IT'S POPULATED:
  When a user signs up on the frontend, Clerk sends a 'user.created'
  webhook to POST /api/v1/auth/webhook. Our webhook handler creates a
  row in this table. This happens within seconds of signup.

  Edge case: if the webhook fails, the user can authenticate (Clerk works)
  but our DB has no row for them. The auth_guard middleware will return 404.
  The webhook endpoint has retry logic — Clerk resends failed webhooks.

NEVER STORES:
  - Passwords (Clerk handles hashing + storage)
  - Full bank account numbers (only last 4 digits, in accounts table)
  - Raw JWT tokens

notification_prefs JSONB shape (documented in Doc1):
  {
    "budget_alert": true,    ← alert when budget is exceeded
    "min_balance": true,     ← alert when account balance drops below threshold
    "due_reminder": true,    ← reminder for pending dues
    "weekly_summary": false, ← weekly spending summary email
    "monthly_report": true   ← monthly financial report email
  }
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, func, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    # TYPE_CHECKING block prevents circular imports at runtime.
    # These imports are only used by mypy/IDE for type hints.
    from app.models.expense import Expense
    from app.models.income import Income
    from app.models.account import Account
    from app.models.budget import Budget
    from app.models.group import Group, GroupMember
    from app.models.due import Due
    from app.models.notification import Notification
    from app.models.category import Category


class User(TimestampMixin, Base):
    """
    Central identity table. One row per registered user.

    Inherits from TimestampMixin: gets id (UUID PK), created_at, updated_at.
    """

    __tablename__ = "users"

    # ── Clerk Identity ────────────────────────────────────────────────────────
    clerk_user_id: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        # Why index: auth_guard does `WHERE clerk_user_id = ?` on EVERY request.
        # Without an index this is a sequential scan over the entire users table.
    )

    # ── Profile ───────────────────────────────────────────────────────────────
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )

    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Cloudinary URL — null until user uploads an avatar
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── App State ─────────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
        # Why server_default: if a migration inserts rows directly via SQL,
        # the Python default=True won't apply — server_default guarantees it.
    )

    # Soft-delete flag. We never hard-delete users — financial data must
    # be retained. Setting is_deleted=True hides the user from all queries.
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )

    # ── Preferences ───────────────────────────────────────────────────────────
    # JSONB: flexible schema for notification preferences.
    # Why JSONB instead of separate boolean columns:
    #   - New notification types can be added without schema migrations
    #   - Frontend can manage the entire prefs object as one API call
    #   - JSONB is indexed and queryable in Postgres (unlike TEXT)
    notification_prefs: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default='{"budget_alert": true, "min_balance": true, '
                       '"due_reminder": true, "weekly_summary": false, '
                       '"monthly_report": true}',
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    # lazy="noload" is the default for async — prevents accidental sync access.
    # All relationship loading must be explicit via selectinload() in queries.
    expenses: Mapped[list["Expense"]] = relationship(
        "Expense",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    incomes: Mapped[list["Income"]] = relationship(
        "Income",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    accounts: Mapped[list["Account"]] = relationship(
        "Account",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    budgets: Mapped[list["Budget"]] = relationship(
        "Budget",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    dues: Mapped[list["Due"]] = relationship(
        "Due",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    notifications: Mapped[list["Notification"]] = relationship(
        "Notification",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    categories: Mapped[list["Category"]] = relationship(
        "Category",
        back_populates="user",
        lazy="noload",
    )