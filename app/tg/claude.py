"""/claude: the connection's status, its windows, and typed-code approval.

Connector plan section 6.1 (docs/claude-connector.md). Three commands:

- `/claude` starts with the connection's status. With a connection, it
  offers the same picker as /grok: four scopes (all off), a look-back
  for dialogs, a lifetime of 1 h or 24 h, [Открыть]/[Отмена]. The whole
  choice rides in the callback data,
  `cl:<action>:<mask>:<period>:<ttl>:<issued_at>`, and a button older
  than CONFIRM_TTL is stale (app/tg/data.py). [Открыть] opens a window
  (app/core/grants.py), closing any other; the connection is checked
  again at that moment.
- `/claude connect <code>` approves the one pending authorize request
  whose waiting page shows that code. **This is the only way a
  connection is ever approved**: the code travels from the user's
  screen into Telegram by hand, never the other way, so a stranger's
  authorize request can never be approved by a careless tap. Five
  wrong codes in an hour lock the command for an hour.
- `/claude disconnect` revokes the connection, its tokens and windows.

All three are Telegram-only (the router's `is_web_sink` guards and
app/web/ingress.py), like /grok. Credential state is written only by
app/web/oauth_store.py; this module asks it.
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
from app.web import oauth, oauth_store

logger = logging.getLogger(__name__)

SCOPE_LABELS = ("Память", "Журнал", "Диалоги", "Состояние")
PERIOD_DAYS = (7, 30, 90)
TTL_HOURS = (1, 24)
TTL_LABELS = ("1 ч", "24 ч")

DISABLED = "Доступ для Claude выключен в настройках (CLAUDE_ACCESS_ENABLED)."
USAGE = (
    "/claude — подключение и окно для чтения\n"
    "/claude connect КОД — подтвердить подключение кодом со страницы claude.ai\n"
    "/claude disconnect — закрыть подключение"
)
NO_CONNECTION = (
    "Нет подключения.\n\n"
    "Как подключить:\n"
    "1. claude.ai → Settings → Connectors → Add custom connector.\n"
    "2. Name: Anchor, URL: {url}\n"
    "3. Нажми Connect: откроется страница с кодом.\n"
    "4. Отправь сюда: /claude connect КОД"
)
STATUS = "Подключение #{id} от {created}, до {expires}."
WINDOW_TEXT = (
    "{status}\n\n"
    "Окно для чтения, только чтение. Всё, что Claude прочитает, уйдёт в "
    "Anthropic и останется в том чате: закрыть можно дальнейшее чтение, но "
    "не то, что уже прочитано. Читай Anchor в чате без коннекторов, которые "
    "умеют писать.\n\n"
    "Диалоги: за {days} дн. · Окно: {ttl}"
)
OPEN_PREFIX = "Сейчас открыто окно: {scopes} до {until}. Новое окно закроет его.\n\n"
OPEN = "Открыть"
CANCEL = "Отмена"
PICK_SOMETHING = "Выбери хотя бы что-то."
CANCELLED_TEXT = "Отменено. Окно не открыто."
OPENED_TEXT = (
    "Окно открыто до {until}: {scopes}.\n"
    "Каждое чтение я покажу здесь. Закрыть: /revoke"
)
GONE_TEXT = "Подключения больше нет. /claude"
CODE_UNKNOWN = "Код не найден или устарел."
LOCKED = "Слишком много неверных кодов. /claude connect снова заработает через час."
APPROVED = (
    "Подтверждено. Вернись в браузер: claude.ai завершит подключение сам, а старое "
    "подключение (если было) закроется. Читать Claude сможет только в окне: /claude"
)
DISCONNECTED = "Подключение закрыто. Коннектор в claude.ai можно удалить."
NOTHING_CONNECTED = "Подключения нет."


def available(settings: Settings) -> str | None:
    """None if /claude may run, else the reason to show the user."""
    return None if settings.CLAUDE_ACCESS_ENABLED else DISABLED


def _mask_scopes(mask: int) -> list[str]:
    return [scope for i, scope in enumerate(grants.SCOPES) if mask & (1 << i)]


def _scope_names(scopes) -> str:
    names = dict(zip(grants.SCOPES, SCOPE_LABELS))
    return ", ".join(names[s].lower() for s in scopes)


def _ttl_choices(settings: Settings) -> tuple[int, ...]:
    return tuple(h for h in TTL_HOURS if h <= settings.CLAUDE_WINDOW_MAX_HOURS) or (1,)


def window_keyboard(mask: int, period: int, ttl: int, issued_at: int) -> InlineKeyboardMarkup:
    def data(action: str) -> str:
        return f"cl:{action}:{mask}:{period}:{ttl}:{issued_at}"

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
                InlineKeyboardButton(text=f"Окно: {TTL_LABELS[ttl]}", callback_data=data("l")),
            ],
            [
                InlineKeyboardButton(text=OPEN, callback_data=data("ok")),
                InlineKeyboardButton(text=CANCEL, callback_data=data("no")),
            ],
        ]
    )


def _local(moment, timezone: str) -> str:
    return f"{moment.astimezone(zone_of(timezone)):%d.%m}"


async def _window_text(session, clock: Clock, connection, period: int, ttl: int) -> str:
    timezone = (await get_state(session)).timezone
    status = STATUS.format(
        id=connection.id,
        created=_local(connection.created_at, timezone),
        expires=_local(connection.expires_at, timezone),
    )
    text = WINDOW_TEXT.format(status=status, days=PERIOD_DAYS[period], ttl=TTL_LABELS[ttl])
    window = await grants.find_open_window(session, clock, connection.id)
    if window is not None:
        until = f"{window.expires_at.astimezone(zone_of(timezone)):%d.%m %H:%M}"
        text = OPEN_PREFIX.format(scopes=_scope_names(window.scopes), until=until) + text
    return text


async def status(
    sessionmaker, settings: Settings, clock: Clock
) -> tuple[str, InlineKeyboardMarkup | None]:
    """`/claude` with no arguments: status, then the picker if connected."""
    async with sessionmaker() as session:
        connection = await oauth_store.current_connection(session, clock)
        if connection is None:
            return NO_CONNECTION.format(url=oauth.resource(settings)), None
        text = await _window_text(session, clock, connection, 0, 0)
    return text, window_keyboard(0, 0, 0, int(clock.now_utc().timestamp()))


async def connect(sessionmaker, clock: Clock, pending: oauth_store.PendingStore | None, code: str) -> str:
    """`/claude connect <code>`: approve exactly the request showing it."""
    if pending is None:
        return DISABLED
    if pending.locked():
        return LOCKED
    entry = pending.match(code)
    if entry is None:
        started = pending.record_failure()
        logger.info("claude connect refused", extra={"event": "claude_connect", "reason": "code"})
        return LOCKED if started else CODE_UNKNOWN
    async with sessionmaker() as session:
        await oauth_store.approve(session, clock, entry)
    return APPROVED


async def disconnect(sessionmaker, clock: Clock) -> str:
    async with sessionmaker() as session:
        count = await oauth_store.disconnect(session, clock)
    return DISCONNECTED if count else NOTHING_CONNECTED


async def command(
    sessionmaker,
    settings: Settings,
    clock: Clock,
    pending: oauth_store.PendingStore | None,
    args: str | None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    words = (args or "").split()
    if not words:
        return await status(sessionmaker, settings, clock)
    if words[0] == "connect" and len(words) == 2:
        return await connect(sessionmaker, clock, pending, words[1]), None
    if words == ["disconnect"]:
        return await disconnect(sessionmaker, clock), None
    return USAGE, None


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
    ttl_choices = _ttl_choices(settings)
    try:
        _, action, mask_s, period_s, ttl_s, issued_s = data.split(":")
        mask, period, ttl, issued_at = int(mask_s), int(period_s), int(ttl_s), int(issued_s)
        if not (0 <= mask < 16 and 0 <= period < len(PERIOD_DAYS) and 0 <= ttl < len(ttl_choices)):
            raise ValueError
    except ValueError:
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, STALE_TEXT, None)
        return

    if available(settings) is not None or not is_fresh(issued_at, now=clock.now_utc().timestamp()):
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, STALE_TEXT, None)
        return

    if action == "no":
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, CANCELLED_TEXT, None)
        return

    async with sessionmaker() as session:
        connection = await oauth_store.current_connection(session, clock)
        if connection is None:
            await answer_callback(bot, callback_id)
            await edit_keyboard(bot, chat_id, message_id, GONE_TEXT, None)
            return

        if action == "ok":
            scopes = _mask_scopes(mask)
            if not scopes:
                await answer_callback(bot, callback_id, PICK_SOMETHING)
                return
            await answer_callback(bot, callback_id)
            window = await grants.open_window(
                session,
                clock,
                connection_id=connection.id,
                scopes=scopes,
                ttl_hours=ttl_choices[ttl],
                dialog_days=PERIOD_DAYS[period],
                max_hours=settings.CLAUDE_WINDOW_MAX_HOURS,
            )
            timezone = (await get_state(session)).timezone
            logger.info(
                "claude window opened",
                extra={"event": "claude_window", "grant_id": window.id, "connection_id": connection.id},
            )
            until = f"{window.expires_at.astimezone(zone_of(timezone)):%d.%m %H:%M}"
            await edit_keyboard(
                bot,
                chat_id,
                message_id,
                OPENED_TEXT.format(until=until, scopes=_scope_names(window.scopes)),
                None,
            )
            return

        await answer_callback(bot, callback_id)
        if action.startswith("t") and action[1:].isdigit() and int(action[1:]) < len(SCOPE_LABELS):
            mask ^= 1 << int(action[1:])
        elif action == "p":
            period = (period + 1) % len(PERIOD_DAYS)
        elif action == "l":
            ttl = (ttl + 1) % len(ttl_choices)
        else:
            return
        text = await _window_text(session, clock, connection, period, ttl)
    await edit_keyboard(bot, chat_id, message_id, text, window_keyboard(mask, period, ttl, issued_at))
