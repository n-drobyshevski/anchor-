"""GET /api/state and the field-write endpoints (W2 roadmap section 4).

StateDTO is deliberately not `_format_state`'s Russian text reparsed:
every field below comes straight from the same data-gathering calls
`app/tg/router.py`'s `/state` handler already makes (`get_state`,
`today_usd`/`today_by_category`, `memory_core.count_active`,
`load_state_summary`, `safety_events.counts`), so the web panel can
never show a number `/state` itself would disagree with. Never
exposed, per the HTTP contract: `chat_id`, `awaiting`/`awaiting_ref`,
raw `state_change` rows, or memory text -- StateDTO's shape below is
the whole allow-list, not a redaction of a bigger dict.

Every mutating handler here:

1. checks the session cookie (401 `unauthenticated`);
2. reads and validates the body (400 `bad_request`, or a field-specific
   422 `invalid`/`detail` the HTTP contract spells out per endpoint);
3. clears any pending check-in note the same way any Telegram command
   does (`checkin_core.clear_awaiting` -- app/tg/router.py's outer
   middleware runs this for every `/`-prefixed message, and a direct
   core call here is the only way to get the same effect, since these
   endpoints never go through that middleware or the dispatcher at
   all);
4. checks the shared 60/min panel-write bucket (429 `rate_limited`);
5. writes through `app/core/commands.py` with `source="web"` -- never
   a bare `update_state` call here, so Telegram and the web can never
   disagree about what a write does;
6. retires any Telegram proposal buttons the write makes stale, using
   the *real* bot (`app["bot"]`), best-effort (a Telegram-side failure
   -- a deleted message, a network error -- is logged and swallowed,
   never turned into a 500 for a write that already committed), and
   publishes `invalidate("state")` (and `invalidate("proposals")`
   whenever a proposal was expired, regardless of whether retiring its
   buttons succeeded) -- never a Telegram chat message: a web-issued
   write is silent in Telegram by design (roadmap section 7), and
   nothing here calls `send_message`/`send_command_reply`.

`POST /api/state/pause` is the one exception to steps 3, 5 and 6: it
enqueues a synthetic `/out` or `/in` web update through the existing
ingress path instead of writing `user_state` itself, so the pause
machinery -- the replay guard, `cancel_outbound`, the canned reply --
stays exactly the single implementation `app/core/turn.py` already
owns. `clear_awaiting` still happens, just later: the synthetic update
is plain command text, so it runs through
`clear_awaiting_on_command`, the router's own outer middleware, the
moment the worker claims it. Nothing here needs to publish
`invalidate("state")` either -- `persona_active` is one of
`app/web/tail.py`'s mapped `state_change` fields, so the tail publishes
it once `run_hard_pause`/`run_resume` actually writes it.
"""

from __future__ import annotations

import datetime
import decimal
import logging
import uuid

from aiohttp import web

from app.core import checkin as checkin_core
from app.core import commands as commands_core
from app.core import memory as memory_core
from app.core import proposal as proposal_core
from app.core import safety_events
from app.core.outbound import load_state_summary
from app.core.quiet import clamp as clamp_quiet
from app.core.spend import today_by_category, today_usd
from app.core.state import get_state
from app.tg import proposals as proposals_ui
from app.web import ingress
from app.web.hub import WebHub
from app.web.http import _cookie_settings, _json, _rate_limited, _read_body, _session_token_valid
from app.web.ratelimit import WebRateLimiter, pending_web_count, MAX_PENDING_WEB_ROWS

logger = logging.getLogger(__name__)

# "Today" rather than app/core/safety_events.py's default 7-day window
# -- /state's Telegram line asks "is this check healthy over the last
# week", but StateDTO.counts asks a narrower question, "how much did
# each check do today", so `days=1` here on purpose.
_TODAY = 1


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _usd(value: decimal.Decimal) -> float:
    return float(value)


