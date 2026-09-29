"""GET /api/obligations and POST /api/obligations/{id}/done|drop: the
debt queue (app/core/obligations.py) on the web's Сегодня page.

The Telegram side is `/paid` and its `ob:d:`/`ob:x:` buttons
(app/tg/obligations.py); both close through the same
`obligations.close`, which only touches a row that is still open, so a
debt closed here and then pressed in an old `/paid` message gets that
message's own «Устарело» answer -- nothing here needs to retire those
buttons.

Closing follows every panel write's shape (app/web/panels/__init__.py):
session, the shared panel-write bucket, `checkin_core.clear_awaiting`,
the core call, then `invalidate("debts")`. Silent in Telegram. Nothing
is logged but the event name: a debt's text is the user's own words
(a promise, the day's action), so it never reaches a log line.
"""

from __future__ import annotations

import logging

from aiohttp import web

from app.core import checkin as checkin_core
from app.core import obligations as obligations_core
from app.db.models import Obligation
from app.web.hub import WebHub
from app.web.http import _cookie_settings, _json, _rate_limited, _session_token_valid
from app.web.panels._common import path_id
from app.web.ratelimit import WebRateLimiter

logger = logging.getLogger(__name__)

_STATUS_BY_ACTION = {"done": obligations_core.DONE, "drop": obligations_core.DROPPED}


def _obligation_dto(row: Obligation) -> dict:
    return {
        "id": row.id,
        "text": row.text,
        "kind": row.kind,
        "source": row.source,
        "opened_at": row.opened_at.isoformat(),
        "due_local_date": row.due_local_date.isoformat() if row.due_local_date else None,
    }


async def get_obligations(request: web.Request) -> web.Response:
    """The open debts, oldest first, and the queue's cap."""
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    async with sessionmaker() as session:
        rows = await obligations_core.open_list(session)
    return _json(
        200,
        {"items": [_obligation_dto(row) for row in rows], "max_open": obligations_core.MAX_OPEN},
    )


async def _close(request: web.Request, action: str) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    obligation_id = path_id(request)
    if obligation_id is None:
        return _json(404, {"error": "not_found"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        closed = await obligations_core.close(session, clock, obligation_id, _STATUS_BY_ACTION[action])

    # Already closed (Telegram, another tab) or never existed: the same
    # 404, so the list refetches and the row simply goes away.
    if closed is None:
        return _json(404, {"error": "not_found"})
    logger.info("obligation closed from the web", extra={"event": f"web_obligation_{action}"})
    hub.publish_invalidate("debts")
    return _json(200, {"obligation": {"id": closed.id, "status": closed.status}})


async def post_done(request: web.Request) -> web.Response:
    return await _close(request, "done")


async def post_drop(request: web.Request) -> web.Response:
    return await _close(request, "drop")


def register(app: web.Application) -> None:
    app.router.add_get("/api/obligations", get_obligations)
    app.router.add_post("/api/obligations/{id}/done", post_done)
    app.router.add_post("/api/obligations/{id}/drop", post_drop)
