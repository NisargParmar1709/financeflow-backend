"""
app/models/category.py — Category & Subcategory Tables

WHY TWO-TIER (Doc1 — Section 2.2, 2.3):
  Category:    Food & Dining, Transport, Education, Medical
  Subcategory: Food > Chai, Food > Canteen, Transport > Auto, Transport > Bus

  Subcategories are optional. A user can track expenses at category level
  (just "Food") or go granular (Food > Samosa specifically).

SYSTEM vs USER CATEGORIES:
  System categories (user_id IS NULL) are seeded on first deploy:
    - Food & Dining, Transport, Groceries, Education, Medical...
    - Shared across ALL users — every new user sees them immediately
    - Cannot be deleted (is_active = FALSE to hide instead)

  User categories (user_id = UUID) are private to that user:
    - "Reading", "Gaming", "Girlfriend" — whatever they want
    - Deleted when user is deleted (cascade)

  Query pattern: `WHERE user_id = ? OR user_id IS NULL`
  This returns both system categories AND the user's own categories.

SOFT DELETE RULE (from Doc1):
  Categories with associated expenses CANNOT be hard-deleted.
  If a category has expenses and you delete it, what happens to expense.category_id?
  ON DELETE RESTRICT (the FK) prevents this — you must set is_active = FALSE instead.
  This preserves historical expense data while hiding the category from UI.

COLOR / ICON:
  Stored as strings. Color is a hex code (#F04438).
  Icon is an emoji stored as a Unicode string (🍛, 🚌, 📚).
  Why not an enum: too many possible icons, user should be able to pick any.
"""

import uuid
from typing import TYPE_CHECKING
from sqlalchemy import String, Boolean, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.expense import Expense
    from app.models.budget import Budget


class Category(TimestampMixin, Base):
    __tablename__ = "categories"

    # user_id IS NULL → system category (shared)
    # user_id IS NOT NULL → user-created (private)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(10), nullable=True)   # emoji
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)   # hex #RRGGBB
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Soft-delete: set is_active=False instead of hard-deleting
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    user: Mapped["User | None"] = relationship("User", back_populates="categories", lazy="noload")
    subcategories: Mapped[list["Subcategory"]] = relationship(
        "Subcategory",
        back_populates="category",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    expenses: Mapped[list["Expense"]] = relationship(
        "Expense",
        back_populates="category",
        lazy="noload",
    )
    budgets: Mapped[list["Budget"]] = relationship(
        "Budget",
        back_populates="category",
        lazy="noload",
    )


class Subcategory(TimestampMixin, Base):
    __tablename__ = "subcategories"

    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False,
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    category: Mapped["Category"] = relationship(
        "Category", back_populates="subcategories", lazy="noload",
    )
    expenses: Mapped[list["Expense"]] = relationship(
        "Expense", back_populates="subcategory", lazy="noload",
    )