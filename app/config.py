"""
app/config.py — Configuration Management

WHY THIS FILE EXISTS:
  Configuration is the DNA of a backend application. This file is the single
  source of truth for every config value the app needs. It uses Pydantic's
  BaseSettings to:
    1. Read values from environment variables (and .env file in dev)
    2. Validate types at startup — a wrong type crashes NOW, not mid-request
    3. Expose a typed `settings` object used everywhere in the app

RULE: Never import os.environ directly anywhere in the app.
      Always import `settings` from this file. This centralizes config and
      makes it trivially easy to mock in tests.

FAIL-FAST PRINCIPLE (from Video 17):
  If DATABASE_URL is missing, the app crashes at import time with a clear
  ValidationError telling you exactly which field is missing.
  This is intentional — a missing env var discovered at 3am by a user is
  infinitely worse than one discovered at deploy time by you.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, AnyUrl
from typing import Literal
from functools import lru_cache


class Settings(BaseSettings):
    """
    All configuration fields are typed. Pydantic validates them on init.
    Missing required fields → ValidationError on startup → deploy fails safely.
    """

    # ── App ──────────────────────────────────────────────────────────────────
    APP_ENV: Literal["development", "staging", "production"] = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    # Why not just `bool`: env vars are strings. Pydantic handles "true"/"false" → bool.
    DEBUG: bool = True

    # ── Database ─────────────────────────────────────────────────────────────
    # No default — this MUST be present. App will not start without it.
    DATABASE_URL: str

    # Connection pool sizing — these are tuning parameters, not secrets.
    # 5 is safe for Neon's free tier (which has a connection limit).
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30  # seconds before "could not get connection" error

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str

    # ── Clerk Authentication ──────────────────────────────────────────────────
    CLERK_SECRET_KEY: str
    CLERK_PUBLISHABLE_KEY: str
    CLERK_WEBHOOK_SECRET: str

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Stored as a comma-separated string in .env, parsed into a list here.
    # Why: Docker/Render env vars don't support list syntax natively.
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list) -> list[str]:
        """
        Accepts either a Python list (from tests) or a comma-separated string
        (from .env / environment variables).

        Why: In tests we pass a list directly. In production, Docker passes a
        string like "https://a.com,https://b.com". One validator handles both.
        """
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    # ── Cloudinary ────────────────────────────────────────────────────────────
    CLOUDINARY_CLOUD_NAME: str
    CLOUDINARY_API_KEY: str
    CLOUDINARY_API_SECRET: str

    # ── Gemini AI ─────────────────────────────────────────────────────────────
    GEMINI_API_KEY: str

    # ── Resend Email ──────────────────────────────────────────────────────────
    RESEND_API_KEY: str
    RESEND_FROM_EMAIL: str = "noreply@financeflow.app"

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    RATE_LIMIT_PER_MINUTE: int = 100

    # ── Pydantic Settings Config ──────────────────────────────────────────────
    model_config = SettingsConfigDict(
        # In development: reads from .env file automatically
        # In production (Render/Docker): reads from actual environment variables
        env_file=".env",
        env_file_encoding="utf-8",
        # Case-insensitive — DATABASE_URL and database_url both work
        case_sensitive=False,
        # Crash on extra fields — prevents silent config drift
        extra="forbid",
    )

    # ── Computed Properties ───────────────────────────────────────────────────

    @property
    def is_production(self) -> bool:
        """
        Use this instead of checking APP_ENV == "production" everywhere.
        Example: if settings.is_production: disable_debug_routes()
        """
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


@lru_cache()
def get_settings() -> Settings:
    """
    Returns a cached singleton Settings instance.

    WHY lru_cache: Settings reads from disk and validates on every instantiation.
    We don't want to re-read .env on every function call. lru_cache means
    Settings() runs ONCE — subsequent calls return the same object.

    In tests: call get_settings.cache_clear() to reset between test cases
    that need different env vars.
    """
    return Settings()


# ── Module-level singleton ────────────────────────────────────────────────────
# Import this object everywhere: `from app.config import settings`
# Do NOT call get_settings() in every module — just use this.
settings: Settings = get_settings()