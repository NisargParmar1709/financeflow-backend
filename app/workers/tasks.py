"""
app/workers/tasks.py — Background Job Definitions

WHY BACKGROUND JOBS (Video 14 — Task Queues):
  Some operations are too slow or non-critical to run inside the
  request-response cycle. If we ran these synchronously on every request:
    - The user would wait 3-5 extra seconds for every page load
    - A failure in an email send would fail the entire HTTP request
    - Repeated alert checks would hammer the database

  Solution: defer them to background workers that run independently.

THREE JOBS:
  1. check_all_user_alerts   — runs on every dashboard load (lightweight)
  2. send_weekly_summary     — runs every Monday (email via Resend)
  3. check_fd_maturity_all   — runs daily (scan all users' FDs)

EXECUTION MODEL:
  FinanceFlow uses a simple in-process background task model via
  FastAPI's BackgroundTasks. This is enough for our scale.
  If volume grows: swap BackgroundTasks for Celery + Redis queue.

  FastAPI BackgroundTasks:
    - Run AFTER the HTTP response is sent
    - Share the same process as the API server
    - No separate worker process needed
    - No queue, no retry — fire and forget

  Usage in a router:
    from fastapi import BackgroundTasks
    from app.workers.tasks import run_alert_checks

    @router.get("/dashboard")
    async def dashboard(background_tasks: BackgroundTasks, ...):
        background_tasks.add_task(run_alert_checks, user_id=current_user.id)
        return dashboard_data
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


async def run_alert_checks(
    db: AsyncSession,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> None:
    """
    Background task: run all alert checks for a user.

    Called after dashboard loads — user already has their data,
    this runs silently in the background and updates notifications.

    Why background: alert checks do 3 DB queries. Doing them inline
    would add ~50-100ms to every dashboard load. In background:
    the user sees the dashboard immediately, alerts appear on next
    bell icon refresh (10-minute cache TTL anyway).
    """
    try:
        from app.services.notification_service import check_all_alerts

        result = await check_all_alerts(db, user_id, trace_id=trace_id)
        log_event(
            logger,
            "background_alert_check_complete",
            trace_id=trace_id,
            user_id=str(user_id),
            notifications_created=result["notifications_created"],
        )
    except Exception as e:
        # Background tasks MUST NOT crash — log and move on
        logger.error(
            "Background alert check failed",
            extra={
                "event": "background_task_error",
                "task": "run_alert_checks",
                "user_id": str(user_id),
                "trace_id": trace_id,
                "error": str(e),
            },
        )


async def send_weekly_summary_email(
    user_id: uuid.UUID,
    user_email: str,
    trace_id: str = "no-trace",
) -> None:
    """
    Background task: send weekly spending summary via Resend.

    Triggered: every Monday morning (run via cron-job.org hitting
    POST /api/v1/internal/trigger-weekly-emails).

    WHY EMAIL (Doc1 — Section 1.1):
      Students may not open the app every day. A weekly email keeps
      them aware of their spending without requiring app opens.
      Email is non-critical: if Resend is down, we log and skip.
      User won't notice until next week.
    """
    try:
        import resend

        from app.config import settings

        resend.api_key = settings.RESEND_API_KEY

        # Build a minimal summary email
        # In production: render a proper HTML template
        params: resend.Emails.SendParams = {
            "from": settings.RESEND_FROM_EMAIL,
            "to": [user_email],
            "subject": "Your FinanceFlow Weekly Summary",
            "html": _build_weekly_email_html(user_id),
        }
        resend.Emails.send(params)

        log_event(
            logger,
            "weekly_summary_email_sent",
            trace_id=trace_id,
            user_id=str(user_id),
            email=user_email,
        )
    except Exception as e:
        logger.warning(
            "Weekly summary email failed",
            extra={
                "event": "background_task_error",
                "task": "send_weekly_summary_email",
                "user_id": str(user_id),
                "trace_id": trace_id,
                "error": str(e),
            },
        )


async def check_fd_maturity_for_all_users(
    db: AsyncSession,
    trace_id: str = "no-trace",
) -> None:
    """
    Background task: scan ALL users' FDs for upcoming maturity.

    Runs: daily via cron-job.org hitting POST /api/v1/internal/trigger-fd-checks.
    Window: FDs maturing in the next 30 days.

    WHY DAILY SCAN:
      FD maturity is date-based. On the day an FD enters the 30-day
      window, the user should get a notification. Without a daily scan,
      they'd only see the alert when they next load the dashboard.
      For FDs, timely notification matters — the user needs to decide
      whether to renew, withdraw, or let it auto-renew.

    SCALE NOTE:
      For small user counts (<10k) this single daily scan is fine.
      At scale: partition by user_id modulo, run sharded workers.
    """
    from sqlalchemy import select

    from app.models.user import User
    from app.services.notification_service import _check_fd_maturity

    try:
        result = await db.execute(
            select(User.id).where(User.is_deleted.is_(False), User.is_active.is_(True))
        )
        user_ids = [row[0] for row in result.all()]

        notifications_created = 0
        for uid in user_ids:
            count = await _check_fd_maturity(db, uid, trace_id=trace_id)
            notifications_created += count

        log_event(
            logger,
            "fd_maturity_scan_complete",
            trace_id=trace_id,
            users_scanned=len(user_ids),
            notifications_created=notifications_created,
        )
    except Exception as e:
        logger.error(
            "FD maturity daily scan failed",
            extra={
                "event": "background_task_error",
                "task": "check_fd_maturity_for_all_users",
                "trace_id": trace_id,
                "error": str(e),
            },
        )


# ── Internal helpers ───────────────────────────────────────────────────────────


def _build_weekly_email_html(user_id: uuid.UUID) -> str:
    """
    Minimal HTML email template for weekly summary.
    In production: use a proper templating engine (Jinja2) with
    real spending data fetched from the DB.
    """
    return """
    <html>
    <body style="font-family: Inter, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #7F56D9;">Your FinanceFlow Weekly Summary</h2>
      <p>Here's a quick look at your finances this week.</p>
      <p style="color: #667085; font-size: 14px;">
        Open the app to see your full spending breakdown, budget status,
        and AI-powered insights.
      </p>
      <a href="https://financeflow.vercel.app/dashboard"
         style="background: #7F56D9; color: white; padding: 12px 24px;
                border-radius: 8px; text-decoration: none; display: inline-block; margin-top: 16px;">
        View Dashboard →
      </a>
      <p style="color: #98A2B3; font-size: 12px; margin-top: 32px;">
        You're receiving this because you have weekly summaries enabled.
        Update preferences in your profile settings.
      </p>
    </body>
    </html>
    """