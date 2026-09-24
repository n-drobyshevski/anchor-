"""/grok and /revoke: the user's switch for outside-assistant access.

/grok sends one message with a keyboard: four scope toggles, a
look-back for dialogs, a lifetime, and [Разрешить]/[Отмена]. All
scopes start off. The whole choice rides in the callback data --
`g:<action>:<mask>:<period>:<ttl>:<issued_at>` -- so nothing is written
until [Разрешить], and a button older than CONFIRM_TTL is stale, the
same rule /delete's confirmation uses (app/tg/data.py).

[Разрешить] creates the grant (app/core/grants.py) and edits the same
message into the capability URL. That message is the only place the
token ever appears; it is not stored as a `message` row, so it is not
in /export and not readable through the dialogs scope.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import grants
from app.core.clock import Clock
from app.core.clock import zone as zone_of
from app.core.state import get_state
from app.tg.data import STALE_TEXT, is_fresh
from app.tg.send import answer_callback, edit_keyboard

logger = logging.getLogger(__name__)

SCOPE_LABELS = ("Память", "Журнал", "Диалоги", "Состояние")
PERIOD_DAYS = (7, 30, 90)
TTL_HOURS = (1, 24, 168)
TTL_LABELS = ("1 ч", "24 ч", "7 дн.")

DISABLED = "Доступ для Grok выключен в настройках (GROK_ACCESS_ENABLED)."
WEBHOOK_ONLY = "Доступ для Grok работает только в режиме webhook с публичным адресом."
GRANT_TEXT = (
    "Доступ для Grok, только чтение.\n\n"
    "Выбери, что открыть. Всё, что Grok прочитает, уйдёт в xAI и останется "
    "в том чате: закрыть можно доступ дальше, но не то, что уже прочитано.\n\n"
    "Диалоги: за {days} дн. · Срок доступа: {ttl}"
)
ACTIVE_PREFIX = "Сейчас открыто: {grants}. Закрыть всё: /revoke\n\n"
PICK_SOMETHING = "Выбери хотя бы что-то."
ALLOW = "Разрешить"
CANCEL = "Отмена"
CANCELLED_TEXT = "Отменено. Ничего не открыто."
GRANTED_TEXT = (
    "Открыто до {until}: {scopes}.\n\n"
    "Ссылка (показываю один раз, никому не пересылай):\n{url}\n\n"
    "grok.com → Connectors → New Connector → Custom → вставь ссылку. "
    "После подключения удали это сообщение.\n"
    "Каждое чтение я покажу здесь. Закрыть доступ: /revoke"
)
REVOKED_TEXT = (
    "Доступ для Grok закрыт ({count}). Коннектор в grok.com можно удалить: "
    "ссылка больше не работает."
)
NOTHING_TO_REVOKE = "Открытых доступов нет."


def _mask_scopes(mask: int) -> list[str]:
    return [scope for i, scope in enumerate(grants.SCOPES) if mask & (1 << i)]


def _scope_names(scopes) -> str:
    names = dict(zip(grants.SCOPES, SCOPE_LABELS))
    return ", ".join(names[s].lower() for s in scopes)


def grant_text(period: int, ttl: int) -> str:
    return GRANT_TEXT.format(days=PERIOD_DAYS[period], ttl=TTL_LABELS[ttl])


def grant_keyboard(mask: int, period: int, ttl: int, issued_at: int) -> InlineKeyboardMarkup:
    def data(action: str) -> str:
        return f"g:{action}:{mask}:{period}:{ttl}:{issued_at}"

    toggles = [
        InlineKeyboardButton(
            text=("✅ " if mask & (1 << i) else "▫️ ") + label, callback_data=data(f"t{i}")
        )
        for i, label in enumerate(SCOPE_LABELS)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            toggles[:2],
            toggles[2:],
            [
                InlineKeyboardButton(
                    text=f"Диалоги: {PERIOD_DAYS[period]} дн.", callback_data=data("p")
                ),
                InlineKeyboardButton(text=f"Срок: {TTL_LABELS[ttl]}", callback_data=data("l")),
            ],
            [
                InlineKeyboardButton(text=ALLOW, callback_data=data("ok")),
                InlineKeyboardButton(text=CANCEL, callback_data=data("no")),
            ],
        ]
    )


def available(settings: Settings) -> str | None:
    """None if /grok may run, else the reason to show the user."""
    if not settings.GROK_ACCESS_ENABLED:
        return DISABLED
    if settings.MODE != "webhook" or not settings.PUBLIC_URL:
        return WEBHOOK_ONLY
    return None


async def opening_text(sessionmaker, clock: Clock, period: int, ttl: int) -> str:
    async with sessionmaker() as session:
        active = await grants.list_active(session, clock)
        timezone = (await get_state(session)).timezone
    text = grant_text(period, ttl)
    if active:
        tz = zone_of(timezone)
        listed = "; ".join(
            f"{_scope_names(g.scopes)} до {g.expires_at.astimezone(tz):%d.%m %H:%M}"
            for g in active
        )
        text = ACTIVE_PREFIX.format(grants=listed) + text
    return text


def mcp_url(settings: Settings, token: str) -> str:
    return f"{settings.PUBLIC_URL.rstrip('/')}/mcp/{token}"


async def handle_callback(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    parts = data.split(":")
    try:
        _, action, mask_s, period_s, ttl_s, issued_s = parts
        mask, period, ttl, issued_at = int(mask_s), int(period_s), int(ttl_s), int(issued_s)
        if not (0 <= mask < 16 and 0 <= period < len(PERIOD_DAYS) and 0 <= ttl < len(TTL_HOURS)):
            raise ValueError
    except ValueError:
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, STALE_TEXT, None)
        return

    if available(settings) is not None or not is_fresh(
        issued_at, now=clock.now_utc().timestamp()
    ):
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, STALE_TEXT, None)
        return

    if action == "no":
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, CANCELLED_TEXT, None)
        return

    if action == "ok":
        scopes = _mask_scopes(mask)
        if not scopes:
            await answer_callback(bot, callback_id, PICK_SOMETHING)
            return
        await answer_callback(bot, callback_id)
        async with sessionmaker() as session:
            token, grant = await grants.create_grant(
                session,
                clock,
                scopes=scopes,
                ttl_hours=TTL_HOURS[ttl],
                dialog_days=PERIOD_DAYS[period],
                max_hours=settings.GROK_GRANT_MAX_HOURS,
            )
            timezone = (await get_state(session)).timezone
        until = grant.expires_at.astimezone(zone_of(timezone))
        logger.info("grant created", extra={"event": "grant", "grant_id": grant.id})
        await edit_keyboard(
            bot,
            chat_id,
            message_id,
            GRANTED_TEXT.format(
                until=f"{until:%d.%m %H:%M}",
                scopes=_scope_names(grant.scopes),
                url=mcp_url(settings, token),
            ),
            None,
        )
        return

    await answer_callback(bot, callback_id)
    if action.startswith("t") and action[1:].isdigit() and int(action[1:]) < len(SCOPE_LABELS):
        mask ^= 1 << int(action[1:])
    elif action == "p":
        period = (period + 1) % len(PERIOD_DAYS)
    elif action == "l":
        ttl = (ttl + 1) % len(TTL_HOURS)
    else:
        return
    await edit_keyboard(
        bot,
        chat_id,
        message_id,
        await opening_text(sessionmaker, clock, period, ttl),
        grant_keyboard(mask, period, ttl, issued_at),
    )


async def revoke(sessionmaker, clock: Clock) -> str:
    async with sessionmaker() as session:
        count = await grants.revoke_all(session, clock)
    logger.info("grants revoked", extra={"event": "revoke", "count": count})
    return REVOKED_TEXT.format(count=count) if count else NOTHING_TO_REVOKE
