"""
app/routers/__init__.py — re-exports all routers for clean imports in main.py
"""
from app.routers import (
    auth, expenses, incomes, accounts, budgets,
    groups, dues, analytics, ai, documents, notifications,
)