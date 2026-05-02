"""
app/services/ai_service.py — AI Insights & Chat Service

RESPONSIBILITIES:
  generate_insights  — gather financial data → build Gemini prompt →
                       parse response → store in DB → cache 24hr
  get_insights       — list stored insights for a user
  chat               — multi-turn conversation with full session history
  get_chat_sessions  — list past sessions
  get_chat_session   — single session with full message history

CACHE-FIRST PATTERN (Doc4 — Section 5.4):
  POST /ai/insights:
    1. Check Redis cache → return immediately if fresh
    2. If miss: gather financial data for the period
    3. Build structured Gemini prompt with context
    4. Call Gemini API (10-30 seconds)
    5. Parse JSON response
    6. Store in ai_insights table (persistent)
    7. SET in Redis with 24hr TTL
    8. Return result

GEMINI PROMPT DESIGN (Doc2 — Section 4.3):
  We send structured financial data as context.
  We ask for a specific JSON response shape.
  We include student context (Gujarat, India) for relevant advice.
  Response is parsed and stored as structured JSON + plain text.

CHAT DESIGN:
  Session history is stored in ai_chat_sessions.history (JSONB).
  Each message: {role: "user"|"model", content: "...", timestamp: "..."}
  Full history is sent to Gemini on every message (multi-turn context).
  Session title is auto-generated from the first user message.

GRACEFUL DEGRADATION:
  If Gemini API is unavailable: raise ExternalServiceException.
  Router catches it, returns 503 with "AI temporarily unavailable".
  The rest of the app continues to work normally.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import redis_client
from app.models.notification import AIChatSession, AIInsight
from app.schemas.notification_schema import AIChatRequest, AIInsightRequest
from app.utils.exceptions import ExternalServiceException, ResourceNotFoundException
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

_INSIGHT_CACHE_TTL = 60 * 60 * 24  # 24 hours
_INSIGHT_DB_TTL_HOURS = 24


# ── Insights ───────────────────────────────────────────────────────────────────


async def generate_insights(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: AIInsightRequest,
    trace_id: str = "no-trace",
) -> dict:
    """
    Cache-first insight generation.
    Returns cached result if fresh, otherwise generates via Gemini.
    """
    cache_key = _insight_cache_key(str(user_id), data)
    cached = await redis_client.get_json(cache_key)
    if cached:
        log_event(
            logger,
            "insight_served_from_cache",
            trace_id=trace_id,
            user_id=str(user_id),
            insight_type=data.insight_type,
        )
        return cached

    # ── Gather financial context ───────────────────────────────────────────────
    context = await _gather_financial_context(db, user_id, data)

    # ── Build Gemini prompt ────────────────────────────────────────────────────
    prompt = _build_prompt(data.insight_type, context)

    # ── Call Gemini ────────────────────────────────────────────────────────────
    raw_response = await _call_gemini(prompt, trace_id)

    # ── Parse response ─────────────────────────────────────────────────────────
    parsed = _parse_gemini_response(raw_response)

    # ── Persist to DB ──────────────────────────────────────────────────────────
    expires_at = datetime.now(UTC) + timedelta(hours=_INSIGHT_DB_TTL_HOURS)
    insight = AIInsight(
        user_id=user_id,
        insight_type=data.insight_type,
        content=parsed.get("plain_text", raw_response),
        input_summary=context,
        expires_at=expires_at,
        tokens_used=parsed.get("tokens_used"),
    )
    db.add(insight)
    await db.commit()
    await db.refresh(insight)

    result = {
        "id": str(insight.id),
        "insight_type": insight.insight_type,
        "content": insight.content,
        "input_summary": insight.input_summary,
        "expires_at": insight.expires_at.isoformat(),
        "tokens_used": insight.tokens_used,
        "generated_at": insight.created_at.isoformat(),
        "insights": parsed.get("insights", []),
        "savings_tips": parsed.get("savings_tips", []),
        "achievement": parsed.get("achievement", ""),
    }

    await redis_client.set_json(cache_key, result, ttl_seconds=_INSIGHT_CACHE_TTL)

    log_event(
        logger,
        "insight_generated",
        trace_id=trace_id,
        user_id=str(user_id),
        insight_type=data.insight_type,
        tokens=insight.tokens_used,
    )
    return result


async def get_insights(
    db: AsyncSession,
    user_id: uuid.UUID,
    insight_type: str | None = None,
    year: int | None = None,
    month: int | None = None,
) -> list[AIInsight]:
    """List previously generated insights for this user."""
    from sqlalchemy import and_

    conditions = [AIInsight.user_id == user_id]
    if insight_type:
        conditions.append(AIInsight.insight_type == insight_type)

    result = await db.execute(
        select(AIInsight).where(and_(*conditions)).order_by(AIInsight.created_at.desc()).limit(20)
    )
    return list(result.scalars().all())


# ── Chat ───────────────────────────────────────────────────────────────────────


async def chat(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: AIChatRequest,
    trace_id: str = "no-trace",
) -> dict:
    """
    Multi-turn AI chat with full session history.

    Flow:
      1. Load or create session
      2. Append user message to history
      3. Build Gemini request with full history (multi-turn context)
      4. Get reply
      5. Append model reply to history
      6. Save updated session
      7. Return reply + session_id
    """
    # ── Load or create session ─────────────────────────────────────────────────
    session = await _get_or_create_session(db, user_id, data.session_id)

    # ── Append user message ────────────────────────────────────────────────────
    user_msg = {
        "role": "user",
        "content": data.message,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    history: list[dict] = session.history or []
    history.append(user_msg)

    # ── Auto-generate session title from first message ─────────────────────────
    if not session.title and len(history) == 1:
        session.title = data.message[:60] + ("..." if len(data.message) > 60 else "")

    # ── Build Gemini chat prompt with history ──────────────────────────────────
    system_prompt = _build_chat_system_prompt()
    gemini_history = [{"role": msg["role"], "parts": [{"text": msg["content"]}]} for msg in history]

    # ── Call Gemini ────────────────────────────────────────────────────────────
    reply_text = await _call_gemini_chat(system_prompt, gemini_history, trace_id)

    # ── Append model reply to history ──────────────────────────────────────────
    model_msg = {
        "role": "model",
        "content": reply_text,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    history.append(model_msg)

    # ── Persist updated session ────────────────────────────────────────────────
    session.history = history
    session.total_tokens_used = (session.total_tokens_used or 0) + len(reply_text.split())
    await db.commit()

    log_event(
        logger,
        "chat_message",
        trace_id=trace_id,
        user_id=str(user_id),
        session_id=str(session.id),
        message_count=len(history),
    )

    return {
        "reply": reply_text,
        "session_id": str(session.id),
    }


async def get_chat_sessions(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[AIChatSession]:
    result = await db.execute(
        select(AIChatSession)
        .where(AIChatSession.user_id == user_id, AIChatSession.is_active.is_(True))
        .order_by(AIChatSession.updated_at.desc())
        .limit(20)
    )
    return list(result.scalars().all())


async def get_chat_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> AIChatSession:
    result = await db.execute(select(AIChatSession).where(AIChatSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise ResourceNotFoundException("ChatSession", str(session_id))
    from app.utils.exceptions import UnauthorizedAccessException

    if session.user_id != user_id:
        raise UnauthorizedAccessException()
    return session


async def delete_chat_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: str = "no-trace",
) -> None:
    session = await get_chat_session(db, session_id, user_id)
    session.is_active = False
    await db.commit()
    log_event(
        logger,
        "chat_session_archived",
        trace_id=trace_id,
        user_id=str(user_id),
        session_id=str(session_id),
    )


# ── Internal helpers ───────────────────────────────────────────────────────────


async def _gather_financial_context(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: AIInsightRequest,
) -> dict:
    """
    Collect financial data for the requested period.
    This is what we send to Gemini as context.
    Keeping it focused prevents token waste.
    """
    from app.services.budget_service import list_budgets_with_status
    from app.services.expense_service import get_monthly_summary
    from app.services.income_service import get_income_summary

    context: dict = {"insight_type": data.insight_type}

    if data.period_month and data.period_year:
        context["period"] = f"{data.period_month}/{data.period_year}"

        expense_summary = await get_monthly_summary(
            db, user_id, data.period_year, data.period_month
        )
        income_summary = await get_income_summary(db, user_id, data.period_year, data.period_month)
        context["expense_total"] = expense_summary.get("total_amount", "0")
        context["income_total"] = income_summary.get("total_amount", "0")
        context["spending_by_category"] = expense_summary.get("by_category", [])
        context["income_by_source"] = income_summary.get("by_source", [])

    budget_status = await list_budgets_with_status(db, user_id, active_only=True)
    context["budget_status"] = [
        {
            "category": b["category"]["name"],
            "limit": b["limit_amount"],
            "spent": b["spent_so_far"],
            "pct": b["spent_pct"],
            "status": b["status"],
        }
        for b in budget_status
    ]

    return context


def _build_prompt(insight_type: str, context: dict) -> str:
    """
    Build the Gemini prompt. Structured context + specific JSON output format.

    Student context: Indian student, Gujarat, tracks daily expenses.
    Tone: encouraging, not judgmental. Simple English.
    """
    period = context.get("period", "recent period")
    expense_total = context.get("expense_total", "0")
    income_total = context.get("income_total", "0")
    categories = context.get("spending_by_category", [])
    budgets = context.get("budget_status", [])

    category_text = "\n".join(
        f"  - {c['category_name']}: ₹{c['total_amount']} ({c['pct_of_total']}%)"
        for c in categories[:8]  # Top 8 to keep prompt size reasonable
    )
    budget_text = "\n".join(
        f"  - {b['category']}: {b['pct']}% used of ₹{b['limit']} ({b['status']})" for b in budgets
    )

    return f"""You are a friendly personal finance advisor for an Indian student.
