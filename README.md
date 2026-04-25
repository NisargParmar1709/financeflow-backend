# FinanceFlow — Backend API

> Personal Finance & Bank Account Manager — built for Indian students
> managing expenses, income, bank accounts, budgets, dues, and groups.

---

## Architecture Overview

```
Browser (React SPA)
    │ HTTPS + Bearer JWT (Clerk-issued)
    ▼
FastAPI Server (Render)
    ├── Routers      → HTTP interface only (no logic)
    ├── Services     → All business logic lives here
    ├── Models       → SQLAlchemy ORM table definitions
    ├── Schemas      → Pydantic request/response shapes
    ├── Database     → asyncpg connection pool via SQLAlchemy
    ├── Cache        → Redis (Upstash serverless) via redis-py async
    ├── Middleware   → CORS, auth guard, error handler, rate limiter
    └── Workers      → Background jobs (email, AI, report generation)
    │
    ├── PostgreSQL (Neon.tech)   — all persistent data
    ├── Redis (Upstash)          — computed cache, rate limits, sessions
    ├── Cloudinary               — document/image file storage
    └── Google Gemini            — AI receipt scanning + insights
```

**Auth flow (Clerk + manual understanding):**
1. User logs in via Clerk on frontend → Clerk issues a signed JWT
2. Frontend sends JWT in `Authorization: Bearer <token>` header
3. Our `auth_guard` middleware verifies the JWT signature using Clerk's public key
4. Decoded `clerk_user_id` is injected into `request.state` (request context)
5. Every protected route reads `request.state.user` — never touches the DB for auth

**Why Clerk instead of custom JWT?**
Clerk handles token issuance, refresh, revocation, MFA, and social login.
We still validate JWTs manually (not trusting a black box) so we understand
exactly what `python-clerk-backend-api` does under the hood.

---

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| Framework | FastAPI 0.111+ | Async-first, auto OpenAPI docs, Python type hints |
| Python | 3.12 | Latest stable, required for `type X = Y` syntax |
| ORM | SQLAlchemy 2.0 async | True async queries, no blocking the event loop |
| DB Driver | asyncpg | Fastest async Postgres driver |
| Migrations | Alembic | Version-controlled schema changes |
| Auth | Clerk JWT (python-clerk-backend-api) | JWT validation without custom auth server |
| Cache | Redis via Upstash (redis-py async) | Serverless Redis, no server management |
| File Storage | Cloudinary Python SDK v2 | CDN-backed file storage with transformations |
| AI | Google Generative AI SDK | Gemini for receipt OCR and spending insights |
| Email | Resend | Simple transactional email API |
| Testing | pytest + pytest-asyncio + httpx | Full async test support |
| Deployment | Render (Dockerfile) | Simple PaaS, free tier available |

---

## Local Development Setup

### Prerequisites
- Python 3.12+
- PostgreSQL (or a Neon.tech account)
- Redis (or an Upstash account)

### Steps

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/financeflow-backend.git
cd financeflow-backend

# 2. Create virtual environment (always use venv, never global pip)
python3.12 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy env template and fill in your values
cp .env.example .env
# Now edit .env with your actual keys

# 5. Run database migrations
alembic upgrade head

# 6. Start the dev server
uvicorn app.main:app --reload --port 8000
```

API docs auto-available at:
- Swagger UI → http://localhost:8000/docs
- ReDoc      → http://localhost:8000/redoc

---

## Environment Variables

See `.env.example` for the full list. **Never commit `.env`.**

Critical variables that will crash the app on startup if missing:
- `DATABASE_URL` — Neon Postgres connection string
- `REDIS_URL` — Upstash Redis connection string  
- `CLERK_SECRET_KEY` — for JWT validation
- `CLERK_PUBLISHABLE_KEY` — for webhook verification

---

## Project Structure

```
financeflow-backend/
├── app/
│   ├── main.py              ← FastAPI app factory, middleware, router registration
│   ├── config.py            ← Pydantic Settings — reads + validates all env vars
│   ├── routers/             ← HTTP layer ONLY (one file per resource)
│   ├── services/            ← Business logic (one file per resource)
│   ├── models/              ← SQLAlchemy ORM table definitions
│   ├── schemas/             ← Pydantic request/response models
│   ├── database/            ← Engine, session factory, get_db dependency
│   ├── cache/               ← Redis client, key builders, cache helpers
│   ├── middleware/          ← auth guard, error handler, rate limiter
│   ├── utils/               ← Shared helpers (INR formatting, dates, validators)
│   └── workers/             ← Background job functions (email, AI, reports)
├── tests/
│   ├── unit/                ← Pure function tests (no DB, no network)
│   ├── integration/         ← Full endpoint tests with test DB
│   └── fixtures/            ← Shared pytest fixtures
├── scripts/                 ← One-off utility scripts (seed data, cleanup)
├── docs/                    ← Architecture diagrams, API design decisions
├── migrations/              ← Alembic migration files (auto-generated)
│   └── versions/
├── .github/workflows/       ← CI/CD pipelines
├── Dockerfile               ← Production container definition
├── render.yaml              ← Render deployment config
├── requirements.txt         ← Pinned production dependencies
├── requirements-dev.txt     ← Dev/test only dependencies
├── alembic.ini              ← Alembic config
├── .env.example             ← Template — copy to .env and fill values
└── README.md
```

---

## Running Tests

```bash
# All tests
pytest

# With coverage report
pytest --cov=app --cov-report=html

# Only unit tests (fast, no DB needed)
pytest tests/unit/

# Only integration tests (needs test DB)
pytest tests/integration/
```

---

## Deployment

Render reads `render.yaml` from the repo root. Every push to `main`
triggers an automatic deploy. The Dockerfile defines the production image.

Health check endpoint (pinged every 14 min to prevent cold starts):
```
GET /api/v1/health
```