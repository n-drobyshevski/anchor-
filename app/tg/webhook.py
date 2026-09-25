"""The aiohttp webhook route: verify, filter, enqueue, 200. Nothing slow.

is_allowed() is also the function polling.py calls (plan section 6.2):
one code path decides which updates get stored, no matter the transport.
"""

from __future__ import annotations

import datetime
import hmac
import logging

from aiohttp import web
from sqlalchemy import select, text

from app.config import Settings
from app.core.clock import Clock, SystemClock
from app.db.models import HeartbeatState
from app.db.queue import enqueue

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def verify_secret(request: web.Request, settings: Settings) -> bool:
    """Compare the request's secret header against the configured one.

    Uses hmac.compare_digest for a constant-time comparison.
    """
    provided = request.headers.get(SECRET_HEADER, "")
    return hmac.compare_digest(provided, settings.TELEGRAM_SECRET_TOKEN)


def extract_chat(payload: dict) -> tuple[int, str] | None:
    """Read chat id/type from message, falling back to callback_query.message."""
    message = payload.get("message") or (payload.get("callback_query") or {}).get("message")
    if not message:
        return None
    chat = message.get("chat")
    if not chat or "id" not in chat or "type" not in chat:
        return None
    return chat["id"], chat["type"]


def is_allowed(chat_id: int, chat_type: str, settings: Settings) -> bool:
    return chat_type == "private" and chat_id == settings.ALLOWED_CHAT_ID


async def filter_and_enqueue(session, payload: dict, settings: Settings) -> bool:
    """Apply the allowlist and, if it passes, enqueue. Returns True if stored.

    Shared by the webhook route and the polling loop (plan section 6.2).
    """
    update_id = payload.get("update_id")
    if update_id is None:
        return False

    chat = extract_chat(payload)
    if chat is None:
        return False
    chat_id, chat_type = chat
    if not is_allowed(chat_id, chat_type, settings):
        return False

    inserted = await enqueue(session, update_id, payload)
    logger.info(
        "update enqueued" if inserted else "update deduplicated",
        extra={"update_id": update_id},
    )
    return inserted


async def handle_webhook(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    sessionmaker = request.app["sessionmaker"]

    if not verify_secret(request, settings):
        return web.Response(status=403)

    try:
        payload = await request.json()
    except Exception:
        # Malformed JSON is not a payload we can act on. Ack it so Telegram
        # stops retrying, but leave a trace: a silent drop here would be
        # indistinguishable from a healthy no-op. The body is never logged.
        logger.warning("malformed webhook body", extra={"event": "malformed_body"})
        return web.Response(status=200)

    async with sessionmaker() as session:
        await filter_and_enqueue(session, payload, settings)

    return web.Response(status=200)


async def healthz(request: web.Request) -> web.Response:
    return web.Response(status=200, text="ok")


def heartbeat_stale(
    heartbeat_at: datetime.datetime | None, now: datetime.datetime, stale_after_min: int
) -> bool:
    """True iff the heartbeat has not run within `stale_after_min` minutes
    (Phase 6 plan section 9.6; milestone 6e).

    `heartbeat_at is None` (the heartbeat loop has never stamped
    `heartbeat_state` at all -- a fresh deploy, before its first tick)
    also counts as stale: /readyz's whole job is gating *readiness*, and
    a service that has never run its heartbeat is not ready, full stop.
    This is deliberately simpler than app/worker.py's `watchdog_is_stale`,
    which adds a startup grace before it will actively kill the process
    -- /readyz only ever reports a status, so there is nothing to guard
    against overreacting to.
    """
    if heartbeat_at is None:
        return True
    return now - heartbeat_at > datetime.timedelta(minutes=stale_after_min)


async def readyz(request: web.Request) -> web.Response:
    sessionmaker = request.app["sessionmaker"]
    settings: Settings = request.app["settings"]
    clock: Clock = request.app.get("clock") or SystemClock()
    try:
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
            result = await session.execute(
                select(HeartbeatState.heartbeat_at).where(HeartbeatState.id == 1)
            )
            heartbeat_at = result.scalar_one_or_none()
    except Exception:
        return web.Response(status=503, text="not ready")
    if heartbeat_stale(heartbeat_at, clock.now_utc(), settings.LIVENESS_STALE_MIN):
        return web.Response(status=503, text="heartbeat stale")
    return web.Response(status=200, text="ok")
