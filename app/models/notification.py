"""
app/models/notification.py — Notification & AI Tables (Doc1 — Section 6)

NOTIFICATION DESIGN:
  All alerts stored here. Frontend polls GET /api/v1/notifications?unread=true.
  Partial index on (user_id, created_at DESC) WHERE is_read = FALSE
  makes this query fast even with thousands of notifications.

AI INSIGHT DESIGN:
  Gemini calls are expensive (latency + API cost). We never call Gemini
  on every request. Instead:
    1. Generate insight → store in ai_insights table
    2. Serve from DB until expires_at
    3. User can manually trigger regeneration

  This is cache-at-the-DB-level (different from Redis cache which is for
  speed). AI insights need persistence across server restarts.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy import String, Boolean, ForeignKey, Text, DateTime, func, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class Notification(TimestampMixin, Base):
    __tablename__ = "notifications"

    __table_args__ = (
        # Partial index: fast unread query (most common access pattern)
        Index("idx_notifications_user_unread", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Notification types: "BUDGET_EXCEEDED", "MIN_BALANCE", "DUE_REMINDER",
    # "WEEKLY_SUMMARY", "MONTHLY_REPORT", "FD_MATURITY"
    notification_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Extra data for the frontend to render rich notifications
    # e.g. {"category": "Food", "limit": 3000, "spent": 3250, "budget_id": "..."}
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True,
        # Column named "metadata_" in Python (avoids conflict with SQLAlchemy metadata)
        # but stored as "metadata" in Postgres
    )

    is_read: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    user: Mapped["User"] = relationship(
        "User", back_populates="notifications", lazy="noload",
    )


class AIInsight(TimestampMixin, Base):
    """
    Cached Gemini AI insights. Generated once, served until expires_at.
    User can force regeneration via POST /api/v1/ai/insights/regenerate.
    """
    __tablename__ = "ai_insights"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # Insight type: "MONTHLY_SUMMARY", "SPENDING_PATTERN", "SAVING_TIP"
    insight_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # The generated insight text from Gemini
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Structured data used to generate this insight (for debugging/audit)
    input_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # When this insight becomes stale — service checks this before serving
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    # Token usage for cost tracking
    tokens_used: Mapped[int | None] = mapped_column(nullable=True)


class AIChatSession(TimestampMixin, Base):
    """
    Multi-turn chat conversations with Gemini.
    History stored so user can continue conversations across sessions.
    """
    __tablename__ = "ai_chat_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Full conversation history as JSONB array of {role, content} objects
    # [{role: "user", content: "..."}, {role: "model", content: "..."}]
    history: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False,
    )
    total_tokens_used: Mapped[int | None] = mapped_column(nullable=True)