"""GET /api/memories and the add/edit/pin/unpin/forget endpoints (W3
plan step 2, "Память" screen).

Same shape as app/web/panels/state.py and app/web/panels/proposals.py:
`_session_token_valid` first, then `_read_body`/validation, then
`request.app["web_rate_limiter"].check_panel_write()`, then core calls
only (`app/core/memory.py`, never a bare SQL write against `memory`),
then `checkin_core.clear_awaiting` and `hub.publish_invalidate(...)`.

**Silent in Telegram, by design (W3's own user decision).** Nothing
here imports `app/tg/send.py` or touches `app["bot"]` -- a web-issued
add/edit/pin/forget sends no Telegram message and edits no Telegram
keyboard, unlike app/web/panels/state.py's proposal-expiry buttons or
app/web/panels/proposals.py's decision-outcome edit, neither of which
this screen has an analog of (a memory was never offered as a Telegram
inline button the way a proposal or a stale due/focus value is).

**Audit source is "web"**, matching app/tg/memory.py's own "command"/
"button"/"user" sources in shape: a value this codebase already uses to
say *how* a change was made, not narrowed or reused from Telegram's own
value.

**Every write invalidates both "memory" and "state"**, unconditionally
-- even a pin/unpin or an edit, neither of which changes
`memory_core.count_active`'s number the State screen shows. This is the
same "cheap direction: both, every time" call app/web/tail.py's own
`_tail_proposals_once` docstring already makes for the *tail's* memory
fingerprint (which fires on every memory write, from Telegram or here
alike, for the identical reason: telling apart "this write changed the
count" from "this write didn't" costs a second query this handler
already has the data to skip paying). Publishing here needn't
outsource this decision to the tail's own two-second poll on top of it
-- a stale State screen after a same-second web write is exactly what
this file exists to avoid.

**Never returned:** superseded rows (`memory_core.list_active`/
`get_active` already exclude them), `confidence` (internal-only,
per plan section 2's `MemoryDTO`), and `PendingMemory` rows (no
endpoint here reads that table at all -- /remember's two-step flow
stays Telegram-only, W3 does not add a web equivalent).

**IDs are positive ints in the path, bounded at bigint's own range.**
`_parse_memory_id` returns `None` for anything else (non-numeric,
negative, zero, or past `2**63 - 1`), which every handler below turns
into a 404, never a 500 -- the same shape app/web/panels/proposals.py's
`_parse_proposal_id` uses, narrowed to `> 0` since a memory id is never
zero or negative, and further bounded because Postgres itself would
otherwise 500 on an id past what its own `bigint` column can hold
(W3 finding; `get_memories`'s `offset` is clamped the same way, for the
same reason).
"""

from __future__ import annotations

import datetime
import re

from aiohttp import web

from app.core import checkin as checkin_core
from app.core import memory as memory_core
from app.web.hub import WebHub
from app.web.http import (
    _cookie_settings,
    _json,
    _parse_positive_int,
    _rate_limited,
    _read_body,
    _session_token_valid,
)
from app.web.ratelimit import WebRateLimiter

MEMORIES_DEFAULT_LIMIT = 30
MEMORIES_MAX_LIMIT = 50

# The same shape check app/web/routes.py's `_valid_text` applies to the
# chat box (design section 5): every C0 control character except tab
# and newline is refused, checked against the *original* string before
# strip() -- not imported from routes.py, which itself imports
# app.web.panels (register()), so importing back would be circular;
# kept identical by inspection instead, the same way this module's
# MEMORY_TEXT_MAX now lives in app/core/memory.py precisely so the two
# transports cannot drift on *that* number the way two copies of a
# regex still could here. Shape violations are a 400 `bad_request`,
# matching `_valid_text`'s own collapse of "not a string" and "has a
# disallowed byte" into one status -- distinct from the 422 `invalid`
# detail codes below, which are about content (empty/too long/bad
# kind), not shape.
_DISALLOWED_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f]")

# W3 finding: a lone UTF-16 surrogate (half of a malformed \uD800-range
# escape) passes both the control-character check above and Python's
# own str type, then fails asyncpg's UTF-8 encoding at the query
# boundary -- an unhandled 500 whose aiohttp.server traceback includes
# the offending text as a SQL parameter repr, breaking the rule that
# logs never contain memory text. Kept as its own regex, identical by
# inspection to app/web/routes.py's `_valid_text`, for the same reason
# `_DISALLOWED_CONTROL_RE` above is duplicated rather than imported.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")

