"""
app/models/__init__.py — Model Registry

RULE: Every new model file MUST be imported here.
Forgetting this = Alembic misses the table = missing migration.     
"""
from app.models.base import Base, TimestampMixin  # noqa: F401
from app.models.enums import (  # noqa: F401
    PaymentMode, IncomeSource, AccountType, BudgetPeriod,
    FDStatus, AccountService, DocType, DueType, SplitType, EntryType,
)
from app.models.user import User  # noqa: F401
from app.models.category import Category, Subcategory  # noqa: F401
from app.models.account import Branch, Account, AccountServiceRecord, FixedDeposit  # noqa: F401
from app.models.expense import Expense  # noqa: F401
from app.models.income import Income  # noqa: F401
from app.models.budget import Budget  # noqa: F401
from app.models.group import Group, GroupMember, GroupExpense, GroupSplit  # noqa: F401
from app.models.due import Due  # noqa: F401
from app.models.document import Document, StatementEntry  # noqa: F401
from app.models.notification import Notification, AIInsight, AIChatSession  # noqa: F401