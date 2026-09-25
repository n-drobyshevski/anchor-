"""GET/POST /api/checkin, GET /api/checkins and GET /api/journal (W4
roadmap section 4, "Чек-ин" screen).

Same shape as app/web/panels/state.py and app/web/panels/memory.py:
`_session_token_valid` first, then `_read_body` and bounded, typed
validation (400 `bad_request` for shape, 422 `invalid`/`detail` for
content), then `check_panel_write()` **and** `check_send()` -- then core
calls only (`app/core/checkin.py`, `app/core/orders.py`,
`app/core/journal.py`), then `hub.publish_invalidate("checkin")`.

**No new LLM path.** POST /api/checkin fills every step -- the note
included -- through `checkin_core.submit` and then enqueues one
synthetic web update, like `POST /api/state/pause` does for `/out`/`/in`:
the `c:n:web` completion callback carrying the check-in's own
server-minted message id (app/tg/checkin.py's `finish_and_react`, which
finishes exactly that check-in through core `finish_submitted` and runs
the same CHECKIN_FLAG turn a Telegram completion does). The global note
step (`awaiting`) is never opened from here: the worker reads that flag
for whichever queued row it claims next, so opening it in this handler
would let an older queued message (a Telegram text sent while the worker
was busy, a second web chat message) be filed as this check-in's note.
A note that is a pause word is not a note (`note_is_pause_word`, plan
section 9's "pause words always win"): it is queued as an ordinary
message instead of a completion, so the pause runs and the check-in
stays unfinished, as in Telegram. Completion, the streak and the reply
all happen in the single worker; the reply reaches the web chat through
the sink.
That is why this endpoint carries `check_send()` and the
`MAX_PENDING_WEB_ROWS` backlog cap on top of the panel-write bucket:
every accepted submit queues a row that reaches the model.

**Silent in Telegram.** Nothing here sends a Telegram message. The one
Telegram-side effect is retiring a *live* check-in keyboard the web
submission made stale (a /checkin started in Telegram and finished on
the web): best-effort, through the real bot for a positive message id,
or through the web bot for a negative one (a /checkin typed into the
web chat), mirroring how app/web/panels/state.py retires stale proposal
buttons.

**`in_progress`** is "a web-submitted check-in is still waiting for the
worker": today's row carries a web-issued (negative) message id and the
completion row for that id is still queued or in flight. A second
submit in that window is a 409 rather than a second queued turn; the
check and the enqueue run under `app["web_checkin_lock"]`, so two
concurrent submits cannot both pass it.

**Never returned:** `chat_id`, `awaiting`/`awaiting_ref`,
`tg_message_id`, the check-in row id. The DTO builders below are the
whole allow-list. Logs carry only an event name and the route -- never
a note, a journal line or an order's text.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import uuid

from aiohttp import web

from app.core import checkin as checkin_core
from app.core import clock as clock_module
from app.core import journal as journal_core
from app.core import orders as orders_core
from app.core.state import get_state
from app.db import queue
from app.db.models import Checkin, Journal
from app.tg import checkin as checkin_ui
from app.web import ingress
from app.web.hub import WebHub
from app.web.http import (
    _cookie_settings,
    _json,
    _parse_positive_int,
    _rate_limited,
    _read_body,
    _session_token_valid,
)
from app.web.ratelimit import MAX_PENDING_WEB_ROWS, WebRateLimiter, pending_web_count

logger = logging.getLogger(__name__)

ROUTE = "/api/checkin"

# Identical by inspection to app/web/panels/memory.py's (and app/web/
# routes.py's `_valid_text`) -- see that module for why each panel keeps
# its own copy rather than importing.
_DISALLOWED_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f]")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")

_MIN_BIGINT = -(2**63)
_MAX_BIGINT = 2**63 - 1

# More than any check-in could ever ask (ORDERS_IN_CHECKIN_MAX is
# validated small); a bound on the shape, not the business rule.
MAX_ORDERS_IN_BODY = 20

_WEB_DUE_RESULTS = (checkin_core.DONE, checkin_core.PARTIAL, checkin_core.NO)
_ORDER_RESULTS = (checkin_core.DONE, checkin_core.NO)

DAYS_DEFAULT = 30
DAYS_MAX = 90

JOURNAL_DEFAULT_LIMIT = 30
JOURNAL_MAX_LIMIT = 50


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _checkin_dto(row: Checkin, order_results: list[tuple[str, str]]) -> dict:
    return {
        "local_date": row.local_date.isoformat(),
        "rating": row.day_rating,
        "due_result": row.due_result,
        "note": row.note,
        "orders": [{"text": text, "result": result} for text, result in order_results],
    }


def _journal_dto(row: Journal) -> dict:
    return {
        "id": row.id,
        "local_date": row.local_date.isoformat(),
        "text": row.text,
        "created_at": _iso(row.created_at),
    }


async def _web_in_flight(session, today: Checkin | None) -> bool:
    """See the module docstring's `in_progress`."""
    if today is None or today.tg_message_id is None or today.tg_message_id >= 0:
        return False
    return await queue.web_callback_pending(
        session, today.tg_message_id, checkin_ui.WEB_SUBMIT_CALLBACK
    )


