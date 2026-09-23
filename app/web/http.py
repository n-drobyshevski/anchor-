"""Shared HTTP-shaped helpers for the web chat's handlers (web-chat
plan track 2), split out of app/web/routes.py so app/web/routes.py
(the static manifest, the SSE writer, and every `/api/*` handler) and
any other module that needs the same request-shaped plumbing --
without duplicating it -- can both import from one place.

Every function here moved out of routes.py **unchanged in behavior**:
same signatures, same status codes, same cookie flags. Nothing in this
module knows about a specific route's business logic (that stays in
routes.py); it only knows how to read `app[...]`, build the handful of
JSON/cookie response shapes the HTTP API contract reuses across
several handlers, and read a bounded JSON body.
"""

from __future__ import annotations

from aiogram import Bot
from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.core.clock import Clock
from app.web import auth, security


def _json(status: int, data: dict, *, retry_after: float | None = None) -> web.Response:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
    return web.json_response(data, status=status, headers=headers)


def _rate_limited(retry_after: float) -> web.Response:
    seconds = max(1, int(retry_after + 0.999))
    return _json(429, {"error": "rate_limited", "retry_after": seconds}, retry_after=retry_after)


async def _read_body(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Bounded JSON read, translated to the contract's error bodies.

    Returns (body, None) on success, or (None, error_response).
    """
    try:
        body = await security.read_json_bounded(request)
    except security.LengthRequired:
        return None, _json(411, {"error": "length_required"})
    except security.PayloadTooLarge:
        return None, _json(413, {"error": "payload_too_large"})
    except security.BadJson:
        return None, _json(400, {"error": "bad_request"})
    return body, None


def _cookie_settings(request: web.Request) -> tuple[Settings, async_sessionmaker, Clock, Bot]:
    app = request.app
    return app["settings"], app["sessionmaker"], app["clock"], app["bot"]


async def _session_token_valid(request: web.Request) -> bool:
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    token = request.cookies.get(auth.SESSION_COOKIE)
    async with sessionmaker() as session:
        return await auth.validate_session(session, clock, settings, token)


def _set_pre_cookie(response: web.Response, token: str, ttl_s: int) -> None:
    response.set_cookie(
        auth.PRE_COOKIE, token, max_age=ttl_s, path="/", secure=True, httponly=True, samesite="Strict"
    )


def _clear_pre_cookie(response: web.Response) -> None:
    response.del_cookie(auth.PRE_COOKIE, path="/", secure=True, samesite="Strict")


def _set_session_cookie(response: web.Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=settings.WEB_SESSION_MAX_DAYS * 24 * 3600,
        path="/",
        secure=True,
        httponly=True,
        samesite="Strict",
    )


def _clear_session_cookie(response: web.Response) -> None:
    response.del_cookie(auth.SESSION_COOKIE, path="/", secure=True, samesite="Strict")


def _parse_positive_int(raw: str | None, default: int | None) -> int | None:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default
