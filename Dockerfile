# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile — FinanceFlow Backend
#
# MULTI-STAGE BUILD — Why:
#   Stage 1 (builder): Installs ALL dependencies including build tools
#                      (gcc, libpq-dev for asyncpg compilation). Build tools
#                      add ~300MB but are only needed at compile time.
#   Stage 2 (final):   Copies only the compiled packages from stage 1.
#                      Final image has NO build tools — smaller and more secure.
#
# RESULT: ~250MB final image vs ~550MB single-stage
#
# SECURITY — Non-root user:
#   By default, Docker containers run as root. If the app is compromised,
#   the attacker has root inside the container — dangerous.
#   We create a "financeflow" user with no special privileges.
#   The app runs as this user — compromise gets limited Linux permissions.
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install system dependencies needed to COMPILE Python packages
# (asyncpg requires libpq-dev, argon2-cffi requires gcc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment in a known location
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only requirements first — Docker layer caching:
# If requirements.txt hasn't changed, Docker skips pip install on next build.
# This makes rebuilds after code changes 10x faster.
COPY requirements.txt .

# Install production dependencies into the venv
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt


# ── Stage 2: Final Image ──────────────────────────────────────────────────────
FROM python:3.12-slim AS final

# Runtime system dependencies only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy the compiled venv from builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ── Security: Non-root user ───────────────────────────────────────────────────
# Create a system user with no login shell and no home directory
RUN groupadd --system financeflow && \
    useradd --system --no-create-home --gid financeflow financeflow

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=financeflow:financeflow . .

# Switch to non-root user before starting the app
USER financeflow

# ── Port ──────────────────────────────────────────────────────────────────────
# Render injects PORT env var. We expose 8000 as documentation.
# Render's router handles the public port mapping.
EXPOSE 8000

# ── Health Check ──────────────────────────────────────────────────────────────
# Docker and Render use this to verify the container is healthy.
# If /api/v1/health returns non-200, Render will restart the container.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/api/v1/health').raise_for_status()"

# ── Start Command ─────────────────────────────────────────────────────────────
# uvicorn: ASGI server
# app.main:app → the `app` object inside app/main.py
# --host 0.0.0.0: listen on all interfaces (required in containers)
# --port 8000: internal port (Render maps its public port to this)
# --workers 1: One worker on free tier (Render free = 512MB RAM, 1 worker is safe)
#              Scale to 2-4 workers on paid tier
# NO --reload: that's for development only — reloading in prod is dangerous
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]