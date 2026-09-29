"""GET /api/notebook, POST /api/notebook and POST /api/notebook/{id}/close:
Echo's notebook (app/core/notebook.py) on the web's Память page.

The same three writers as Telegram's `/mind`, `/mind add` and its ✖
button (app/tg/notebook.py): the list is `notebook.active_entries`, an
add is `notebook.add_user_intention` (the user may only add an
intention; observations and open threads are Echo's own), and a close
is `notebook.close_entry(by="user")`, which accepts an entry of any
source -- the user can close anything in the notebook, Echo's entries
included. The add's outcomes map to the same Russian replies `/mind
add` gives.

Every write: session, the shared panel-write bucket,
`checkin_core.clear_awaiting`, the core call, `invalidate("notebook")`.
Silent in Telegram; entry text never reaches a log line.
"""

from __future__ import annotations

import logging

from aiohttp import web

from app.core import checkin as checkin_core
from app.core import notebook as notebook_core
from app.tg.notebook import ADD_REPLIES
from app.web.hub import WebHub
from app.web.http import _cookie_settings, _json, _rate_limited, _read_body, _session_token_valid
from app.web.panels._common import path_id, valid_text
from app.web.ratelimit import WebRateLimiter

logger = logging.getLogger(__name__)


def _group(items: list[tuple[int, str, str]]) -> list[dict]:
    return [{"id": entry_id, "text": text, "source": source} for entry_id, text, source in items]


async def get_notebook(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    async with sessionmaker() as session:
        view = await notebook_core.active_entries(session)
    return _json(
        200,
        {
            "intentions": _group(view.intentions),
            "observations": _group(view.observations),
            "threads": _group(view.threads),
            "limits": {
                "text_max": notebook_core.TEXT_MAX,
                "intentions_max": settings.NOTEBOOK_MAX_INTENTIONS,
            },
        },
    )


async def post_notebook(request: web.Request) -> web.Response:
    """Add one intention. 201 on success; otherwise 422 with `detail`
    one of add_user_intention's own results (refused, cap, too_long,
    duplicate, or empty) and `message`, the Russian reply `/mind add`
    gives for it."""
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    text = body.get("text")
    if not valid_text(text):
        return _json(400, {"error": "bad_request"})
    if not text.strip():
        return _json(422, {"error": "invalid", "detail": "empty"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        result = await notebook_core.add_user_intention(session, settings, text, clock=clock)

    if result != "ok":
        logger.info("notebook add refused", extra={"event": f"web_notebook_{result}"})
        return _json(422, {"error": "invalid", "detail": result, "message": ADD_REPLIES[result]})
    logger.info("notebook intention added from the web", extra={"event": "web_notebook_add"})
    hub.publish_invalidate("notebook")
    return _json(201, {})


async def post_close(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    entry_id = path_id(request)
    if entry_id is None:
        return _json(404, {"error": "not_found"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        closed = await notebook_core.close_entry(session, entry_id, by="user", clock=clock)

    if not closed:
        return _json(404, {"error": "not_found"})
    logger.info("notebook entry closed from the web", extra={"event": "web_notebook_close"})
    hub.publish_invalidate("notebook")
    return _json(200, {})


def register(app: web.Application) -> None:
    app.router.add_get("/api/notebook", get_notebook)
    app.router.add_post("/api/notebook", post_notebook)
    app.router.add_post("/api/notebook/{id}/close", post_close)
