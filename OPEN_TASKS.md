# FinanceFlow Backend — Open Tasks

## Stage 3: Services (Next)

- [ ] `app/services/category_service.py`
- [ ] `app/services/expense_service.py` — budget check, cache invalidation
- [ ] `app/services/income_service.py`
- [ ] `app/services/account_service.py` — FD maturity calc, balance alerts
- [ ] `app/services/budget_service.py` — real-time status computation
- [ ] `app/services/group_service.py` — split validation
- [ ] `app/services/due_service.py`
- [ ] `app/services/analytics_service.py` — all SQL + Redis caching
- [ ] `app/services/notification_service.py`
- [ ] `app/services/ai_service.py`
- [ ] `app/services/auth_service.py` — webhook handler, onboarding
- [ ] `app/database/session.py` — get_db dependency

## Stage 4: Routers

- [ ] `app/routers/auth.py`
- [ ] `app/routers/expenses.py`
- [ ] `app/routers/incomes.py`
- [ ] `app/routers/accounts.py`
- [ ] `app/routers/budgets.py`
- [ ] `app/routers/groups.py`
- [ ] `app/routers/dues.py`
- [ ] `app/routers/analytics.py`
- [ ] `app/routers/ai.py`
- [ ] `app/routers/documents.py`
- [ ] `app/routers/notifications.py`
- [ ] `app/dependencies.py` — get_current_user FastAPI dependency

## Stage 5: Tests

- [ ] `tests/unit/test_expense_schemas.py`
- [ ] `tests/unit/test_expense_service.py`
- [ ] `tests/unit/test_budget_service.py`
- [ ] `tests/unit/test_group_service.py`
- [ ] `tests/unit/test_analytics_service.py`

## Stage 6: Migrations

- [ ] Initial Alembic migration (all 18 tables + enums + indexes)
- [ ] Seed migration (system categories)
- [ ] `alembic.ini` and `migrations/env.py` setup

## Known Issues / Decisions Needed

- `account_schema.py` — FDResponse has a `days_to_maturity` property but Pydantic
  doesn't auto-include properties in serialization. Decide: computed_field or
  compute in service and return as regular field.
- `category_schema.py` — `is_system` defined both as a model field and a property.
  Clean this up when building CategoryResponse in the service layer.
- `app/models/account.py` has TYPE_CHECKING import `from app.models.statement`
  but the module is `app.models.document`. Already works (StatementEntry is in
  document.py) but the comment is misleading — fix in a cleanup pass.