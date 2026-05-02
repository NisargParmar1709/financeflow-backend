"""
app/services/group_service.py — Group Expense Service

RESPONSIBILITIES:
  Groups:        create, list, get detail, update, archive
  Members:       add member to group
  Expenses:      create group expense with split validation
  Splits:        settle individual split
  Balances:      compute net balance per member

SPLIT VALIDATION RULES (Doc1 — Section 5.3, Doc4 — Section 7.4):
  EQUAL:      auto-calculate each member's share = total / member_count
              payer's split is auto-marked as settled
  PERCENTAGE: splits[].percentage must sum to exactly 100.00
  EXACT:      splits[].amount must sum to exactly total_amount

  All validation is done in this service — NOT in the model or schema.
  The schema collects raw input; we validate the aggregates here.
"""

import uuid
from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import PaymentMode, SplitType
from app.models.expense import Expense
from app.models.group import Group, GroupExpense, GroupMember, GroupSplit
from app.schemas.group_schema import (
    GroupCreate,
    GroupExpenseCreate,
    GroupUpdate,
    MemberCreate,
    SettleSplit,
)
from app.utils.exceptions import (
    ResourceNotFoundException,
    UnauthorizedAccessException,
    ValidationException,
)
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


# ── Groups ─────────────────────────────────────────────────────────────────────


async def create_group(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: GroupCreate,
    trace_id: str = "no-trace",
) -> Group:
    group = Group(
        created_by=user_id,
        name=data.name,
        description=data.description,
    )
    db.add(group)
    await db.flush()  # Get group.id before adding members

    # Auto-add creator as admin member
    creator_member = GroupMember(
        group_id=group.id,
        user_id=user_id,
        name="You",
        is_admin=True,
    )
    db.add(creator_member)

    # Add initial members
    for m in data.members:
        member = GroupMember(
            group_id=group.id,
            user_id=m.user_id,
            name=m.name,
            phone=m.phone,
            is_admin=False,
        )
        db.add(member)

    await db.commit()
    await db.refresh(group)

    log_event(
        logger,
        "group_created",
        trace_id=trace_id,
        user_id=str(user_id),
        group_id=str(group.id),
        name=group.name,
        member_count=len(data.members) + 1,
    )
    return group


