"""The aiohttp webhook route: verify, filter, enqueue, 200. Nothing slow.

is_allowed() is also the function polling.py calls (plan section 6.2):
one code path decides which updates get stored, no matter the transport.
"""

from __future__ import annotations

import hmac
import logging

from aiohttp import web
from sqlalchemy import text

from app.config import Settings
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


async def readyz(request: web.Request) -> web.Response:
    sessionmaker = request.app["sessionmaker"]
    try:
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return web.Response(status=503, text="not ready")
    return web.Response(status=200, text="ok")
