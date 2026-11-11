"""`POST /planner/board-sync`: the planner's "Sync with Obsidian" button.

The planner's MCP app (its `sync_boards` tool) forwards the signed-in
planner user's own Supabase access token here; Echo runs one board-sync
pass now (app/planner/board_job.py, trigger `planner`) and answers with
its counts. No shared secret exists between the two apps (docs/decisions.md,
"Board sync" -> "Manual pass from the planner"):

1. Gates: `board_job.gates_open` (BOARD_SYNC_ENABLED, PLANNER_ENABLED,
   a VAULT_MODE other than `off`), PLANNER_SUPABASE_ANON_KEY set and the
   planner link active. Any of them shut: aiohttp's plain 404.
2. `Authorization: Bearer <token>`, a JWT-shaped string, else 401. Then
   an in-memory limit (RATE_PER_MINUTE, all callers together: there is
   one user), else 429.
3. Supabase's own `GET /auth/v1/user` with that token and the project's
   public anon key: anything but a 200 with an `id` is a 401. Supabase,
   not Echo, checks the signature and the expiry.
4. That `id` must be the planner account Echo is linked to: the `sub` of
   Echo's own stored planner access token, read from the row as it is
   (Echo's token, so the payload is decoded without verification; never
   refreshed here). Unreadable: 503. Another account: 403.
5. Queue the pass (deduplicated per minute, as `/boards now`) and wait up
   to WAIT_SECONDS for the job to finish: 200 `done` with the outcome and
   per-board counts (never a board's name), 202 `queued` when it is still
   running, 200 `already_queued` when a pass from the button is already
   queued this minute. The worker still sends its usual Telegram reply,
   so every pass the button runs leaves a record there.

Nothing here logs a token, a user id or a board name: event names only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime
import hmac
import json
import logging
import re
import time
from typing import NoReturn

import aiohttp
from aiohttp import web
from sqlalchemy import func, select

from app.config import Settings
from app.core.clock import Clock
from app.db.models import BoardSyncRun, Job, PlannerCredential
from app.planner import auth as planner_auth
from app.planner import board_job
from app.planner.board_sync import Counts
from app.web.mcp_core import RateLimiter

logger = logging.getLogger(__name__)

PATH = "/planner/board-sync"
LIMITER_KEY = "board_sync_trigger_limiter"
RATE_PER_MINUTE = 6
VERIFY_TIMEOUT = aiohttp.ClientTimeout(total=5)
WAIT_SECONDS = 20.0
POLL_SECONDS = 1.0

_BEARER_RE = re.compile(r"^Bearer ([A-Za-z0-9._-]{20,4096})$")
_FINISHED = ("done", "failed")
_COUNT_FIELDS = tuple(Counts.__dataclass_fields__)


def register(app: web.Application) -> None:
    """Registered whatever the settings, like the OAuth callback: the
    handler 404s itself, so the route's existence says nothing."""
    app[LIMITER_KEY] = RateLimiter(RATE_PER_MINUTE)
    app.router.add_post(PATH, handle)


def _log(event: str) -> None:
    logger.info("board sync trigger", extra={"event": f"board_sync_trigger_{event}"})


def _not_found() -> NoReturn:
    # The same answer aiohttp gives a route that does not exist.
    raise web.HTTPNotFound()


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"error": code}, status=status)


def _gates_open(settings: Settings) -> bool:
    return board_job.gates_open(settings) and bool(settings.PLANNER_SUPABASE_ANON_KEY)


def bearer_token(header: str | None) -> str | None:
    match = _BEARER_RE.match(header or "")
    return match.group(1) if match else None


