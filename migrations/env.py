"""
migrations/env.py — Alembic Migration Environment

WHY THIS FILE IS CUSTOMIZED:
  Alembic's default env.py is synchronous. Our app uses async SQLAlchemy
  (asyncpg). We need to configure Alembic to run migrations using an
  async engine — otherwise `alembic upgrade head` would fail to connect.

  This file also imports our `Base.metadata` so Alembic can detect model
  changes and generate accurate `--autogenerate` diffs.

CRITICAL IMPORT:
  We import ALL models below so Alembic knows about every table.
  If a model is not imported here, Alembic won't detect it and won't
  generate the correct migration — it will think the table doesn't exist.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ── Import app config and Base ────────────────────────────────────────────────
# Use sys.path manipulation if running alembic from project root
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.database.connection import Base

# ── CRITICAL: Import ALL models here ─────────────────────────────────────────
# Each import registers the model's table with Base.metadata.
# Alembic reads Base.metadata to detect schema changes.
# Missing import = missing table in autogenerate.
#
# Add new model imports here as you create them:
# from app.models.user import User
# from app.models.expense import Expense
# from app.models.income import Income
# (Uncomment as models are created)

# ─────────────────────────────────────────────────────────────────────────────

# Alembic Config object from alembic.ini
config = context.config

# Override the sqlalchemy.url with our settings value.
# Why: alembic.ini has a placeholder URL. The real URL comes from .env.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Set up logging from alembic.ini [loggers] sections
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata Alembic uses for autogenerate ("--autogenerate" flag)
target_metadata = Base.metadata


# ── Offline Mode (generates SQL without connecting) ───────────────────────────
def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Why: Useful for generating raw SQL scripts to review before applying.
    Command: alembic upgrade head --sql > migration.sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # compare_type=True: detect column type changes in autogenerate
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online Mode (actually connects and runs migrations) ───────────────────────
def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # compare_type=True: detect column type changes (e.g., VARCHAR → TEXT)
        compare_type=True,
        # include_schemas: if we ever use Postgres schemas (not just public)
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations using an async engine.

    Why async: Our app uses asyncpg. Alembic's default sync approach
    would require a separate psycopg2 dependency just for migrations.
    Using the same async engine keeps dependency count down.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool: migrations are run once, not continuously.
        # We don't need connection pooling for a one-shot migration run.
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migration mode."""
    asyncio.run(run_async_migrations())


# ── Entry Point ────────────────────────────────────────────────────────────────
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()