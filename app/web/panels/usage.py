"""Лимиты (#/usage): what has been used against every cap, read-only.

- GET /api/usage: today's spend against DAILY_USD_CAP and IDLE_USD_CAP,
  the last 14 local days of spend (per category, and per model over the
  whole window), the daily quotas (unsolicited messages, idle jobs,
  /study and /read, planner writes, web sends) and Claude's hourly and
  daily write caps. Only numbers, dates, ledger categories and model
  names: never message text. A quota whose feature is switched off for
  this deploy is left out, the same way /menu hides it.
- GET /api/usage/openrouter: the OpenRouter key's own limit and usage
  (GET /api/v1/key) and, where the key may read it, the account's
  credit balance (GET /api/v1/credits). Asked server-side with the
  existing OPENROUTER_API_KEY, which never leaves the server and is
  never logged; the key's label is dropped too. Cached for five
  minutes, so reloading the screen does not hammer OpenRouter. Any
  failure is `{"available": false}` with a 200, so the screen says
  "недоступно" instead of showing an error.

Nothing here writes, so there is no panel-write bucket and no
invalidate.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import web

from app.core import clock as clock_module
from app.core import claude_write_limits as write_limits
from app.core import spend
from app.core.idle.facts import idle_jobs_today
from app.core.outbound import sent_today
from app.core.state import get_state
from app.llm.openrouter import OPENROUTER_BASE_URL
from app.planner import actions as planner_actions
from app.research import jobs as research_jobs
from app.web.http import _cookie_settings, _json, _session_token_valid
from app.web.panels.state import claude_usage
from app.web.ratelimit import SEND_PER_DAY_LIMIT, WebRateLimiter

logger = logging.getLogger(__name__)

HISTORY_DAYS = 14

OPENROUTER_KEY_URL = f"{OPENROUTER_BASE_URL}/key"
OPENROUTER_CREDITS_URL = f"{OPENROUTER_BASE_URL}/credits"
OPENROUTER_TIMEOUT_S = 5
OPENROUTER_CACHE_S = 5 * 60
OPENROUTER_FAILURE_CACHE_S = 60

# The /api/v1/key fields passed through, all numbers or flags.
_KEY_FIELDS = ("limit", "limit_remaining", "limit_reset", "usage", "usage_daily", "usage_weekly", "usage_monthly", "is_free_tier")


def _usd(value) -> float:
    return float(value)


def _quota(key: str, label: str, used: int, limit: int, window: str) -> dict:
    return {"key": key, "label": label, "used": used, "limit": limit, "window": window}


async def _quotas(session, settings, clock, timezone: str, limiter: WebRateLimiter) -> list[dict]:
    today = clock_module.local_date(clock, timezone)
    quotas = []
    if settings.OUTBOUND_ENABLED:
        quotas.append(
            _quota("unsolicited", "Сообщений первой", await sent_today(session, today), settings.MAX_UNSOLICITED_PER_DAY, "day")
        )
    if settings.IDLE_ENABLED:
        quotas.append(
            _quota(
                "idle_jobs",
                "Фоновых задач",
                await idle_jobs_today(session, clock, timezone),
                settings.IDLE_MAX_JOBS_PER_DAY,
                "day",
            )
        )
    if settings.RESEARCH_ENABLED:
        used = await research_jobs.used_today(session, today)
        quotas.append(_quota("study", "Исследований /study", used[research_jobs.STUDY], settings.RESEARCH_JOBS_PER_DAY, "day"))
        quotas.append(_quota("read", "Чтений /read", used[research_jobs.READ], settings.RESEARCH_READS_PER_DAY, "day"))
    if settings.PLANNER_ENABLED:
        quotas.append(
            _quota(
                "planner_writes",
                "Записей в планер",
                await planner_actions.count_today(session, clock, timezone),
                settings.PLANNER_MAX_WRITES_PER_DAY,
                "day",
            )
        )
    quotas.append(_quota("web_sends", "Сообщений из веба", limiter.sends_today(), SEND_PER_DAY_LIMIT, "24h"))
    return quotas


async def _claude_dto(session, settings, clock) -> list[dict] | None:
    if not settings.CLAUDE_ACCESS_ENABLED:
        return None
    current = (await write_limits.effective(session)).as_dict()
    used = await claude_usage(session, clock)
    return [
        {
            "key": key,
            "label": write_limits.SPECS[key].label,
            "used": used[key],
            "limit": current[key],
            "unit": "bytes" if key == "bytes_per_day" else "count",
            "window": "hour" if key.endswith("_per_hour") else "day",
        }
        for key in write_limits.SPECS
        if key in used
    ]


async def _usage_dto(session, settings, clock, limiter: WebRateLimiter) -> dict:
    state = await get_state(session)
    timezone = state.timezone
    history = await spend.history(session, clock, timezone, HISTORY_DAYS)
    return {
        "timezone": timezone,
        "spend": {
            "today_usd": _usd(await spend.today_usd(session, clock, timezone)),
            "cap_usd": float(settings.DAILY_USD_CAP),
            "idle_today_usd": _usd(await spend.today_idle_usd(session, clock, timezone)),
            "idle_cap_usd": float(settings.IDLE_USD_CAP) if settings.IDLE_ENABLED else None,
            "history": [
                {
                    "date": date.isoformat(),
                    "usd": _usd(total),
                    "by_category": {name: _usd(amount) for name, amount in categories.items()},
                }
                for date, total, categories in history.days
            ],
            "by_model": {model: _usd(total) for model, total in history.by_model.items()},
        },
        "quotas": await _quotas(session, settings, clock, timezone, limiter),
        "claude": await _claude_dto(session, settings, clock),
    }


async def get_usage(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    async with sessionmaker() as session:
        dto = await _usage_dto(session, settings, clock, limiter)
    return _json(200, dto)


# --- OpenRouter ----------------------------------------------------------


async def _get_json(http: aiohttp.ClientSession, url: str, api_key: str) -> dict | None:
    """`data` from one OpenRouter GET, or None on any non-200."""
    async with http.get(url, headers={"Authorization": f"Bearer {api_key}"}) as response:
        if response.status != 200:
            return None
        body = await response.json()
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


async def fetch_openrouter(api_key: str) -> dict | None:
    """The key's limits and, when the key may read it, the account
    balance. None when the key endpoint itself is unreachable or
    refuses. Only the fields in _KEY_FIELDS survive (never `label`)."""
    timeout = aiohttp.ClientTimeout(total=OPENROUTER_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        key = await _get_json(http, OPENROUTER_KEY_URL, api_key)
        if key is None:
            return None
        # /credits may be refused for an ordinary (non-management) key;
        # the balance is then simply absent.
        credits = await _get_json(http, OPENROUTER_CREDITS_URL, api_key)
    result = {}
    for field in _KEY_FIELDS:
        value = key.get(field)
        if field == "is_free_tier":
            result[field] = value if isinstance(value, bool) else None
        elif field == "limit_reset":
            result[field] = value if isinstance(value, str) else None
        else:
            result[field] = _number(value)
    balance = None
    if credits is not None:
        total, used = _number(credits.get("total_credits")), _number(credits.get("total_usage"))
        if total is not None and used is not None:
            balance = total - used
    result["balance"] = balance
    return result


_CACHE_KEY = "usage_openrouter_cache"


async def get_openrouter(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, _sessionmaker, clock, _bot = _cookie_settings(request)
    now = clock.now_utc()
    cache: dict = request.app[_CACHE_KEY]
    if "dto" in cache and (now - cache["at"]).total_seconds() < cache["ttl"]:
        return _json(200, cache["dto"])

    data = None
    if settings.OPENROUTER_API_KEY:
        try:
            data = await fetch_openrouter(settings.OPENROUTER_API_KEY)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            # The exception's type only: its text can carry a URL or
            # headers, and the key must never reach a log line.
            logger.warning("openrouter usage unavailable", extra={"event": "web_openrouter_usage_failed", "error": type(exc).__name__})
    if data is None:
        dto = {"available": False, "fetched_at": now.isoformat()}
    else:
        dto = {"available": True, "fetched_at": now.isoformat(), **data}
    # A failure is cached briefly too, so a flapping OpenRouter is asked
    # once a minute at most, not on every render.
    cache.update(at=now, dto=dto, ttl=OPENROUTER_CACHE_S if data is not None else OPENROUTER_FAILURE_CACHE_S)
    return _json(200, dto)


def register(app: web.Application) -> None:
    app[_CACHE_KEY] = {}
    app.router.add_get("/api/usage", get_usage)
    app.router.add_get("/api/usage/openrouter", get_openrouter)
