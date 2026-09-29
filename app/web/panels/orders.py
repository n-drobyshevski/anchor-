"""GET /api/orders, POST /api/orders and POST /api/orders/{id}/retire:
standing orders (app/core/orders.py) -- the user's recurring
commitments, asked about in every check-in -- on the web's Память page.

The same writers as Telegram's `/order <каденция> <текст>` and
`/orders`' [Снять] (app/tg/orders.py): `orders.create_active` with
`source="user"` (the web user is the user; standing_order.source allows
only anchor/user/review) and `orders.retire`. Proposed orders -- the
extractor's and the weekly review's suggestions, decided with buttons
-- are not listed here; accepting those comes with the review on
Дневник.

The cadence arrives as `parse_cadence`'s own token (`daily`,
`weekdays`, `weekly:<1-7>`, `once`), so the web and `/order` accept
exactly the same set.

Every write: session, the shared panel-write bucket,
`checkin_core.clear_awaiting`, the core call, then
`invalidate("orders")` and `invalidate("checkin")` -- the check-in
form asks about every active order, so Сегодня's form must refetch.
Silent in Telegram; order text never reaches a log line.
"""

from __future__ import annotations

import logging

from aiohttp import web

from app.core import checkin as checkin_core
from app.core import orders as orders_core
from app.db.models import StandingOrder
from app.web.hub import WebHub
from app.web.http import _cookie_settings, _json, _rate_limited, _read_body, _session_token_valid
from app.web.panels._common import path_id, valid_text
from app.web.ratelimit import WebRateLimiter

logger = logging.getLogger(__name__)

_MESSAGES = {"refused": orders_core.REFUSAL_TEXT, "cap": orders_core.CAP_TEXT}


def _order_dto(row: StandingOrder) -> dict:
    return {
        "id": row.id,
        "text": row.text,
        "cadence": row.cadence,
        "weekday": row.weekday,
        "cadence_label": orders_core.cadence_label(row.cadence, row.weekday),
        "source": row.source,
    }


def _invalidate(hub: WebHub) -> None:
    hub.publish_invalidate("orders")
    hub.publish_invalidate("checkin")


async def get_orders(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    async with sessionmaker() as session:
        rows = await orders_core.active_orders(session)
    return _json(
        200,
        {
            "items": [_order_dto(row) for row in rows],
            "limits": {"text_max": orders_core.TEXT_MAX, "active_max": settings.ORDERS_MAX_ACTIVE},
        },
    )


async def post_order(request: web.Request) -> web.Response:
    """Create one active order. 201 on success; 422 `bad_cadence` for a
    cadence `parse_cadence` rejects, `empty`, or `refused`/`cap` (with
    `message`, the Russian reply `/order` gives)."""
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    text = body.get("text")
    cadence_token = body.get("cadence")
    if not valid_text(text) or not isinstance(cadence_token, str):
        return _json(400, {"error": "bad_request"})
    if not text.strip():
        return _json(422, {"error": "invalid", "detail": "empty"})
    parsed = orders_core.parse_cadence(cadence_token)
    if parsed is None:
        return _json(422, {"error": "invalid", "detail": "bad_cadence"})
    cadence, weekday = parsed

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        result = await orders_core.create_active(
            session, settings, text, cadence, weekday, source="user", clock=clock
        )

    if result != "ok":
        logger.info("order create refused", extra={"event": f"web_order_{result}"})
        return _json(422, {"error": "invalid", "detail": result, "message": _MESSAGES[result]})
    logger.info("order created from the web", extra={"event": "web_order_create"})
    _invalidate(hub)
    return _json(201, {})


async def post_retire(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    _settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    order_id = path_id(request)
    if order_id is None:
        return _json(404, {"error": "not_found"})

    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        result = await orders_core.retire(session, order_id, clock=clock)

    if result != "ok":
        return _json(404, {"error": "not_found"})
    logger.info("order retired from the web", extra={"event": "web_order_retire"})
    _invalidate(hub)
    return _json(200, {})


def register(app: web.Application) -> None:
    app.router.add_get("/api/orders", get_orders)
    app.router.add_post("/api/orders", post_order)
    app.router.add_post("/api/orders/{id}/retire", post_retire)