# --- GET /api/checkin ---------------------------------------------------


async def get_checkin(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)

    async with sessionmaker() as session:
        user_state = await get_state(session)
        timezone = user_state.timezone
        local_date = clock_module.local_date(clock, timezone)
        today = await checkin_core.get_for_date(session, local_date)
        today_dto = None
        if today is not None:
            today_dto = _checkin_dto(today, await orders_core.results_for_checkin(session, today.id))
        form_orders = await checkin_core.form_orders(
            session, clock, timezone, settings.ORDERS_IN_CHECKIN_MAX
        )
        in_progress = await _web_in_flight(session, today)
        last = user_state.last_checkin_at
        done_today = last is not None and clock_module.local_date_of(last, timezone) == local_date
        dto = {
            "local_date": local_date.isoformat(),
            "streak": user_state.streak,
            "last_checkin_at": _iso(last),
            "done_today": done_today,
            "in_progress": in_progress,
            "today": today_dto,
            "form": {
                "due_action": user_state.due_action,
                "orders": [{"id": order.id, "text": order.text} for order in form_orders],
                "note_max": checkin_core.NOTE_MAX,
            },
        }
    return _json(200, dto)


# --- POST /api/checkin --------------------------------------------------


def _parse_submit(body: dict) -> tuple[dict | None, web.Response | None]:
    """Shape (400) then content (422) checks that need no database.

    Returns `(parsed, None)` or `(None, error_response)`. `due_result`
    and the order-id set are checked later, against the database (the
    due action and today's orders), in `post_checkin` itself.
    """
    bad = _json(400, {"error": "bad_request"})

    rating = body.get("rating")
    if not _is_int(rating):
        return None, bad

    due_result = body.get("due_result")
    if due_result is not None and (
        not isinstance(due_result, str) or due_result not in _WEB_DUE_RESULTS
    ):
        return None, bad

    raw_orders = body.get("orders", [])
    if not isinstance(raw_orders, list) or len(raw_orders) > MAX_ORDERS_IN_BODY:
        return None, bad
    order_results: list[tuple[int, str]] = []
    for item in raw_orders:
        if not isinstance(item, dict) or set(item) != {"id", "result"}:
            return None, bad
        order_id = item["id"]
        result = item["result"]
        if not _is_int(order_id) or not _MIN_BIGINT <= order_id <= _MAX_BIGINT:
            return None, bad
        if not isinstance(result, str) or result not in _ORDER_RESULTS:
            return None, bad
        order_results.append((order_id, result))

    note = body.get("note")
    if note is not None:
        if not isinstance(note, str):
            return None, bad
        if _DISALLOWED_CONTROL_RE.search(note) or _SURROGATE_RE.search(note):
            return None, bad

    if not 1 <= rating <= 5:
        return None, _json(422, {"error": "invalid", "detail": "rating"})
    stripped = note.strip() if note is not None else ""
    if len(stripped) > checkin_core.NOTE_MAX:
        return None, _json(422, {"error": "invalid", "detail": "note_too_long"})
    if stripped.startswith("/"):
        return None, _json(422, {"error": "invalid", "detail": "note_command"})

    return {
        "rating": rating,
        "due_result": due_result,
        "orders": order_results,
        "note": stripped or None,
    }, None


async def _retire_old_keyboard(request: web.Request, chat_id: int, message_id: int) -> None:
    """Best-effort: drop the check-in keyboard the web submission just
    made stale. A Telegram-side failure (deleted message, network
    error) is logged by type and swallowed -- the submission is already
    committed and queued."""
    bot = request.app["bot"] if message_id > 0 else request.app["web_bot"]
    try:
        await checkin_ui.retire_for_web(bot, chat_id, message_id)
    except Exception as exc:  # noqa: BLE001 - best-effort, matching the proposals panel's guard
        logger.warning(
            "retiring checkin keyboard failed",
            extra={"event": type(exc).__name__, "route": ROUTE},
        )


