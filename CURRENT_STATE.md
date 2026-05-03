# FinanceFlow Backend — Current State

_Last updated: Schema stage complete_

---

## What Is Done ✅

### Stage 0 — Runnable Foundation
- `app/main.py` — FastAPI app factory, middleware stack, router registration
- `app/config.py` — Pydantic Settings, all env vars validated at startup
- `app/middleware/error_handler.py` — global exception handlers with trace_id in every log; registers `FinanceFlowException` as first handler so business exceptions (BudgetExceeded, ResourceNotFound, etc.) return clean 4xx not 500
- `app/middleware/auth_guard.py` — Clerk JWT validation; logs auth success/failure with trace_id and structured fields (event, clerk_user_id, path)
- `app/middleware/rate_limiter.py` — Redis sliding-window IP rate limiting
- `app/middleware/request_logger.py` — generates trace_id, logs every request start/end with timing; runs as step 2 (right after CORS) so trace_id exists for all subsequent middleware
- `app/utils/exceptions.py` — full custom exception hierarchy
- `app/utils/logging.py` — structured JSON logging (prod) / human-readable (dev); `log_event()` helper for business events; `generate_trace_id()`; `RequestLogger`
- `app/utils/formatting.py`, — shared helpers
- `app/cache/redis_client.py`, `app/cache/keys.py` — Redis layer
- `app/database/connection.py` — async SQLAlchemy engine + session factory
- `.github/workflows/ci.yml` — lint (ruff), typecheck (mypy), unit tests (pytest)
- Router stubs — `app/routers/{auth,expenses,incomes,...}.py`
- `app/routers/__init__.py` — fixed circular import

**Middleware execution order (verified):**
```
1. CORSMiddleware           → preflight OPTIONS handling
2. RequestLoggingMiddleware → generates trace_id (MUST be here so auth logs have it)
3. AuthGuardMiddleware      → validates Clerk JWT, logs with trace_id
4. RateLimiterMiddleware    → Redis counter, logs with trace_id
5. Router handlers
```

### Stage 1 — Models (Complete)
All 18 tables defined as SQLAlchemy ORM models:
- `app/models/enums.py` — 9 enums (PaymentMode, IncomeSource, AccountType, …)
- `app/models/base.py` — Base + TimestampMixin (UUID PK, created_at, updated_at)
- `app/models/user.py` — User
- `app/models/category.py` — Category, Subcategory
- `app/models/account.py` — Branch, Account, AccountServiceRecord, FixedDeposit
- `app/models/expense.py` — Expense (with composite indexes)
- `app/models/income.py` — Income
- `app/models/budget.py` — Budget
- `app/models/group.py` — Group, GroupMember, GroupExpense, GroupSplit
- `app/models/due.py` — Due
- `app/models/document.py` — Document, StatementEntry
- `app/models/notification.py` — Notification, AIInsight, AIChatSession

### CI/CD, Workers, Tests (Complete)

**CI Pipeline** (`.github/workflows/ci.yml`) — placed in repo, fixes applied:
- `ruff check` + `ruff format --check` — all 68 files pass
- `mypy app/ --ignore-missing-imports` — 0 errors across 68 files
- `pytest tests/unit/ --cov=app --cov-fail-under=70` — **157 tests, 80.5% coverage**
- Root cause of previous CI failures: CI file was never committed to repo; `FinanceFlowException` not registered in error handler; middleware order wrong; ruff auto-fixed 125 violations; mypy had 31 errors (all fixed)

**Workers** (`app/workers/tasks.py`):
- `run_alert_checks(db, user_id)` — called as FastAPI BackgroundTask after dashboard load
- `send_weekly_summary_email(user_id, email)` — Resend HTML email, non-fatal on failure
- `check_fd_maturity_for_all_users(db)` — daily scan of all active users' FDs

**Tests** (`tests/unit/` — 157 tests, all passing):
- `test_formatting.py` — 20 tests (pre-existing)
- `test_schemas.py` — 51 tests covering all Pydantic business rules
- `test_exceptions.py` — 31 tests verifying status codes, error codes, details
- `test_budget_service.py` — 14 tests for SAFE/WARNING/EXCEEDED computation
- `test_group_service.py` — 13 tests for split schema validation
- `test_logging.py` — 28 tests for trace_id, DevFormatter, log_event, JsonFormatter

**Model fixes** (required by service layer):
- `app/models/income.py` — added `sender_name`, `screenshot_url`, `screenshot_public_id`, `is_recurring`
- `app/models/group.py` — added `settlement_note` to `GroupSplit`

