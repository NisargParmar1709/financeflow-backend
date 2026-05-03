"""
app/schemas/category_schema.py — Category & Subcategory Schemas

COVERS (Doc2 — Section 3.x):
  GET  /categories             → list[CategoryResponse]
  POST /categories             → CategoryCreate → CategoryResponse
  PATCH /categories/{id}       → CategoryUpdate → CategoryResponse
  POST /categories/{id}/subcategories → SubcategoryCreate → SubcategoryResponse

DESIGN NOTES:
  - System categories (user_id IS NULL) are visible to all users.
    They are NOT returned with user_id — the frontend doesn't need
    to know if a category is system or user-created for most operations.
  - icon is an emoji string. No enum — too many possible icons.
  - color is a 7-char hex string (#F04438). Validated by a field_validator.
  - CategoryBrief is a lightweight nested object embedded inside
    ExpenseResponse — it avoids deep joins by returning just id/name/icon/color.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# ── Subcategory ────────────────────────────────────────────────────────────────


class SubcategoryCreate(BaseModel):
    """POST /categories/{id}/subcategories"""

    name: str = Field(..., min_length=1, max_length=100)
    icon: str | None = Field(None, max_length=10)


class SubcategoryUpdate(BaseModel):
    """PATCH /categories/{id}/subcategories/{sub_id}"""

    name: str | None = Field(None, min_length=1, max_length=100)
    icon: str | None = Field(None, max_length=10)
    is_active: bool | None = None


class SubcategoryResponse(BaseModel):
    """Single subcategory as returned by the API."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    category_id: uuid.UUID
    name: str
    icon: str | None
    is_active: bool


class SubcategoryBrief(BaseModel):
    """
    Minimal subcategory used when nested inside ExpenseResponse.
    Avoids sending the full object when only id+name are needed.
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    icon: str | None


# ── Category ───────────────────────────────────────────────────────────────────


class CategoryCreate(BaseModel):
    """POST /categories — create a user-owned custom category."""

    name: str = Field(..., min_length=1, max_length=100)
    icon: str | None = Field(None, max_length=10, description="Emoji icon, e.g. 🍛")
    color: str | None = Field(None, description="Hex color, e.g. #F04438")
    description: str | None = Field(None, max_length=500)

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        """Ensure color is a valid 7-character hex string like #F04438."""
        if v is None:
            return v
        if not (len(v) == 7 and v.startswith("#")):
            raise ValueError("color must be a 7-character hex string like #F04438")
        try:
            int(v[1:], 16)
        except ValueError:
            raise ValueError("color must be a valid hex color like #F04438")
        return v.upper()


class CategoryUpdate(BaseModel):
    """PATCH /categories/{id} — partial update of a user-owned category."""

    name: str | None = Field(None, min_length=1, max_length=100)
    icon: str | None = Field(None, max_length=10)
    color: str | None = None
    description: str | None = None
    is_active: bool | None = None

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not (len(v) == 7 and v.startswith("#")):
            raise ValueError("color must be a 7-character hex string like #F04438")
        try:
            int(v[1:], 16)
        except ValueError:
            raise ValueError("color must be a valid hex color like #F04438")
        return v.upper()


class CategoryBrief(BaseModel):
    """
    Lightweight category info embedded inside other responses.
    Used in ExpenseResponse so the frontend doesn't need a separate
    category lookup to render the expense row.
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    icon: str | None
    color: str | None


class CategoryResponse(BaseModel):
    """Full category object returned by list and detail endpoints."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID | None  # None = system category
    name: str
    icon: str | None
    color: str | None
    description: str | None
    is_active: bool
    is_system: bool  # True for built-in categories that can't be deleted (user_id IS NULL)

    # Subcategories only included on detail endpoints, not list
    subcategories: list[SubcategoryResponse] = []

    created_at: datetime
    updated_at: datetime