async def _build_state_dto(session, settings, clock) -> dict:
    user_state = await get_state(session)
    spend_today = await today_usd(session, clock, user_state.timezone)
    by_category = await today_by_category(session, clock, user_state.timezone)
    memories = await memory_core.count_active(session)
    summary = await load_state_summary(session, clock, settings, user_state)

    welfare_ok, welfare_fail = await safety_events.counts(
        session, clock, user_state.timezone, kind=safety_events.WELFARE, days=_TODAY
    )
    distill_ok, distill_fail = await safety_events.counts(
        session, clock, user_state.timezone, kind=safety_events.DISTILL, days=_TODAY
    )
    search_ok, search_fail = await safety_events.counts(
        session, clock, user_state.timezone, kind=safety_events.SEARCH, days=_TODAY
    )

    return {
        "focus": {"on": user_state.focus_on, "since": _iso(user_state.focus_since)},
        "due": {"action": user_state.due_action, "set_at": _iso(user_state.due_set_at)},
        "streak": user_state.streak,
        "last_checkin_at": _iso(user_state.last_checkin_at),
        "quiet_until": _iso(user_state.quiet_until),
        "timezone": user_state.timezone,
        "paused": not user_state.persona_active,
        "ignored_in_row": user_state.ignored_in_row,
        "next_planned_for": _iso(summary.next_planned_for),
        "spend": {
            "today_usd": _usd(spend_today),
            "cap_usd": float(settings.DAILY_USD_CAP),
            "by_category": {name: _usd(total) for name, total in by_category.items()},
        },
        "counts": {
            "memories": memories,
            "welfare_today": welfare_ok + welfare_fail,
            "distill_today": distill_ok + distill_fail,
            "search_today": search_ok + search_fail,
        },
        "limits": {"due_max_len": commands_core.DUE_ACTION_MAX_LEN},
    }


async def _expire_proposal_in_txn(session, clock, field: str) -> proposal_core.Proposal | None:
    """The DB half of expiring a proposal made stale by a web write:
    expire the pending proposal for `field` (if any) in the caller's
    own session/transaction. Split from `_retire_expired_buttons` below
    so the caller's `async with sessionmaker()` block can close -- and
    release its DB connection -- before that function's Telegram HTTP
    round-trip runs (review finding: a web request must not hold a
    pooled connection open for the duration of a Telegram API call).
    """
    return await commands_core.expire_proposal_for(session, clock, field)


async def _retire_expired_buttons(sessionmaker, bot, chat_id: str | int, expired: proposal_core.Proposal, hub: WebHub) -> None:
    """Best-effort: retire `expired`'s Telegram buttons, then always
    tell any open Proposals panel to refetch.

    Mirrors app/tg/proposals.py's `send_proposal`, which guards this
    same `retire_buttons` call the same way for the identical reason:
    `edit_keyboard` re-raises every Telegram error except "message is
    not modified" (a deleted message, TelegramNetworkError, ...), and
    by the time this runs the due/focus write and the proposal's
    EXPIRED status are already committed -- a Telegram-side failure
    here must not turn an already-successful web write into a 500, and
    must not skip the invalidate that tells other open tabs about it.
    """
    try:
        await proposals_ui.retire_buttons(sessionmaker, bot, chat_id=chat_id, proposal_id=expired.id)
    except Exception as exc:  # noqa: BLE001 - best-effort, matching send_proposal's own guard
        logger.warning(
            "retiring expired proposal buttons failed",
            extra={"proposal_id": expired.id, "event": type(exc).__name__},
        )
    hub.publish_invalidate("proposals")


# --- GET /api/state ---------------------------------------------------