The student lives in Gujarat, India. They use this app to track daily expenses.

FINANCIAL DATA FOR {period}:
- Total Income: ₹{income_total}
- Total Expenses: ₹{expense_total}
- Net Savings: ₹{float(income_total or 0) - float(expense_total or 0):.2f}

SPENDING BY CATEGORY:
{category_text or "  No data available"}

BUDGET STATUS:
{budget_text or "  No active budgets"}

TASK: Generate a {insight_type.replace("_", " ").lower()} analysis.

GUIDELINES:
- Use simple, conversational English (not formal)
- Mention specific categories and amounts from the data above
- Be encouraging and positive, not judgmental
- Give practical, actionable advice relevant to a student in India
- Consider Indian context: festivals, seasons, student lifestyle
- Keep insights concise (2-3 sentences each)

RESPOND WITH ONLY valid JSON in this exact format (no markdown, no extra text):
{{
  "insights": [
    "insight 1 text",
    "insight 2 text",
    "insight 3 text",
    "insight 4 text",
    "insight 5 text"
  ],
  "savings_tips": [
    "tip 1",
    "tip 2",
    "tip 3"
  ],
  "achievement": "One positive thing the student did this period",
  "plain_text": "A 2-3 sentence plain text summary of the overall financial situation"
}}"""


def _parse_gemini_response(raw: str) -> dict:
    """
    Parse Gemini's JSON response. Handles cases where Gemini adds
    markdown code fences or extra whitespace around the JSON.
    """
    # Strip markdown code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first line (```json or ```) and last line (```)
        cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Gemini didn't follow the JSON format — return raw as plain text
        logger.warning("Gemini response was not valid JSON — returning as plain text")
        return {
            "plain_text": raw,
            "insights": [raw],
            "savings_tips": [],
            "achievement": "",
        }


def _build_chat_system_prompt() -> str:
    """System prompt that sets Gemini's persona for the chat feature."""
    return """You are a friendly personal finance assistant for an Indian student.
You help them understand their spending, answer questions about their finances,
and give practical money-saving advice relevant to student life in India.

Keep responses concise (2-4 sentences unless a detailed breakdown is needed).
Use simple English. Be encouraging. Reference Indian context when relevant
(UPI payments, festivals, hostel expenses, etc.).

You have access to the conversation history below. Answer the latest user question."""


