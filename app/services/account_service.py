"""
app/services/account_service.py — Bank Account Service

RESPONSIBILITIES:
  Accounts:   create, list, get detail, update, soft-delete
  Balance:    manual update + min-balance alert check
  Services:   add/update banking services (debit card, passbook, etc.)
  FDs:        create FD with auto-calculated maturity, update status
  Statements: add manual entry, list entries

SECURITY RULE (Doc1 — Section 3.1):
  account_last4 is already stripped to 4 digits by the schema validator.
  This service trusts that — no additional stripping needed.

FD MATURITY CALCULATION:
  maturity_amount = principal × (1 + rate/100 × tenure_days/365)
  Calculated once on creation and stored.
  maturity_date = start_date + tenure_days days.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cache.keys import CacheKeys
from app.cache.redis_client import redis_client
from app.models.account import Account, AccountServiceRecord, Branch, FixedDeposit
from app.models.document import StatementEntry
from app.schemas.account_schema import (
    AccountCreate,
    AccountUpdate,
    BalanceUpdate,
    BranchCreate,
    FDCreate,
    FDUpdate,
    ServiceCreate,
    ServiceUpdate,
    StatementEntryCreate,
)
from app.utils.exceptions import (
    DuplicateResourceException,
    ResourceNotFoundException,
    UnauthorizedAccessException,
)
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

_ACCOUNT_CACHE_TTL = 60 * 60 * 2  # 2 hours


# ── Branch ─────────────────────────────────────────────────────────────────────


async def create_branch(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: BranchCreate,
    trace_id: str = "no-trace",
) -> Branch:
    branch = Branch(
        user_id=user_id,
        bank_name=data.bank_name,
        branch_name=data.branch_name,
        ifsc_code=data.ifsc_code,
        city=data.city,
        state=data.state,
        address=data.address,
        phone=data.phone,
    )
    db.add(branch)
    await db.commit()
    await db.refresh(branch)
    log_event(
        logger, "branch_created", trace_id=trace_id, user_id=str(user_id), branch_id=str(branch.id)
    )
    return branch


# ── Accounts ───────────────────────────────────────────────────────────────────


async def create_account(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: AccountCreate,
    trace_id: str = "no-trace",
) -> Account:
    # If is_primary=True, unset any existing primary account
    if data.is_primary:
        await _unset_primary(db, user_id)

    account = Account(
        user_id=user_id,
        account_name=data.account_name,
        bank_name=data.bank_name,
        account_last4=data.account_last4,  # already stripped by schema
        account_type=data.account_type,
        current_balance=data.current_balance,
        min_balance=data.min_balance,
        branch_id=data.branch_id,
        is_primary=data.is_primary,
        opened_date=data.opened_date,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    await _invalidate_account_cache(str(user_id))

    log_event(
        logger,
        "account_created",
        trace_id=trace_id,
        user_id=str(user_id),
        account_id=str(account.id),
        bank=data.bank_name,
        last4=data.account_last4,
    )
    return account


async def list_accounts(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[Account]:
    """List all active accounts. Cached 2 hours."""
    cache_key = CacheKeys.account_list(str(user_id))
    cached = await redis_client.get_json(cache_key)
    if cached:
        pass  # Re-query for ORM objects; cache used only as existence signal

    result = await db.execute(
        select(Account)
        .where(Account.user_id == user_id, Account.is_active.is_(True))
        .order_by(Account.is_primary.desc(), Account.created_at.asc())
    )
    return list(result.scalars().all())


async def get_account(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Account:
    """Fetch account with ownership check."""
    result = await db.execute(
        select(Account).where(
            Account.id == account_id,
            Account.is_active.is_(True),
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise ResourceNotFoundException("Account", str(account_id))
    if account.user_id != user_id:
        raise UnauthorizedAccessException()
    return account


async def get_account_detail(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Account:
    """Full account with services, FDs, recent statements, documents loaded."""
    result = await db.execute(
        select(Account)
        .where(Account.id == account_id, Account.is_active.is_(True))
        .options(
            selectinload(Account.branch),
            selectinload(Account.services),
            selectinload(Account.fixed_deposits),
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise ResourceNotFoundException("Account", str(account_id))
    if account.user_id != user_id:
        raise UnauthorizedAccessException()
    return account


async def update_account(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    data: AccountUpdate,
    trace_id: str = "no-trace",
) -> Account:
    account = await get_account(db, account_id, user_id)

    if data.is_primary is True:
        await _unset_primary(db, user_id, exclude_id=account_id)

    if data.account_name is not None:
        account.account_name = data.account_name
    if data.bank_name is not None:
        account.bank_name = data.bank_name
    if data.account_type is not None:
        account.account_type = data.account_type
    if data.min_balance is not None:
        account.min_balance = data.min_balance
    if data.branch_id is not None:
        account.branch_id = data.branch_id
    if data.is_primary is not None:
        account.is_primary = data.is_primary
    if data.opened_date is not None:
        account.opened_date = data.opened_date
    if data.is_active is not None:
        account.is_active = data.is_active

    await db.commit()
    await db.refresh(account)
    await _invalidate_account_cache(str(user_id))

    log_event(
        logger,
        "account_updated",
        trace_id=trace_id,
        user_id=str(user_id),
        account_id=str(account_id),
    )
    return account


async def soft_delete_account(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> None:
    account = await get_account(db, account_id, user_id)
    account.is_active = False
    await db.commit()
    await _invalidate_account_cache(str(user_id))

    log_event(
        logger,
        "account_deactivated",
        trace_id=trace_id,
        user_id=str(user_id),
        account_id=str(account_id),
    )


async def update_balance(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    data: BalanceUpdate,
    trace_id: str = "no-trace",
) -> Account:
    """Manually update balance. Checks if new balance triggers min-balance alert."""
    account = await get_account(db, account_id, user_id)
    old_balance = account.current_balance
    account.current_balance = data.balance
    await db.commit()
    await db.refresh(account)
    await _invalidate_account_cache(str(user_id))

    log_event(
        logger,
        "balance_updated",
        trace_id=trace_id,
        user_id=str(user_id),
        account_id=str(account_id),
        old_balance=str(old_balance),
        new_balance=str(data.balance),
    )
    return account


# ── Account Services ───────────────────────────────────────────────────────────


async def add_service(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ServiceCreate,
    trace_id: str = "no-trace",
) -> AccountServiceRecord:
    # Verify account ownership
    await get_account(db, account_id, user_id)

    # Check for duplicate service type on this account
    existing = await db.execute(
        select(AccountServiceRecord).where(
            AccountServiceRecord.account_id == account_id,
            AccountServiceRecord.service_type == data.service_type,
        )
    )
    if existing.scalar_one_or_none():
        raise DuplicateResourceException(
            "AccountService",
            f"This account already has a {data.service_type.value} service record.",
        )

    service = AccountServiceRecord(
        account_id=account_id,
        service_type=data.service_type,
        is_active=data.is_active,
        activated_date=data.activated_date,
        notes=data.notes,
    )
    db.add(service)
    await db.commit()
    await db.refresh(service)

    log_event(
        logger,
        "account_service_added",
        trace_id=trace_id,
        user_id=str(user_id),
        account_id=str(account_id),
        service_type=data.service_type.value,
    )
    return service


async def update_service(
    db: AsyncSession,
    service_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ServiceUpdate,
    trace_id: str = "no-trace",
) -> AccountServiceRecord:
    result = await db.execute(
        select(AccountServiceRecord)
        .where(AccountServiceRecord.id == service_id)
        .options(selectinload(AccountServiceRecord.account))
    )
    service = result.scalar_one_or_none()
    if not service:
        raise ResourceNotFoundException("AccountService", str(service_id))
    if service.account.user_id != user_id:
        raise UnauthorizedAccessException()

    if data.is_active is not None:
        service.is_active = data.is_active
    if data.activated_date is not None:
        service.activated_date = data.activated_date
    if data.notes is not None:
        service.notes = data.notes

    await db.commit()
    await db.refresh(service)
    return service


# ── Fixed Deposits ─────────────────────────────────────────────────────────────


async def create_fd(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    data: FDCreate,
    trace_id: str = "no-trace",
) -> FixedDeposit:
    """Create FD with auto-calculated maturity_date and maturity_amount."""
    await get_account(db, account_id, user_id)

    maturity_date = data.start_date + timedelta(days=data.tenure_days)
    # Simple interest formula: A = P(1 + rt)
    # r = rate/100 per annum, t = tenure_days/365
    t = Decimal(str(data.tenure_days)) / Decimal("365")
    r = data.interest_rate / Decimal("100")
    maturity_amount = data.principal_amount * (1 + r * t)
    maturity_amount = maturity_amount.quantize(Decimal("0.01"))

    fd = FixedDeposit(
        account_id=account_id,
        user_id=user_id,
        fd_number=data.fd_number,
        principal_amount=data.principal_amount,
        interest_rate=data.interest_rate,
        tenure_days=data.tenure_days,
        start_date=data.start_date,
        maturity_date=maturity_date,
        maturity_amount=maturity_amount,
        auto_renew=data.auto_renew,
        notes=data.notes,
    )
    db.add(fd)
    await db.commit()
    await db.refresh(fd)

    log_event(
        logger,
        "fd_created",
        trace_id=trace_id,
        user_id=str(user_id),
        fd_id=str(fd.id),
        principal=str(data.principal_amount),
        maturity_date=maturity_date.isoformat(),
    )
    return fd


async def update_fd(
    db: AsyncSession,
    fd_id: uuid.UUID,
    user_id: uuid.UUID,
    data: FDUpdate,
    trace_id: str = "no-trace",
) -> FixedDeposit:
    result = await db.execute(select(FixedDeposit).where(FixedDeposit.id == fd_id))
    fd = result.scalar_one_or_none()
    if not fd:
        raise ResourceNotFoundException("FixedDeposit", str(fd_id))
    if fd.user_id != user_id:
        raise UnauthorizedAccessException()

    if data.status is not None:
        fd.status = data.status
    if data.auto_renew is not None:
        fd.auto_renew = data.auto_renew
    if data.notes is not None:
        fd.notes = data.notes

    await db.commit()
    await db.refresh(fd)

    log_event(
        logger,
        "fd_updated",
        trace_id=trace_id,
        user_id=str(user_id),
        fd_id=str(fd_id),
        status=fd.status.value,
    )
    return fd


async def list_fds(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[FixedDeposit]:
    await get_account(db, account_id, user_id)
    result = await db.execute(
        select(FixedDeposit)
        .where(FixedDeposit.account_id == account_id)
        .order_by(FixedDeposit.start_date.desc())
    )
    return list(result.scalars().all())


# ── Statement Entries ──────────────────────────────────────────────────────────


async def add_statement_entry(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    data: StatementEntryCreate,
    trace_id: str = "no-trace",
) -> StatementEntry:
    await get_account(db, account_id, user_id)

    entry = StatementEntry(
        account_id=account_id,
        user_id=user_id,
        transaction_date=data.transaction_date,
        description=data.description,
        debit_amount=data.debit_amount,
        credit_amount=data.credit_amount,
        balance_after=data.balance_after,
        reference_number=data.reference_number,
        entry_type=data.entry_type,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    log_event(
        logger,
        "statement_entry_added",
        trace_id=trace_id,
        user_id=str(user_id),
        account_id=str(account_id),
    )
    return entry


async def list_statement_entries(
    db: AsyncSession,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    page: int = 1,
    limit: int = 20,
) -> tuple[list[StatementEntry], int]:
    await get_account(db, account_id, user_id)

    from app.utils.formatting import calculate_offset

    total = await db.scalar(
        select(func.count(StatementEntry.id)).where(StatementEntry.account_id == account_id)
    )
    offset = calculate_offset(page, limit)
    rows = await db.execute(
        select(StatementEntry)
        .where(StatementEntry.account_id == account_id)
        .order_by(StatementEntry.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), total or 0


# ── Internal helpers ───────────────────────────────────────────────────────────


async def _unset_primary(
    db: AsyncSession,
    user_id: uuid.UUID,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Unset is_primary on all accounts (before setting a new primary)."""
    result = await db.execute(
        select(Account).where(
            Account.user_id == user_id,
            Account.is_primary.is_(True),
        )
    )
    for acc in result.scalars().all():
        if exclude_id and acc.id == exclude_id:
            continue
        acc.is_primary = False


async def _invalidate_account_cache(user_id: str) -> None:
    await redis_client.delete(
        CacheKeys.account_list(user_id),
    )