async def list_groups(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[Group]:
    """All groups where this user is a member."""
    result = await db.execute(
        select(Group)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .where(GroupMember.user_id == user_id, Group.is_active.is_(True))
        .options(selectinload(Group.members))
        .order_by(Group.created_at.desc())
    )
    return list(result.scalars().unique().all())


async def get_group(
    db: AsyncSession,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Group:
    result = await db.execute(
        select(Group)
        .where(Group.id == group_id)
        .options(
            selectinload(Group.members),
            selectinload(Group.expenses).selectinload(GroupExpense.splits),
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise ResourceNotFoundException("Group", str(group_id))

    # Check membership
    member_ids = {str(m.user_id) for m in group.members}
    if str(user_id) not in member_ids:
        raise UnauthorizedAccessException()
    return group


async def update_group(
    db: AsyncSession,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    data: GroupUpdate,
    trace_id: str = "no-trace",
) -> Group:
    group = await get_group(db, group_id, user_id)

    # Only admin can update
    admin_ids = {str(m.user_id) for m in group.members if m.is_admin}
    if str(user_id) not in admin_ids:
        raise UnauthorizedAccessException()

    if data.name is not None:
        group.name = data.name
    if data.description is not None:
        group.description = data.description
    if data.is_active is not None:
        group.is_active = data.is_active

    await db.commit()
    await db.refresh(group)
    return group


async def add_member(
    db: AsyncSession,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    data: MemberCreate,
    trace_id: str = "no-trace",
) -> GroupMember:
    group = await get_group(db, group_id, user_id)

    member = GroupMember(
        group_id=group.id,
        user_id=data.user_id,
        name=data.name,
        phone=data.phone,
        is_admin=False,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)

    log_event(
        logger,
        "group_member_added",
        trace_id=trace_id,
        user_id=str(user_id),
        group_id=str(group_id),
        member=data.name,
    )
    return member


# ── Group Expenses ─────────────────────────────────────────────────────────────


async def create_group_expense(
    db: AsyncSession,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    data: GroupExpenseCreate,
    trace_id: str = "no-trace",
) -> GroupExpense:
    """
    Create a group expense with split validation.

    EQUAL:      auto-divide, no per-member input needed
    PERCENTAGE: validate percentages sum to 100
    EXACT:      validate amounts sum to total
    """
    group = await get_group(db, group_id, user_id)
    members = group.members
    member_ids = {str(m.id): m for m in members}

    # Validate paid_by_member_id is in this group
    if str(data.paid_by_member_id) not in member_ids:
        raise ValidationException(
            "paid_by_member_id must belong to this group",
            "paid_by_member_id",
        )

    # ── Compute splits ─────────────────────────────────────────────────────────
    splits_to_create: list[dict[str, object]] = []

    if data.split_type == SplitType.EQUAL:
        count = len(members)
        per_person = (data.total_amount / count).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        # Handle rounding remainder: add to payer's split
        remainder = data.total_amount - (per_person * count)
        for i, member in enumerate(members):
            amount = per_person + (remainder if i == 0 else Decimal("0"))
            splits_to_create.append(
                {
                    "member_id": member.id,
                    "amount": amount,
                    "percentage": None,
                    "is_settled": member.id == data.paid_by_member_id,
                }
            )

    elif data.split_type == SplitType.PERCENTAGE:
        total_pct = sum(s.percentage or Decimal("0") for s in data.splits)
        if abs(total_pct - Decimal("100")) > Decimal("0.01"):
            raise ValidationException(
                f"Percentages must sum to 100. Got {total_pct}.",
                "splits",
            )
        for s in data.splits:
            pct = s.percentage or Decimal("0")
            amount = (data.total_amount * pct / 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            splits_to_create.append(
                {
                    "member_id": s.member_id,
                    "amount": amount,
                    "percentage": s.percentage,
                    "is_settled": s.member_id == data.paid_by_member_id,
                }
            )

    elif data.split_type == SplitType.EXACT:
        total_splits = sum(s.amount or Decimal("0") for s in data.splits)
        if abs(total_splits - data.total_amount) > Decimal("0.01"):
            raise ValidationException(
                f"Split amounts ({total_splits}) must equal total ({data.total_amount}).",
                "splits",
            )
        for s in data.splits:
            splits_to_create.append(
                {
                    "member_id": s.member_id,
                    "amount": s.amount,
                    "percentage": None,
                    "is_settled": s.member_id == data.paid_by_member_id,
                }
            )

    # ── Create group expense ───────────────────────────────────────────────────
    group_expense = GroupExpense(
        group_id=group_id,
        paid_by_member_id=data.paid_by_member_id,
        total_amount=data.total_amount,
        description=data.description,
        split_type=data.split_type,
        payment_mode=data.payment_mode,
        expense_date=data.expense_date,
        notes=data.notes,
    )
    db.add(group_expense)
    await db.flush()

    # ── Create splits ──────────────────────────────────────────────────────────
    for split_data in splits_to_create:
        split = GroupSplit(
            group_expense_id=group_expense.id,
            member_id=split_data["member_id"],
            amount=split_data["amount"],
            percentage=split_data["percentage"],
            is_settled=split_data["is_settled"],
        )
        db.add(split)

    # ── Create personal expense for payer ──────────────────────────────────────
    payer_member = member_ids[str(data.paid_by_member_id)]
    if payer_member.user_id:
        personal_expense = Expense(
            user_id=payer_member.user_id,
            amount=data.total_amount,
            expense_date=data.expense_date,
            description=f"[Group: {group.name}] {data.description}",
            payment_mode=data.payment_mode or PaymentMode.CASH,
            group_expense_id=group_expense.id,
            is_split=True,
            # category_id — use a default "Groups" category or leave None
            # Services can set this based on user preference
        )
        db.add(personal_expense)

    await db.commit()
    await db.refresh(group_expense)

    log_event(
        logger,
        "group_expense_created",
        trace_id=trace_id,
        user_id=str(user_id),
        group_id=str(group_id),
        expense_id=str(group_expense.id),
        amount=str(data.total_amount),
        split_type=data.split_type.value,
    )
    return group_expense


async def settle_split(
    db: AsyncSession,
    split_id: uuid.UUID,
    user_id: uuid.UUID,
    data: SettleSplit,
    trace_id: str = "no-trace",
) -> GroupSplit:
    """
    Mark a split as settled. If all splits for the expense are settled,
    mark the group_expense as settled too.
    """
    result = await db.execute(
        select(GroupSplit)
        .where(GroupSplit.id == split_id)
        .options(selectinload(GroupSplit.group_expense))
    )
    split = result.scalar_one_or_none()
    if not split:
        raise ResourceNotFoundException("GroupSplit", str(split_id))

    from datetime import datetime

    split.is_settled = True
    split.settled_at = datetime.now(UTC).isoformat()
    if data.settlement_note:
        split.settlement_note = data.settlement_note

    # Check if all splits for this expense are now settled
    all_splits = await db.execute(
        select(GroupSplit).where(GroupSplit.group_expense_id == split.group_expense_id)
    )
    all_splits_list = list(all_splits.scalars().all())
    all_settled = all(s.is_settled or s.id == split_id for s in all_splits_list)
    if all_settled:
        split.group_expense.is_settled = True

    await db.commit()
    await db.refresh(split)

    log_event(
        logger,
        "split_settled",
        trace_id=trace_id,
        user_id=str(user_id),
        split_id=str(split_id),
        expense_id=str(split.group_expense_id),
    )
    return split


async def get_group_balances(
    db: AsyncSession,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[dict]:
    """
    Net balance per member.
    Positive = others owe this member. Negative = this member owes others.
    net = total_paid - total_owed
    """
    group = await get_group(db, group_id, user_id)
    members = {str(m.id): m for m in group.members}
    balances: dict[str, dict] = {
        mid: {
            "member_id": mid,
            "member_name": m.name,
            "user_id": str(m.user_id) if m.user_id else None,
            "total_paid": Decimal("0"),
            "total_owed": Decimal("0"),
        }
        for mid, m in members.items()
    }

    # What each member paid
    paid_rows = await db.execute(
        select(GroupExpense.paid_by_member_id, func.sum(GroupExpense.total_amount))
        .where(GroupExpense.group_id == group_id, GroupExpense.is_settled.is_(False))
        .group_by(GroupExpense.paid_by_member_id)
    )
    for member_id, total_paid in paid_rows.all():
        mid = str(member_id)
        if mid in balances:
            balances[mid]["total_paid"] = Decimal(str(total_paid))

    # What each member owes (unsettled splits)
    owed_rows = await db.execute(
        select(GroupSplit.member_id, func.sum(GroupSplit.amount))
        .join(GroupExpense, GroupExpense.id == GroupSplit.group_expense_id)
        .where(
            GroupExpense.group_id == group_id,
            GroupSplit.is_settled.is_(False),
        )
        .group_by(GroupSplit.member_id)
    )
    for member_id, total_owed in owed_rows.all():
        mid = str(member_id)
        if mid in balances:
            balances[mid]["total_owed"] = Decimal(str(total_owed))

    result = []
    for b in balances.values():
        net = b["total_paid"] - b["total_owed"]
        result.append(
            {
                **b,
                "net_balance": str(net),
                "total_paid": str(b["total_paid"]),
                "total_owed": str(b["total_owed"]),
            }
        )

    return sorted(result, key=lambda x: Decimal(x["net_balance"]), reverse=True)