# The upper end of Postgres bigint (memory.id's column type). Anything
# past this reaches the database and raises an unhandled 500 instead of
# the 404 every other malformed id gets -- see `_parse_memory_id`.
_MAX_BIGINT = 2**63 - 1


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _memory_dto(row: memory_core.Memory, *, has_predecessor: bool = False) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "kind_label": memory_core.KIND_LABEL.get(row.kind, row.kind),
        "text": row.text,
        "pinned": row.pinned,
        "source": row.source,
        "use_count": row.use_count,
        "last_used_at": _iso(row.last_used_at),
        "created_at": _iso(row.created_at),
        # W3 finding: forgetting the head of a chain with a predecessor
        # (an edited or consolidate-merged row) resurrects that
        # predecessor -- hard_delete's own documented behavior, not a
        # bug (see app/core/memory.py's has_predecessors docstring).
        # The confirm dialog warns on this rather than the backend
        # silently changing what "Забыть" does.
        "has_predecessor": has_predecessor,
    }


def _valid_shape(value: object) -> bool:
    return (
        isinstance(value, str)
        and not _DISALLOWED_CONTROL_RE.search(value)
        and not _SURROGATE_RE.search(value)
    )


def _parse_memory_id(request: web.Request) -> int | None:
    try:
        value = int(request.match_info["id"])
    except ValueError:
        return None
    return value if 0 < value <= _MAX_BIGINT else None


async def _duplicate_response(session, text: str, *, ignore_id: int | None = None) -> web.Response:
    """409, carrying the existing row the dedupe check hit -- `write_memory`
    itself reports only *that* a duplicate exists, never which row (its
    own docstring), so this re-runs the same query app.core.memory.
    near_duplicate uses to find it, in the same still-open transaction.
    """
    duplicate = await memory_core.near_duplicate(session, text, ignore_id=ignore_id)
    existing = _memory_dto(duplicate) if duplicate is not None else None
    return _json(409, {"error": "duplicate", "existing": existing})


def _invalidate(hub: WebHub) -> None:
    hub.publish_invalidate("memory")
    hub.publish_invalidate("state")


# --- GET /api/memories -------------------------------------------------


def _parse_kind_filter(raw: str | None) -> tuple[str | None, bool]:
    if raw is None or raw == "":
        return None, True
    if raw not in memory_core.KINDS:
        return None, False
    return raw, True


def _parse_pinned_filter(raw: str | None) -> tuple[bool | None, bool]:
    if raw is None or raw == "":
        return None, True
    if raw == "true":
        return True, True
    if raw == "false":
        return False, True
    return None, False


