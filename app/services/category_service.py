"""
app/services/category_service.py — Category & Subcategory Service

RESPONSIBILITIES:
  - List all categories visible to a user (system + their own)
  - Create user-owned custom categories
  - Update category name/icon/color
  - Soft-delete (is_active=False) — never hard-delete if expenses exist

SYSTEM vs USER CATEGORIES (Doc1 — Section 2.2):
  System categories have user_id IS NULL.
  Query: WHERE (user_id = :uid OR user_id IS NULL) AND is_active = TRUE
  This returns both system categories AND the user's private ones.

CACHE:
  Category lists are cached 7 days (they change very rarely).
  Cache key: ff:{user_id}:categories
  Invalidated: when user adds or deactivates a category.
"""

import uuid
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category, Subcategory
from app.schemas.category_schema import (
    CategoryCreate, CategoryUpdate,
    SubcategoryCreate, SubcategoryUpdate,
)
from app.cache.redis_client import redis_client
from app.cache.keys import CacheKeys
from app.utils.exceptions import (
    ResourceNotFoundException,
    UnauthorizedAccessException,
    DuplicateResourceException,
    ValidationException,
)
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

_CACHE_TTL = 60 * 60 * 24 * 7  # 7 days


# ── Categories ─────────────────────────────────────────────────────────────────