async def get_state_view(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    async with sessionmaker() as session:
        dto = await _build_state_dto(session, settings, clock)
    return _json(200, dto)


# --- POST /api/state/due -----------------------------------------------


async def post_due(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    text = body.get("text")
    if not isinstance(text, str):
        return _json(400, {"error": "bad_request"})
    stripped = text.strip()
    if not stripped:
        return _json(422, {"error": "invalid", "detail": "empty"})
    if len(stripped) > commands_core.DUE_ACTION_MAX_LEN:
        return _json(422, {"error": "invalid", "detail": "too_long"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        await commands_core.set_due(session, clock, stripped, "web")
        expired = await _expire_proposal_in_txn(session, clock, proposal_core.DUE_ACTION)
        dto = await _build_state_dto(session, settings, clock)

    if expired is not None:
        await _retire_expired_buttons(sessionmaker, bot, settings.ALLOWED_CHAT_ID, expired, hub)
    hub.publish_invalidate("state")
    return _json(200, {"state": dto})


# --- POST /api/state/focus ----------------------------------------------


async def post_focus(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    on = body.get("on")
    if not isinstance(on, bool):
        return _json(400, {"error": "bad_request"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        await commands_core.set_focus(session, clock, on, "web")
        expired = await _expire_proposal_in_txn(session, clock, proposal_core.FOCUS_ON)
        dto = await _build_state_dto(session, settings, clock)

    if expired is not None:
        await _retire_expired_buttons(sessionmaker, bot, settings.ALLOWED_CHAT_ID, expired, hub)
    hub.publish_invalidate("state")
    return _json(200, {"state": dto})


# --- POST /api/state/quiet ----------------------------------------------


def _parse_quiet_until(raw: object, clock, max_days: int) -> tuple[datetime.datetime | None, str | None]:
    """`(until, None)` on success, `(None, detail)` on a 422.

    `raw` is the request body's "until": null (off) or an ISO-8601
    string that must carry a UTC offset -- unlike Telegram's `/quiet
    <N>m|h|d`, the web sends an absolute end time, so there is no
    duration to parse, only a timestamp to validate and clamp exactly
    the way `app.core.quiet.clamp` already clamps a duration for
    Telegram (a `until` further away than `QUIET_MAX_DAYS` is capped to
    it, not rejected -- same reasoning as `/quiet 30d`).
    """
    if raw is None:
        return None, None
    if not isinstance(raw, str) or len(raw) > 64:
        return None, "bad_time"
    try:
        parsed = datetime.datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return None, "bad_time"
        # A timestamp near datetime's range limits (e.g. year 9999 with
        # a large negative offset, or year 1 with a large positive one)
        # parses fine above but overflows on conversion to UTC --
        # `OverflowError` alongside `ValueError` here, so both collapse
        # to the same 422 rather than an unhandled 500.
        until = parsed.astimezone(datetime.timezone.utc)
    except (ValueError, OverflowError):
        return None, "bad_time"
    now = clock.now_utc()
    if until <= now:
        return None, "past"
    capped = clamp_quiet(until - now, max_days)
    return now + capped, None


async def post_quiet(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    if "until" not in body:
        return _json(400, {"error": "bad_request"})
    until, detail = _parse_quiet_until(body.get("until"), clock, settings.QUIET_MAX_DAYS)
    if detail is not None:
        return _json(422, {"error": "invalid", "detail": detail})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        await commands_core.set_quiet(session, clock, until, "web")
        dto = await _build_state_dto(session, settings, clock)

    hub.publish_invalidate("state")
    return _json(200, {"state": dto})


# --- POST /api/state/timezone -------------------------------------------


async def post_timezone(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    tz = body.get("tz")
    if not isinstance(tz, str) or not tz:
        return _json(400, {"error": "bad_request"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        try:
            await commands_core.set_timezone(session, tz, "web")
        except commands_core.InvalidTimezone:
            return _json(422, {"error": "invalid", "detail": "unknown_timezone"})
        dto = await _build_state_dto(session, settings, clock)

    hub.publish_invalidate("state")
    return _json(200, {"state": dto})


# --- POST /api/state/pause ------------------------------------------------


async def post_pause(request: web.Request) -> web.Response:
    """Enqueue the synthetic `/out` or `/in` web command (module
    docstring). No direct `user_state` write here at all -- see the
    module docstring for why that is the point, not an omission.

    This is the one panel-write endpoint that queues a real ingress row
    (every other one writes `user_state` directly), so it also has to
    carry the guards `POST /api/send` puts around that same queue
    (app/web/routes.py's `send`) -- the shared 60/min panel-write
    bucket alone is not a substitute for either: `check_send`'s 12/min,
    300/day caps, and the `MAX_PENDING_WEB_ROWS` backlog cap. Without
    them, a stuck frontend or a scripted loop hammering this one
    endpoint could queue far more turns than the send box itself ever
    allows, each one running `clear_awaiting`, the pause/resume write,
    `cancel_outbound` and a canned reply sent to the real Telegram
    chat -- spamming Telegram and exhausting the bot's own send quota.
    """
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    on = body.get("on")
    if not isinstance(on, bool):
        return _json(400, {"error": "bad_request"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)
    retry = limiter.check_send()
    if retry is not None:
        return _rate_limited(retry)

    text = "/out" if on else "/in"
    async with sessionmaker() as session:
        # Already in the requested state: nothing to enqueue. A repeat
        # tap while the previous toggle's own turn has not run yet
        # still enqueues below (persona_active has not flipped yet, so
        # this comparison is false), which keeps a genuinely-in-flight
        # toggle from being silently dropped -- this only short-circuits
        # a settled state a repeat call would otherwise queue for no
        # reason.
        user_state = await get_state(session)
        if on == (not user_state.persona_active):
            return _json(202, {})
        if await pending_web_count(session) >= MAX_PENDING_WEB_ROWS:
            return _rate_limited(5.0)
        await ingress.send_text(
            session, settings=settings, text=text, client_key=str(uuid.uuid4())
        )
    return _json(202, {})


# --- registration --------------------------------------------------------


def register(app: web.Application) -> None:
    app.router.add_get("/api/state", get_state_view)
    app.router.add_post("/api/state/due", post_due)
    app.router.add_post("/api/state/focus", post_focus)
    app.router.add_post("/api/state/quiet", post_quiet)
    app.router.add_post("/api/state/timezone", post_timezone)
    app.router.add_post("/api/state/pause", post_pause)
