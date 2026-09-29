"""Настройки's integrations and background work: vault notes, the
planner's sync switch, Claude's connection (read-only), and the idle
digest with undo.

- GET /api/settings: what is switched on and what is connected. Only
  flags, counts and times: never a token, a path or a note title. Each
  integration is null when its deploy-level setting is off
  (VAULT_MODE, PLANNER_ENABLED, CLAUDE_ACCESS_ENABLED), the same way
  /menu hides its section.
- POST /api/settings/notes {on}: `/vault notes on|off`, through
  app/vault/consent.py. Off deletes everything read from notes (a
  derived index, rebuilt from the vault when turned back on) -- /menu
  offers both directions to the web too.
- POST /api/settings/planner {on}: `/planner on|off`, through
  app/planner/auth.py's set_enabled -- it only pauses syncing. Linking
  the planner stays Telegram-only (/planner_link, ingress.py).
- Claude's library switches are shown but never written here: like
  /claude itself they are Telegram-only (app/tg/menu.py's
  action_available refuses them for the web), so a stolen web session
  cannot open the notes to Claude.
- GET /api/digest?window=24h|7d: `/digest`'s own text
  (app/core/idle/digest.py's build_digest, unchanged) plus the runs it
  offers to undo, each labelled from its own summary counts.
- POST /api/digest/{id}/undo: app/core/idle/undo.py's undo_run, the
  same as /digest's [Отменить].

Every write: session, the shared panel-write bucket,
`checkin_core.clear_awaiting`, the core call, `invalidate("settings")`
(an undo also "memory" and "notebook": it restores rows there).
Silent in Telegram.
"""

from __future__ import annotations

import logging

from aiohttp import web
from sqlalchemy import select

from app.core import checkin as checkin_core
from app.core.idle import CONSOLIDATE, REFLECT
from app.core.idle.digest import WINDOW_24H, WINDOWS, build_digest
from app.core.idle.undo import STATUS_OK, undo_run
from app.core.state import get_state
from app.db.models import IdleRun, VaultStatus
from app.planner import auth as planner_auth
from app.vault import consent as vault_consent
from app.web import oauth_store
from app.web.hub import WebHub
from app.web.http import _cookie_settings, _json, _rate_limited, _read_body, _session_token_valid
from app.web.panels._common import path_id
from app.web.ratelimit import WebRateLimiter

logger = logging.getLogger(__name__)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


async def _settings_dto(session, settings, clock) -> dict:
    state = await get_state(session)
    vault = None
    if settings.VAULT_MODE != "off":
        row = await session.get(VaultStatus, 1)
        vault = {
            "mode": settings.VAULT_MODE,
            "notes_consent": state.notes_consent,
            "knowledge_enabled": settings.VAULT_KNOWLEDGE_ENABLED,
            "last_ok_at": _iso(row.last_ok_at) if row else None,
            "last_unavailable_at": _iso(row.last_unavailable_at) if row else None,
        }
    planner = None
    if settings.PLANNER_ENABLED:
        credential = await planner_auth.get_status(session)
        planner = {
            "linked": credential is not None,
            "status": credential.status if credential else None,
            "enabled": credential.enabled if credential else False,
        }
    claude = None
    if settings.CLAUDE_ACCESS_ENABLED:
        connection = await oauth_store.current_connection(session, clock)
        claude = {
            "connected": connection is not None,
            "expires_at": _iso(connection.expires_at) if connection else None,
            "library_read": bool(connection and connection.library_read),
            "library_write": bool(connection and connection.library_write),
        }
    return {
        "vault": vault,
        "planner": planner,
        "claude": claude,
        "idle": {"enabled": settings.IDLE_ENABLED, "undo_days": settings.IDLE_UNDO_DAYS},
    }


async def get_settings(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    async with sessionmaker() as session:
        dto = await _settings_dto(session, settings, clock)
    return _json(200, dto)


async def _read_on(request: web.Request) -> tuple[bool | None, web.Response | None]:
    body, error = await _read_body(request)
    if error is not None:
        return None, error
    on = body.get("on")
    if not isinstance(on, bool):
        return None, _json(400, {"error": "bad_request"})
    return on, None


async def post_notes(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]
    if settings.VAULT_MODE == "off":
        return _json(404, {"error": "not_found"})

    on, error = await _read_on(request)
    if error is not None:
        return error
    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        await vault_consent.set_notes_consent(session, on)
        dto = await _settings_dto(session, settings, clock)
    logger.info("notes consent set from the web", extra={"event": "web_notes_on" if on else "web_notes_off"})
    hub.publish_invalidate("settings")
    return _json(200, {"settings": dto})


async def post_planner(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]
    if not settings.PLANNER_ENABLED:
        return _json(404, {"error": "not_found"})

    on, error = await _read_on(request)
    if error is not None:
        return error
    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        row = await planner_auth.set_enabled(session, on)
        if row is None:
            # Never linked: linking is /planner_link, Telegram only.
            return _json(409, {"error": "not_linked"})
        dto = await _settings_dto(session, settings, clock)
    logger.info("planner sync set from the web", extra={"event": "web_planner_on" if on else "web_planner_off"})
    hub.publish_invalidate("settings")
    return _json(200, {"settings": dto})


def _run_label(run: IdleRun) -> str:
    summary = run.summary or {}
    if run.kind == CONSOLIDATE:
        merged = int(summary.get("merged", 0) or 0)
        contradicted = int(summary.get("contradicted", 0) or 0)
        return f"Память: объединено {merged}, противоречий {contradicted}"
    if run.kind == REFLECT:
        added = int(summary.get("added", 0) or 0)
        closed = int(summary.get("closed", 0) or 0)
        return f"Блокнот: +{added}, закрыто {closed}"
    return run.kind


async def get_digest(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    window = request.query.get("window", WINDOW_24H)
    if window not in WINDOWS:
        return _json(400, {"error": "bad_request"})
    async with sessionmaker() as session:
        digest = await build_digest(session, clock, undo_days=settings.IDLE_UNDO_DAYS, window=window)
        runs = []
        if digest.undoable_run_ids:
            result = await session.execute(select(IdleRun).where(IdleRun.id.in_(digest.undoable_run_ids)))
            by_id = {run.id: run for run in result.scalars()}
            runs = [by_id[run_id] for run_id in digest.undoable_run_ids if run_id in by_id]
    return _json(
        200,
        {
            "window": window,
            "lines": digest.text.split("\n"),
            "undoable": [
                {"id": run.id, "kind": run.kind, "label": _run_label(run), "created_at": _iso(run.created_at)}
                for run in runs
            ],
        },
    )


async def post_undo(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    run_id = path_id(request)
    if run_id is None:
        return _json(404, {"error": "not_found"})
    retry = limiter.check_panel_write()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        await checkin_core.clear_awaiting(session)
        result = await undo_run(session, settings, run_id, clock=clock)
    if result.status != STATUS_OK:
        return _json(409, {"error": "refused", "reason": result.reason})
    logger.info("idle run undone from the web", extra={"event": "web_idle_undo"})
    for topic in ("settings", "memory", "notebook"):
        hub.publish_invalidate(topic)
    return _json(200, {"restored": result.restored, "skipped_conflicts": result.skipped_conflicts})


def register(app: web.Application) -> None:
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings/notes", post_notes)
    app.router.add_post("/api/settings/planner", post_planner)
    app.router.add_get("/api/digest", get_digest)
    app.router.add_post("/api/digest/{id}/undo", post_undo)