def token_subject(token: str) -> str | None:
    """The `sub` claim of a JWT's payload, unverified; None if unreadable."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1]
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        claims = json.loads(raw)
    except (binascii.Error, ValueError):
        return None
    sub = claims.get("sub") if isinstance(claims, dict) else None
    return sub if isinstance(sub, str) and sub else None


async def verify_user(settings: Settings, token: str) -> str | None:
    """Supabase's `id` for this token, or None when Supabase refuses it.
    Raises aiohttp.ClientError / TimeoutError when unreachable."""
    url = f"{settings.PLANNER_SUPABASE_URL.rstrip('/')}/auth/v1/user"
    headers = {"Authorization": f"Bearer {token}", "apikey": settings.PLANNER_SUPABASE_ANON_KEY}
    async with aiohttp.ClientSession(timeout=VERIFY_TIMEOUT) as http, http.get(url, headers=headers) as resp:
        if resp.status != 200:
            return None
        try:
            body = await resp.json(content_type=None)
        except ValueError:
            return None
    user_id = body.get("id") if isinstance(body, dict) else None
    return user_id if isinstance(user_id, str) and user_id else None


async def _linked_subject(session) -> str | None:
    row = await session.get(PlannerCredential, planner_auth.CREDENTIAL_ID)
    if row is None or not row.access_token:
        return None
    return token_subject(row.access_token)


def _board_json(run: BoardSyncRun) -> dict:
    return {"outcome": run.outcome, **{name: getattr(run, name) for name in _COUNT_FIELDS}}


async def _result(session, started: datetime.datetime) -> dict:
    """The rows of the newest pass that started at or after `started`.
    One worker runs one job at a time, so once our job is finished the
    newest pass is ours (an automatic pass claimed just before it is older)."""
    newest = (
        await session.execute(select(func.max(BoardSyncRun.at)).where(BoardSyncRun.at >= started))
    ).scalar_one_or_none()
    if newest is None:
        return {"status": "done", "outcome": board_job.OK, "boards": []}
    runs = (
        (
            await session.execute(
                select(BoardSyncRun).where(BoardSyncRun.at == newest).order_by(BoardSyncRun.id)
            )
        )
        .scalars()
        .all()
    )
    whole = next((run for run in runs if run.board is None), None)
    return {
        "status": "done",
        "outcome": whole.outcome if whole is not None else board_job.OK,
        "boards": [_board_json(run) for run in runs if run.board is not None],
    }


async def _wait(sessionmaker, dedup_key: str, started: datetime.datetime) -> dict | None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        async with sessionmaker() as session:
            status = (
                await session.execute(select(Job.status).where(Job.dedup_key == dedup_key))
            ).scalar_one_or_none()
            if status == "failed":
                return {"status": "failed"}
            if status == "done":
                return await _result(session, started)
    return None


async def handle(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    if not _gates_open(settings):
        _not_found()
    sessionmaker = request.app["sessionmaker"]
    async with sessionmaker() as session:
        if not await planner_auth.is_enabled(session):
            _not_found()

    token = bearer_token(request.headers.get("Authorization"))
    if token is None:
        _log("bad_header")
        return _error(401, "unauthorized")

    limiter: RateLimiter = request.app[LIMITER_KEY]
    if not limiter.allow(0):
        _log("rate_limited")
        return _error(429, "rate_limited")

    try:
        user_id = await verify_user(settings, token)
    except (aiohttp.ClientError, TimeoutError):
        _log("verify_unavailable")
        return _error(503, "verify_unavailable")
    if user_id is None:
        _log("rejected")
        return _error(401, "unauthorized")

    clock: Clock = request.app["clock"]
    async with sessionmaker() as session:
        linked = await _linked_subject(session)
        if linked is None:
            _log("no_linked_account")
            return _error(503, "no_linked_account")
        if not hmac.compare_digest(user_id.encode(), linked.encode()):
            _log("other_account")
            return _error(403, "other_account")

        started = clock.now_utc()
        dedup_key = board_job.command_dedup_key(started, board_job.TRIGGER_PLANNER)
        enqueued = await board_job.enqueue_command(session, clock, board_job.TRIGGER_PLANNER, now=started)
    if not enqueued:
        _log("already_queued")
        return web.json_response({"status": "already_queued"})
    _log("queued")

    result = await _wait(sessionmaker, dedup_key, started)
    if result is None:
        _log("still_running")
        return web.json_response({"status": "queued"}, status=202)
    _log(result["status"])
    return web.json_response(result)