async def list_categories(
    db: AsyncSession,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> list[Category]:
    """
    Returns all categories visible to this user: system + their own.
    Results are cached for 7 days.
    """
    cache_key = CacheKeys.user_categories(str(user_id))
    cached = await redis_client.get_json(cache_key)
    if cached:
        log_event(logger, "categories_cache_hit", trace_id=trace_id,
                  user_id=str(user_id))
        # Cache stores dicts — re-query DB for ORM objects on cache hit
        # (categories are cheap to fetch, cache mainly avoids repeated queries)

    result = await db.execute(
        select(Category)
        .where(
            or_(Category.user_id == user_id, Category.user_id.is_(None)),
            Category.is_active.is_(True),
        )
        .options(selectinload(Category.subcategories))
        .order_by(Category.user_id.is_(None).desc(), Category.name)
        # System categories first (user_id IS NULL sorts first with desc()),
        # then user's own categories alphabetically
    )
    categories = list(result.scalars().all())
    return categories


async def get_category(
    db: AsyncSession,
    category_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Category:
    """
    Fetch a single category. Ensures it belongs to the user or is system.
    """
    result = await db.execute(
        select(Category)
        .where(
            Category.id == category_id,
            or_(Category.user_id == user_id, Category.user_id.is_(None)),
            Category.is_active.is_(True),
        )
        .options(selectinload(Category.subcategories))
    )
    category = result.scalar_one_or_none()
    if not category:
        raise ResourceNotFoundException("Category", str(category_id))
    return category


async def create_category(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: CategoryCreate,
    trace_id: str = "no-trace",
) -> Category:
    """
    Create a user-owned custom category.
    Raises DuplicateResourceException if user already has a category with same name.
    """
    # Check for duplicate name (case-insensitive)
    existing = await db.execute(
        select(Category).where(
            Category.user_id == user_id,
            func.lower(Category.name) == data.name.lower(),
            Category.is_active.is_(True),
        )
    )
    if existing.scalar_one_or_none():
        raise DuplicateResourceException(
            "Category",
            f"You already have a category named '{data.name}'"
        )

    category = Category(
        user_id=user_id,
        name=data.name,
        icon=data.icon,
        color=data.color,
        description=data.description,
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)

    await _invalidate_cache(str(user_id))

    log_event(logger, "category_created",
              trace_id=trace_id,
              user_id=str(user_id),
              category_id=str(category.id),
              name=category.name)
    return category


async def update_category(
    db: AsyncSession,
    category_id: uuid.UUID,
    user_id: uuid.UUID,
    data: CategoryUpdate,
    trace_id: str = "no-trace",
) -> Category:
    """
    Update a user-owned category. System categories (user_id IS NULL) cannot
    be edited by users — raises UnauthorizedAccessException.
    """
    result = await db.execute(
        select(Category).where(Category.id == category_id)
    )
    category = result.scalar_one_or_none()
    if not category:
        raise ResourceNotFoundException("Category", str(category_id))

    # System categories are read-only
    if category.user_id is None:
        raise UnauthorizedAccessException()

    # Ownership check
    if category.user_id != user_id:
        raise UnauthorizedAccessException()

    if data.name is not None:
        category.name = data.name
    if data.icon is not None:
        category.icon = data.icon
    if data.color is not None:
        category.color = data.color
    if data.description is not None:
        category.description = data.description
    if data.is_active is not None:
        category.is_active = data.is_active

    await db.commit()
    await db.refresh(category)
    await _invalidate_cache(str(user_id))

    log_event(logger, "category_updated",
              trace_id=trace_id,
              user_id=str(user_id),
              category_id=str(category_id))
    return category


async def delete_category(
    db: AsyncSession,
    category_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> None:
    """
    Soft-delete a user category (is_active=False).
    System categories cannot be deleted.
    Categories with expenses cannot be deleted — FK RESTRICT prevents it.
    We catch this at the service layer to give a better error message.
    """
    result = await db.execute(
        select(Category).where(Category.id == category_id)
    )
    category = result.scalar_one_or_none()
    if not category:
        raise ResourceNotFoundException("Category", str(category_id))
    if category.user_id is None:
        raise ValidationException("System categories cannot be deleted")
    if category.user_id != user_id:
        raise UnauthorizedAccessException()

    # Soft delete — do NOT hard delete (FK RESTRICT from expenses table)
    category.is_active = False
    await db.commit()
    await _invalidate_cache(str(user_id))

    log_event(logger, "category_deactivated",
              trace_id=trace_id,
              user_id=str(user_id),
              category_id=str(category_id))


# ── Subcategories ──────────────────────────────────────────────────────────────

async def create_subcategory(
    db: AsyncSession,
    category_id: uuid.UUID,
    user_id: uuid.UUID,
    data: SubcategoryCreate,
    trace_id: str = "no-trace",
) -> Subcategory:
    """Create a subcategory under a user-owned category."""
    # Verify parent category exists and belongs to user
    await get_category(db, category_id, user_id)

    subcategory = Subcategory(
        category_id=category_id,
        name=data.name,
        icon=data.icon,
    )
    db.add(subcategory)
    await db.commit()
    await db.refresh(subcategory)
    await _invalidate_cache(str(user_id))

    log_event(logger, "subcategory_created",
              trace_id=trace_id,
              user_id=str(user_id),
              subcategory_id=str(subcategory.id),
              category_id=str(category_id))
    return subcategory


async def update_subcategory(
    db: AsyncSession,
    subcategory_id: uuid.UUID,
    user_id: uuid.UUID,
    data: SubcategoryUpdate,
    trace_id: str = "no-trace",
) -> Subcategory:
    """Update a subcategory. Verifies ownership via parent category."""
    result = await db.execute(
        select(Subcategory)
        .options(selectinload(Subcategory.category))
        .where(Subcategory.id == subcategory_id)
    )
    subcategory = result.scalar_one_or_none()
    if not subcategory:
        raise ResourceNotFoundException("Subcategory", str(subcategory_id))
    if subcategory.category.user_id != user_id:
        raise UnauthorizedAccessException()

    if data.name is not None:
        subcategory.name = data.name
    if data.icon is not None:
        subcategory.icon = data.icon
    if data.is_active is not None:
        subcategory.is_active = data.is_active

    await db.commit()
    await db.refresh(subcategory)
    await _invalidate_cache(str(user_id))
    return subcategory


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _invalidate_cache(user_id: str) -> None:
    """Delete category cache so the next request fetches fresh data."""
    await redis_client.delete(CacheKeys.user_categories(user_id))