async def get_memories(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, _clock, _bot = _cookie_settings(request)

    kind, kind_ok = _parse_kind_filter(request.query.get("kind"))
    pinned, pinned_ok = _parse_pinned_filter(request.query.get("pinned"))
    if not kind_ok or not pinned_ok:
        return _json(400, {"error": "bad_request"})

    offset = _parse_positive_int(request.query.get("offset"), 0) or 0
    # An offset past this (e.g. a stale/tampered "Показать ещё" value)
    # would otherwise reach Postgres and raise an unhandled 500 --
    # clamped rather than rejected, since an out-of-range offset is not
    # a shape violation, just a page with nothing on it.
    offset = min(offset, 2**31)
    limit = _parse_positive_int(request.query.get("limit"), MEMORIES_DEFAULT_LIMIT)
    limit = min(limit, MEMORIES_MAX_LIMIT)

    async with sessionmaker() as session:
        rows, total = await memory_core.list_active(
            session, offset=offset, limit=limit, kind=kind, pinned=pinned, order="web"
        )
        pinned_count = await memory_core.count_pinned(session)
        predecessors = await memory_core.has_predecessors(session, [row.id for row in rows])

    return _json(
        200,
        {
            "items": [
                _memory_dto(row, has_predecessor=row.id in predecessors) for row in rows
            ],
            "total": total,
            "pinned_count": pinned_count,
            "pinned_max": settings.MEMORY_PINNED_MAX,
        },
    )


# --- POST /api/memories (add) -------------------------------------------


async def post_memory(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    kind = body.get("kind")
    text = body.get("text")
    if not isinstance(kind, str) or not _valid_shape(text):
        return _json(400, {"error": "bad_request"})

    if kind not in memory_core.KINDS:
        return _json(422, {"error": "invalid", "detail": "bad_kind"})
    stripped = text.strip()
    if not stripped:
        return _json(422, {"error": "invalid", "detail": "empty"})
    if len(stripped) > memory_core.MEMORY_TEXT_MAX:
        return _json(422, {"error": "invalid", "detail": "too_long"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        written = await memory_core.write_memory(
            session, kind=kind, text=stripped, source="user"
        )
        if written is None:
            return await _duplicate_response(session, stripped)
        dto = _memory_dto(written)

    _invalidate(hub)
    return _json(201, {"memory": dto})


# --- POST /api/memories/{id}/edit ---------------------------------------


async def post_edit(request: web.Request) -> web.Response:
    """The Telegram equivalent of a correction: a new row, the old one
    superseded (kept as history, not returned by any endpoint here) --
    never an in-place update, matching app/core/memory.py's own "a fact
    is never edited in place" rule (module docstring).
    """
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    memory_id = _parse_memory_id(request)
    if memory_id is None:
        return _json(404, {"error": "not_found"})

    body, error = await _read_body(request)
    if error is not None:
        return error
    text = body.get("text")
    if not _valid_shape(text):
        return _json(400, {"error": "bad_request"})
    stripped = text.strip()
    if not stripped:
        return _json(422, {"error": "invalid", "detail": "empty"})
    if len(stripped) > memory_core.MEMORY_TEXT_MAX:
        return _json(422, {"error": "invalid", "detail": "too_long"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        old = await memory_core.get_active(session, memory_id)
        if old is None:
            return _json(404, {"error": "not_found"})
        written = await memory_core.write_memory(
            session,
            kind=old.kind,
            text=stripped,
            source="user",
            # W3 finding: write_memory's own default (pinned=False)
            # would silently unpin every edited memory -- carry the old
            # row's pinned state forward, the same way its kind already
            # does two lines up.
            pinned=old.pinned,
            supersedes_id=memory_id,
        )
        if written is None:
            return await _duplicate_response(session, stripped, ignore_id=memory_id)
        # written.superseded_by is always None here (freshly inserted)
        # and old.id always points at it -- has_predecessor is true by
        # construction, no extra query needed.
        dto = _memory_dto(written, has_predecessor=True)

    _invalidate(hub)
    return _json(200, {"memory": dto})


# --- POST /api/memories/{id}/pin | unpin ---------------------------------


async def _set_pinned(request: web.Request, *, pinned: bool) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    memory_id = _parse_memory_id(request)
    if memory_id is None:
        return _json(404, {"error": "not_found"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        outcome = await memory_core.set_pinned_capped(
            session, memory_id, pinned, max_pinned=settings.MEMORY_PINNED_MAX
        )
        if outcome == memory_core.PIN_MISSING:
            return _json(404, {"error": "not_found"})
        if outcome == memory_core.PIN_OVER_CAP:
            return _json(409, {"error": "over_cap", "max": settings.MEMORY_PINNED_MAX})
        row = await memory_core.get_active(session, memory_id)
        predecessors = await memory_core.has_predecessors(session, [memory_id])
        dto = _memory_dto(row, has_predecessor=memory_id in predecessors)

    _invalidate(hub)
    return _json(200, {"memory": dto})


async def post_pin(request: web.Request) -> web.Response:
    return await _set_pinned(request, pinned=True)


async def post_unpin(request: web.Request) -> web.Response:
    return await _set_pinned(request, pinned=False)


# --- POST /api/memories/{id}/forget ---------------------------------------


async def post_forget(request: web.Request) -> web.Response:
    """A hard delete -- irreversible, per plan section 11 -- so this
    endpoint accepts exactly one id from the path and nothing from the
    body worth reading (no bulk `ids` list: `_read_body` is not even
    called here, since there is no field to validate).

    **Superseded ids are refused the same 404 as a missing one (W3
    finding).** `edit`/`pin`/`unpin` already 404 on a superseded id via
    `get_active`; forget used to hard-delete it and return 200,
    because `memory_core.forget` (unlike this module) intentionally
    accepts *any* row id -- Telegram's /forget has always been able to
    delete a row no /memories listing shows. That gap gave the web an
    existence oracle for hidden history (a superseded id 200s, an
    unused id 404s) and let a stale tab "forget" a fact whose correction
    -- the row that actually replaced it -- stays active and unaffected.
    Checking `get_active` first closes both, without changing
    `memory_core.forget` or Telegram's own behavior at all.
    """
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    memory_id = _parse_memory_id(request)
    if memory_id is None:
        return _json(404, {"error": "not_found"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        if await memory_core.get_active(session, memory_id) is None:
            return _json(404, {"error": "not_found"})
        outcome = await memory_core.forget(session, memory_id, source="web")
    if outcome == memory_core.FORGET_PROTECTED:
        return _json(409, {"error": "adopted"})
    if outcome != memory_core.FORGET_OK:
        return _json(404, {"error": "not_found"})

    _invalidate(hub)
    return _json(200, {})


# --- registration --------------------------------------------------------


def register(app: web.Application) -> None:
    app.router.add_get("/api/memories", get_memories)
    app.router.add_post("/api/memories", post_memory)
    app.router.add_post("/api/memories/{id}/edit", post_edit)
    app.router.add_post("/api/memories/{id}/pin", post_pin)
    app.router.add_post("/api/memories/{id}/unpin", post_unpin)
    app.router.add_post("/api/memories/{id}/forget", post_forget)