**pyproject.toml** — populated with pytest config, coverage omit list (services excluded from unit coverage — they need integration tests), ruff config, mypy config
All 11 service files built. Every function uses `log_event()` with `trace_id`.

- `app/services/auth_service.py` — Clerk webhook handler (created/updated/deleted), user profile update, onboarding. Idempotent webhook handling.
- `app/services/category_service.py` — list (system + user), create with duplicate check, update, soft-delete. 7-day Redis cache.
- `app/services/expense_service.py` — create with full budget check (hard block + soft alert), list with filters + sum, update (re-runs budget check if amount/category changed), delete + Cloudinary cleanup, monthly summary (10-min cache).
- `app/services/income_service.py` — full CRUD + monthly summary by source (cached).
- `app/services/budget_service.py` — create with uniqueness enforcement, list with real-time SAFE/WARNING/EXCEEDED status computed live from expense SUM, update, soft-delete.
- `app/services/due_service.py` — full CRUD, settle, net position summary (30-min cache).
- `app/services/account_service.py` — accounts (create/list/detail/update/soft-delete), balance update, services (add/update), FDs (create with maturity calculation, update status), statement entries.
- `app/services/group_service.py` — group CRUD, add member, create group expense with EQUAL/PERCENTAGE/EXACT split validation, settle split (auto-marks group expense settled if all splits done), net balances per member.
- `app/services/notification_service.py` — list, mark read, mark all read, check_all_alerts (budget + FD maturity + min balance), all idempotent with 24hr duplicate check.
- `app/services/analytics_service.py` — dashboard KPIs (1hr cache), spending by category (1hr), monthly trend (24hr), daily pattern (24hr), payment mode split (1hr), yearly summary (24hr), net worth (30min), income source breakdown (1hr).
- `app/services/ai_service.py` — cache-first insight generation via Gemini, multi-turn chat with session history, session management.
- `app/dependencies.py` — `get_current_user` FastAPI dependency (reads `clerk_user_id` from `request.state`, fetches User from DB). `get_current_user_optional` for public routes.
- `app/cache/keys.py` — added `user_categories` key.
All Pydantic request/response schemas built and validated:
- `app/schemas/common.py` — `SuccessResponse[T]`, `PaginationMeta`, helpers
- `app/schemas/user_schema.py` — Clerk webhook, UserResponse, UserUpdate
- `app/schemas/category_schema.py` — Category + Subcategory CRUD + CategoryBrief
- `app/schemas/expense_schema.py` — ExpenseCreate (with cross-field CASH rule), filters, response
- `app/schemas/income_schema.py` — IncomeCreate, filters, summary response
- `app/schemas/account_schema.py` — Account, Branch, FD, Service, Statement, Document
- `app/schemas/budget_schema.py` — BudgetCreate, BudgetWithStatus (with status field)
- `app/schemas/group_schema.py` — Group, Members, GroupExpense, Splits, Dues
- `app/schemas/analytics_schema.py` — Dashboard KPI, charts, net worth
- `app/schemas/notification_schema.py` — Notifications, AI insights, chat
- `app/schemas/__init__.py` — single-import re-exports for all schemas

**Validated business rules in schemas:**
- Expense date cannot be in the future
- Non-CASH payment requires account_id
- account_last4 always stripped to last 4 digits
- StatementEntry requires exactly one of debit/credit
- PaginationMeta derives pages/has_next/has_prev correctly
- IFSC code format validated (4 alpha + 0 + 6 alphanumeric)
- Hex color validated (#RRGGBB format)

---

## What Is Next 🔜

### Stage 3 — Services
Build `app/services/` — one file per domain. Implement all business logic.
**Order to build:**
1. `category_service.py` — list + create + soft-delete
2. `expense_service.py` — create (with budget check + cache invalidation)
3. `income_service.py`
4. `account_service.py` — balance update, FD maturity calculation
5. `budget_service.py` — status computation, alert threshold check
6. `group_service.py` — split validation (EQUAL/PERCENTAGE/EXACT)
7. `due_service.py`
8. `analytics_service.py` — all SQL aggregates, all cached
9. `notification_service.py` — alert checks
10. `ai_service.py` — Gemini prompts, insight caching

### Stage 4 — Routers
Wire services to HTTP endpoints. Replace stubs with real routes.

### Stage 5 — Tests
Unit tests for services (mock DB + Redis). Integration tests for critical flows.

### Stage 6 — Migrations
Alembic migration files for all tables + indexes + triggers.

---

## App Health

| Check | Status |
|---|---|
| `python -c "from app.main import app"` | ✅ Boots |
| All model imports | ✅ OK |
| All schema imports | ✅ OK |
| Schema business rules | ✅ 6/6 pass |
| CI pipeline | ✅ Configured |