async def _submit_locked(settings, sessionmaker, clock, parsed) -> web.Response | int | None:
    """The check-then-act part of POST /api/checkin, run under
    `web_checkin_lock`. Returns an error response, or the check-in's
    previous message id (None if there was none) for keyboard retiring.
    """
    async with sessionmaker() as session:
        user_state = await get_state(session)
        timezone = user_state.timezone
        today = await checkin_core.today(session, clock, timezone)
        if await _web_in_flight(session, today):
            return _json(409, {"error": "in_progress"})
        if await pending_web_count(session) >= MAX_PENDING_WEB_ROWS:
            return _rate_limited(5.0)

        due_result = checkin_core.resolve_due_result(user_state.due_action, parsed["due_result"])
        if due_result is None:
            return _json(422, {"error": "invalid", "detail": "due_result"})
        expected = {
            order.id
            for order in await checkin_core.form_orders(
                session, clock, timezone, settings.ORDERS_IN_CHECKIN_MAX
            )
        }
        submitted = [order_id for order_id, _ in parsed["orders"]]
        if len(set(submitted)) != len(submitted) or set(submitted) != expected:
            return _json(422, {"error": "invalid", "detail": "orders"})

        old_message_id = today.tg_message_id if today is not None else None
        note = parsed["note"]
        pause_note = checkin_core.note_is_pause_word(note)
        try:
            # As Telegram's command middleware would for /checkin:
            # whatever conversational step was open is closed first.
            await checkin_core.clear_awaiting(session)
            message_id = await queue.reserve_web_id(session)
            await checkin_core.submit(
                session,
                clock,
                timezone,
                rating=parsed["rating"],
                due_result=due_result,
                order_results=parsed["orders"],
                note=None if pause_note else note,
                message_id=message_id,
            )
            if pause_note:
                # Queued as an ordinary message: turn.run's pause handling
                # runs in the worker, and the check-in stays unfinished.
                await ingress.send_text(
                    session, settings=settings, text=note, client_key=str(uuid.uuid4())
                )
            else:
                await ingress.checkin_complete(session, settings=settings, message_id=message_id)
        except ingress.BlockedCommand:
            # Unreachable ("/" is refused in _parse_submit); defensive.
            return _json(422, {"error": "invalid", "detail": "note_command"})
        except Exception as exc:  # noqa: BLE001 - see below
            # A database failure part-way (each core step commits on its
            # own). Nothing it can leave behind swallows a later message:
            # the note step is never opened here, and a row with no
            # completion queued is not in_progress, so a resubmit simply
            # restarts the day. Logged by type only -- a DBAPIError's
            # text carries the statement's parameters, i.e. the note --
            # and never re-raised into aiohttp's own traceback logging.
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 - the connection may be gone too
                pass
            logger.warning(
                "checkin submit failed",
                extra={"event": type(exc).__name__, "route": ROUTE},
            )
            return _json(500, {"error": "internal"})
    return old_message_id


async def post_checkin(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    parsed, error = _parse_submit(body)
    if error is not None:
        return error

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)
    retry = limiter.check_send()
    if retry is not None:
        return _rate_limited(retry)

    lock: asyncio.Lock = request.app["web_checkin_lock"]
    async with lock:
        outcome = await _submit_locked(settings, sessionmaker, clock, parsed)
    if isinstance(outcome, web.Response):
        return outcome
    old_message_id = outcome

    if old_message_id:
        await _retire_old_keyboard(request, settings.ALLOWED_CHAT_ID, old_message_id)
    hub.publish_invalidate("checkin")
    logger.info("checkin submitted", extra={"event": "checkin_submitted", "route": ROUTE})
    return _json(202, {})


# --- GET /api/checkins ----------------------------------------------------


def _parse_days(raw: str | None) -> int | None:
    if raw is None:
        return DAYS_DEFAULT
    if not raw.isascii() or not raw.isdigit() or len(raw) > 3:
        return None
    value = int(raw)
    return value if 1 <= value <= DAYS_MAX else None


async def get_checkins(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, clock, _bot = _cookie_settings(request)

    days = _parse_days(request.query.get("days"))
    if days is None:
        return _json(400, {"error": "bad_request"})

    async with sessionmaker() as session:
        user_state = await get_state(session)
        end = clock_module.local_date(clock, user_state.timezone)
        start = end - datetime.timedelta(days=days - 1)
        rows = await checkin_core.list_range(session, start, end)
        results = await orders_core.results_for_checkins(session, [row.id for row in rows])

    return _json(
        200,
        {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "days": days,
            "items": [_checkin_dto(row, results.get(row.id, [])) for row in rows],
        },
    )


# --- GET /api/journal -------------------------------------------------------


async def get_journal(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, _clock, _bot = _cookie_settings(request)

    # Clamped, not rejected -- app/web/panels/memory.py's `get_memories`
    # pattern: an out-of-range offset is just a page with nothing on it.
    offset = _parse_positive_int(request.query.get("offset"), 0) or 0
    offset = min(offset, 2**31)
    limit = _parse_positive_int(request.query.get("limit"), JOURNAL_DEFAULT_LIMIT)
    limit = min(limit, JOURNAL_MAX_LIMIT)

    async with sessionmaker() as session:
        rows, total = await journal_core.list_journal(session, offset, limit)

    return _json(200, {"items": [_journal_dto(row) for row in rows], "total": total})


# --- registration --------------------------------------------------------


def register(app: web.Application) -> None:
    app.router.add_get("/api/checkin", get_checkin)
    app.router.add_post("/api/checkin", post_checkin)
    app.router.add_get("/api/checkins", get_checkins)
    app.router.add_get("/api/journal", get_journal)