async def _call_gemini(prompt: str, trace_id: str) -> str:
    """
    Call Gemini API for insight generation.
    Raises ExternalServiceException if the API call fails.
    """
    try:
        import google.generativeai as genai

        from app.config import settings

        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        log_event(logger, "gemini_request_start", trace_id=trace_id, model="gemini-1.5-flash")

        response = model.generate_content(prompt)
        text = response.text

        log_event(logger, "gemini_request_complete", trace_id=trace_id, response_length=len(text))
        return text

    except Exception as e:
        logger.error(
            "Gemini API call failed",
            extra={"trace_id": trace_id, "error": str(e)},
        )
        raise ExternalServiceException("Gemini AI") from e


async def _call_gemini_chat(
    system_prompt: str,
    history: list[dict],
    trace_id: str,
) -> str:
    """
    Call Gemini for multi-turn chat.
    Sends full conversation history for context.
    """
    try:
        import google.generativeai as genai

        from app.config import settings

        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(
            "gemini-1.5-flash",
            system_instruction=system_prompt,
        )

        # Convert history to Gemini's format
        # Gemini expects alternating user/model turns
        chat_session = model.start_chat(history=history[:-1])  # type: ignore[arg-type]
        last_message = history[-1]["parts"][0]["text"]

        response = chat_session.send_message(last_message)

        log_event(logger, "chat_gemini_response", trace_id=trace_id, turns=len(history))
        return response.text

    except Exception as e:
        logger.error(
            "Gemini chat API call failed",
            extra={"trace_id": trace_id, "error": str(e)},
        )
        raise ExternalServiceException("Gemini AI") from e


async def _get_or_create_session(
    db: AsyncSession,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
) -> AIChatSession:
    """Load existing session or create a new one."""
    if session_id:
        result = await db.execute(
            select(AIChatSession).where(
                AIChatSession.id == session_id,
                AIChatSession.user_id == user_id,
                AIChatSession.is_active.is_(True),
            )
        )
        session = result.scalar_one_or_none()
        if session:
            return session

    # Create new session
    session = AIChatSession(
        user_id=user_id,
        history=[],
        total_tokens_used=0,
    )
    db.add(session)
    await db.flush()  # Get session.id
    return session


def _insight_cache_key(user_id: str, data: AIInsightRequest) -> str:
    """Build the Redis cache key for an insight request."""
    month = data.period_month or 0
    year = data.period_year or 0
    return f"financeflow:ai:{user_id}:insight:{data.insight_type}:{year}:{month:02d}"