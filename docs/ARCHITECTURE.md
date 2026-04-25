# FinanceFlow Backend — Architecture Decisions

> **Audience:** Any developer (including future-you) opening this codebase
> for the first time. Explains *why* decisions were made, not just *what* was built.

---

## Layer Architecture

```
HTTP Request
     │
     ▼
┌─────────────────────────────────────────┐
│  Middleware Layer                        │
│  1. CORSMiddleware (Starlette built-in) │
│  2. AuthGuardMiddleware (JWT validate)  │
│  3. RateLimiterMiddleware (Redis INCR)  │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌────────────────────────────────────────┐
│  Router Layer  (app/routers/)          │
│  • HTTP concerns only                  │
│  • Extract path/query params           │
│  • Call service → return response      │
└────────────────┬───────────────────────┘
                 │
                 ▼
┌────────────────────────────────────────┐
│  Service Layer  (app/services/)        │
│  • ALL business logic lives here       │
│  • Orchestrates models + cache         │
│  • No HTTP awareness (no Request obj)  │
└──────┬──────────────────┬─────────────┘
       │                  │
       ▼                  ▼
┌─────────────┐    ┌──────────────┐
│ Model Layer │    │  Cache Layer │
│ (SQLAlchemy)│    │  (Redis)     │
└──────┬──────┘    └──────────────┘
       │
       ▼
┌─────────────┐
│  PostgreSQL │
│  (Neon.tech)│
└─────────────┘
```

---

## Key Decisions

### Why FastAPI over Django/Flask?

- **Async-first**: FastAPI is built on Starlette (async ASGI). Django and Flask
  are traditionally synchronous. Async matters for FinanceFlow because most
  operations are I/O bound (DB queries, Redis reads, Gemini API calls). Async
  means one process can handle many concurrent requests without blocking.
- **Auto-generated docs**: FastAPI generates `/docs` (Swagger) and `/redoc`
  from Pydantic schemas automatically. No maintenance overhead.
- **Pydantic v2**: 5-10x faster than v1. All request validation is free performance.

### Why PostgreSQL over MongoDB?

FinanceFlow's data is **highly relational**:
- Expenses belong to users
- Group expenses split among members
- Bank accounts link to transactions
- Budgets reference expense categories

Relational databases enforce these relationships with foreign keys and
referential integrity. MongoDB's flexibility would mean bugs in our code
could silently create orphaned records or inconsistent states.

### Why Clerk over custom JWT auth?

We use Clerk but validate JWTs manually (we don't trust the black box).

Clerk handles: MFA, social login (Google), session management, token refresh,
user management UI. Building this from scratch for a solo project is months of work.

We still understand the flow: JWT → RS256 signature → JWKS endpoint → verify.
See `app/middleware/auth_guard.py` for the full annotated implementation.

### Why Redis (Upstash) for caching?

- **Shared cache**: Multiple Render instances share one Redis. In-memory dict
  cache would be per-instance — instance A caches, instance B misses.
- **Rate limiting**: Redis INCR is atomic — race-condition-free counters.
- **Serverless**: Upstash Redis has no persistent connection requirement,
  which pairs well with Neon's serverless Postgres.

### Why async SQLAlchemy?

The main alternative is psycopg2 (synchronous). With psycopg2, every DB query
blocks the entire event loop — meaning while one request waits for Postgres,
ALL other requests on that server wait too.

asyncpg + SQLAlchemy async means: while waiting for Postgres, the event loop
handles other requests. For I/O-bound workloads, this multiplies throughput.

---

## Request Lifecycle (full trace)

1. User clicks "Add Expense" on React frontend
2. React calls `POST /api/v1/expenses` with JWT in Authorization header
3. **CORS middleware** checks if origin is in CORS_ORIGINS → allows
4. **AuthGuard middleware** extracts JWT → verifies with Clerk JWKS → injects `clerk_user_id` into `request.state`
5. **RateLimiter middleware** → Redis INCR on IP → checks < 100/min
6. **Router** (`expenses.router`) → extracts body → calls `ExpenseService.create()`
7. **Service** → validates business rules (budget check, duplicate check)
8. **Service** → calls `session.add(new_expense)` + `session.commit()`
9. **Service** → invalidates Redis cache keys for this user's expense list
10. **Router** → returns `201 Created` with the new expense as JSON
11. Pydantic serializes the response → `Content-Type: application/json`
12. Response travels back through middleware stack
13. React receives `201` → updates Zustand store → re-renders expense list

---

## Data Flow: Cache-aside Pattern

```
GET /api/v1/expenses?page=1

Service.list_expenses()
    │
    ├──→ redis.get_json("financeflow:expenses:user_abc:list:p1:l20")
    │         │
    │    HIT  │   MISS
    │    ←────┘    │
    │              ▼
    │         DB query (SELECT * FROM expenses ...)
    │              │
    │              ▼
    │         redis.set_json(key, data, ttl=60)
    │              │
    └──────────────▼
         return expense_list
```

---

## Environment Separation

| Variable | Development | Production |
|---|---|---|
| `DEBUG` | true | false |
| `APP_ENV` | development | production |
| `/docs` | visible | hidden |
| SQL logging | enabled (echo=True) | disabled |
| Error messages | full detail | generic |
| CORS origins | localhost:5173 | financeflow.vercel.app |

---

## File Naming Convention

| Pattern | Example | Rule |
|---|---|---|
| Routers | `expenses.py` | Plural noun, snake_case |
| Services | `expense_service.py` | Singular noun + _service |
| Models | `expense.py` | Singular noun |
| Schemas | `expense_schema.py` | Singular noun + _schema |
| Tests | `test_expense_service.py` | test_ prefix, mirrors source path |