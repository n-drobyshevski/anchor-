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

import datetime
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import clock as clock_module
from app.core import claude_write_limits as write_limits
from app.core import grants
from app.core.clock import Clock
from app.core.clock import zone as zone_of
from app.core.report import may_report_now
from app.core.scene import Deferred
from app.core.state import get_state
from app.tg.data import STALE_TEXT, is_fresh
from app.tg.send import answer_callback, edit_keyboard
from app.tg.vault import _ru_plural
from app.vault.client import VaultClient
from app.web import claude_write, oauth, oauth_store
from app.web.claude_write import Refused, undoable_changesets

logger = logging.getLogger(__name__)

SCOPE_LABELS = ("Память", "Журнал", "Диалоги", "Состояние")
PERIOD_DAYS = (7, 30, 90)
TTL_HOURS = (1, 24)
TTL_LABELS = ("1 ч", "24 ч")

DISABLED = "Доступ для Claude выключен в настройках (CLAUDE_ACCESS_ENABLED)."
USAGE = (
    "/claude — подключение и окно для чтения\n"
    "/claude connect КОД — подтвердить подключение кодом со страницы claude.ai\n"
    "/claude disconnect — закрыть подключение\n"
    "/claude library on|off — включить или выключить библиотеку\n"
    "/claude library write on|off — включить или выключить запись в библиотеку\n"
    "/claude undo — откатить последнее изменение Claude\n"
    "/claude undo all — откатить все изменения Claude за последние 24 часа\n"
    "/claude limits — лимиты записи Claude (изменить: /claude limits КЛЮЧ ЧИСЛО)"
)
NO_CONNECTION = (
    "Нет подключения.\n\n"
    "Как подключить:\n"
    "1. claude.ai → Settings → Connectors → Add custom connector.\n"
    "2. Name: Echo, URL: {url}\n"
    "3. Нажми Connect: откроется страница с кодом.\n"
    "4. Отправь сюда: /claude connect КОД"
)
STATUS = "Подключение #{id} от {created}, до {expires}."
# C3: the library's standing switch, shown as one more status line, and
# also the answer to a no-connection `/claude library on|off` -- reuses
# NO_CONNECTION's own wording style rather than a new string.
LIBRARY_LINE_OFF = "Библиотека: выключена."
LIBRARY_LINE_READ_ONLY = "Библиотека: включена · запись выключена."
LIBRARY_LINE_READ_WRITE = "Библиотека: включена · запись включена."
LIBRARY_NO_CONNECTION = "Нет подключения. Сначала подключи Claude: /claude"
LIBRARY_USAGE = "/claude library on|off"
LIBRARY_SET_ON = "Библиотека включена. Читать её Claude может без окна, пока подключение живо."
LIBRARY_SET_OFF = "Библиотека выключена."

# W2b (plan section 5): the write switch, off by default, needs read on.
LIBRARY_WRITE_USAGE = "/claude library write on|off"
LIBRARY_WRITE_NEEDS_READ = "Сначала включи чтение: /claude library on"
LIBRARY_WRITE_SET_ON = (
    "Запись в библиотеку включена: Claude может менять заметки-знания. Откатить: /claude undo"
)
LIBRARY_WRITE_SET_OFF = "Запись в библиотеку выключена."

# Claude's write caps, tuned by hand (app/core/claude_write_limits.py).
LIMITS_HEADER = "Лимиты записи Claude:"
LIMITS_LINE = "• {label}: {value}{mark} — {key} ({min}–{max})"
LIMITS_OVERRIDDEN = " (по умолчанию {default})"
LIMITS_FOOTER = (
    "Кнопки ниже меняют на шаг. Точное число: /claude limits КЛЮЧ ЧИСЛО\n"
    "Вернуть по умолчанию: /claude limits КЛЮЧ reset · все: /claude limits reset\n"
    "Обнулить счётчики за час и день: /claude limits counters"
)
LIMITS_USAGE = "/claude limits [КЛЮЧ ЧИСЛО | КЛЮЧ reset | reset | counters]"
LIMITS_UNKNOWN_KEY = "Нет такого лимита. Список: /claude limits"
LIMITS_OUT_OF_RANGE = "{label}: допустимо от {min} до {max}."
LIMITS_SET = "{label}: теперь {value}."
LIMITS_RESET_ONE = "{label}: снова {value} (по умолчанию)."
LIMITS_RESET_ALL = "Все лимиты записи Claude вернулись к значениям по умолчанию."
LIMITS_PUSH_FAILED = " Сохранено, vault обновится позже."
LIMITS_COUNTERS_RESET = "Счётчики обнулены: Claude снова может писать в пределах лимитов."

# W2b (plan section 6): /claude undo, undo all.
UNDO_NOTHING = "Нечего откатывать."
UNDO_CAP = "Слишком много откатов за час. Попробуй позже."
UNDO_DONE = "Откатил: {restored} {noun}."
UNDO_REFUSED_SUFFIX = " Не откатил {refused}: их изменили после Claude."
UNDO_FILE_FORMS = ("файл", "файла", "файлов")
WINDOW_TEXT = (
    "{status}\n\n"
    "Окно для чтения, только чтение. Всё, что Claude прочитает, уйдёт в "
    "Anthropic и останется в том чате: закрыть можно дальнейшее чтение, но "
    "не то, что уже прочитано. Читай Echo в чате без коннекторов, которые "
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

# C3: the once-a-day library digest (connector plan section 9).
DIGEST_TEXT = "Claude за сутки: библиотека — {n} {noun}."
DIGEST_FORMS = ("запрос", "запроса", "запросов")
# A digest deferred by quiet hours/pause/quiet retries this soon --
# short enough that "the first allowed tick that day or the next"
# reads as "shortly after the block lifts", not "sometime tomorrow".
DIGEST_RETRY = datetime.timedelta(minutes=15)

# W2b (plan section 6.7): the digest's write line and its undo-all button.
DIGEST_WRITE_LINE = "Claude за сутки изменил {n} {noun}: {titles}."
DIGEST_WRITE_FORMS = ("заметку", "заметки", "заметок")
DIGEST_REFUSED_SUFFIX = " Отклонено: {k}."
# Rev. 3 (plan section 14): "новых папок: N" when Claude's writes that
# day created any folders. Pluralised with the same helper as every
# other count in this file, over the three forms the task named
# (папку/папки/папок) -- "N папку/папки/папок" agrees the way "N
# заметку/заметки/заметок" already does above, so this reads as
# ordinary Russian rather than the plain, unpluralised "Отклонено: N."
DIGEST_FOLDERS_SUFFIX = " Создал {n} {noun}."
DIGEST_FOLDER_FORMS = ("папку", "папки", "папок")
DIGEST_MAX_TITLES = 10
DIGEST_MORE = "и ещё {m}"
DIGEST_CREATED_SUFFIX = " (создана)"
DIGEST_RENAMED_SEP = " → "
DIGEST_RENAMED_SUFFIX = " (переименована)"
UNDO_ALL_BUTTON = "Откатить всё за сутки"
UNDO_STALE = "Устарело."
UNDO_DAY_DONE = "Откатил за {date}: {restored} {noun}."


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


def _library_line(connection) -> str:
    """The three-way status line (W2b: read off / read on, write off /
    read on, write on)."""
    if not connection.library_read:
        return LIBRARY_LINE_OFF
    return LIBRARY_LINE_READ_WRITE if connection.library_write else LIBRARY_LINE_READ_ONLY


async def _window_text(session, clock: Clock, connection, period: int, ttl: int) -> str:
    timezone = (await get_state(session)).timezone
    status = STATUS.format(
        id=connection.id,
        created=_local(connection.created_at, timezone),
        expires=_local(connection.expires_at, timezone),
    ) + "\n" + _library_line(connection)
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


async def library(sessionmaker, clock: Clock, word: str) -> str:
    """`/claude library on|off` (C3): the standing switch, not a window."""
    if word not in ("on", "off"):
        return LIBRARY_USAGE
    async with sessionmaker() as session:
        connection = await oauth_store.set_library(session, clock, word == "on")
    if connection is None:
        return LIBRARY_NO_CONNECTION
    return LIBRARY_SET_ON if connection.library_read else LIBRARY_SET_OFF


async def library_write(sessionmaker, clock: Clock, word: str) -> str:
    """`/claude library write on|off` (W2b, plan section 5): the write
    switch. `on` with read off is refused without touching the DB --
    read-then-refuse, so a retry after `/claude library on` just works."""
    if word not in ("on", "off"):
        return LIBRARY_WRITE_USAGE
    async with sessionmaker() as session:
        connection = await oauth_store.current_connection(session, clock)
        if connection is None:
            return LIBRARY_NO_CONNECTION
        if word == "on" and not connection.library_read:
            return LIBRARY_WRITE_NEEDS_READ
        connection = await oauth_store.set_library_write(session, clock, word == "on")
    return LIBRARY_WRITE_SET_ON if connection.library_write else LIBRARY_WRITE_SET_OFF


def _undo_reply(restored: int, refused: int) -> str:
    text = UNDO_DONE.format(restored=restored, noun=_ru_plural(restored, UNDO_FILE_FORMS))
    if refused:
        text += UNDO_REFUSED_SUFFIX.format(refused=refused)
    return text


async def undo(
    sessionmaker, settings: Settings, clock: Clock, scope: str,
    client_factory=VaultClient.from_settings,
) -> str:
    """`/claude undo` (last changeset) / `/claude undo all` (last 24h),
    plan section 6.2. Works even with the write switch off -- it only
    ever restores the user's own text -- but needs a live connection."""
    async with sessionmaker() as session:
        connection = await oauth_store.current_connection(session, clock)
        if connection is None:
            return LIBRARY_NO_CONNECTION
        since = None if scope == "last" else clock.now_utc() - datetime.timedelta(hours=24)
        limit = 1 if scope == "last" else None
        rows = await undoable_changesets(session, connection.id, since=since, limit=limit)
        if not rows:
            return UNDO_NOTHING
        client = client_factory(settings)
        restored_total = 0
        refused_total = 0
        for row in rows:
            try:
                result = await claude_write.undo_changeset(session, clock, client, connection.id, row.id)
            except Refused as exc:
                if exc.code == "cap_undos":
                    return UNDO_CAP
                continue
            restored_total += result["restored"]
            refused_total += result["refused"]
    return _undo_reply(restored_total, refused_total)


async def limits_text(sessionmaker) -> str:
    async with sessionmaker() as session:
        current = (await write_limits.effective(session)).as_dict()
    lines = [LIMITS_HEADER]
    for key, spec in write_limits.SPECS.items():
        value = current[key]
        mark = (
            LIMITS_OVERRIDDEN.format(default=write_limits.format_value(key, spec.default))
            if value != spec.default
            else ""
        )
        lines.append(
            LIMITS_LINE.format(
                label=spec.label,
                value=write_limits.format_value(key, value),
                mark=mark,
                key=key,
                min=write_limits.format_value(key, spec.min),
                max=write_limits.format_value(key, spec.max),
            )
        )
    lines.append("")
    lines.append(LIMITS_FOOTER)
    return "\n".join(lines)


# The +/- keyboard under `/claude limits`: one row per cap, each button
# carrying the value it sets (`cw:s:<key>:<value>`), never a step -- a
# doubled or stale press lands on the number the button showed, the
# same rule as the menu's intensity buttons. The middle button only
# names the cap (`cw:i:<key>`).
LIMITS_SHORT = {
    "files_per_changeset": "Файлов/пакет",
    "changesets_per_hour": "Пакетов/ч",
    "creates_per_day": "Заметок/день",
    "bytes_per_day": "КБ/день",
    "undos_per_hour": "Откатов/ч",
    "folders_per_changeset": "Папок/пакет",
    "folders_per_day": "Папок/день",
    "move_files_per_changeset": "Переносов/пакет",
    "moves_per_day": "Переносов/день",
}
LIMITS_STEP = {
    "files_per_changeset": 5,
    "changesets_per_hour": 1,
    "creates_per_day": 10,
    "bytes_per_day": 128 * 1024,
    "undos_per_hour": 1,
    "folders_per_changeset": 1,
    "folders_per_day": 5,
    "move_files_per_changeset": 5,
    "moves_per_day": 20,
}
LIMITS_RESET_BUTTON = "↺ Все по умолчанию"
LIMITS_COUNTERS_BUTTON = "🔄 Обнулить счётчики"
LIMITS_COUNTERS_DONE = "Счётчики обнулены."
LIMITS_COUNTERS_DONE_PENDING = "Счётчики обнулены, vault обновится позже."
LIMITS_SAVED = "Сохранено."
LIMITS_SAVED_PENDING = "Сохранено, vault обновится позже."
LIMITS_STALE = "Устарело."


def _short_value(key: str, value: int) -> str:
    return str(value // 1024) if key == "bytes_per_day" else str(value)


def limits_keyboard(current: write_limits.Limits) -> InlineKeyboardMarkup:
    values = current.as_dict()
    rows = []
    for key, spec in write_limits.SPECS.items():
        value, step = values[key], LIMITS_STEP[key]
        row = []
        if value > spec.min:
            low = max(spec.min, value - step)
            row.append(InlineKeyboardButton(text=f"➖ {_short_value(key, low)}", callback_data=f"cw:s:{key}:{low}"))
        row.append(
            InlineKeyboardButton(
                text=f"{LIMITS_SHORT[key]}: {_short_value(key, value)}", callback_data=f"cw:i:{key}"
            )
        )
        if value < spec.max:
            high = min(spec.max, value + step)
            row.append(InlineKeyboardButton(text=f"➕ {_short_value(key, high)}", callback_data=f"cw:s:{key}:{high}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text=LIMITS_COUNTERS_BUTTON, callback_data="cw:c")])
    if current != write_limits.DEFAULTS:
        rows.append([InlineKeyboardButton(text=LIMITS_RESET_BUTTON, callback_data="cw:r")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def handle_limits_callback(
    sessionmaker,
    settings: Settings,
    bot: Bot,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    client_factory=VaultClient.from_settings,
) -> None:
    """A press on `/claude limits`' keyboard: set (or reset) through the
    same `set_and_push` the command and the web app use, then redraw
    the message in place. A forged key or out-of-range value changes
    nothing and answers «Устарело.»."""
    if not settings.CLAUDE_ACCESS_ENABLED:
        await answer_callback(bot, callback_id, DISABLED)
        return
    parts = data.split(":")
    if parts[:2] == ["cw", "i"] and len(parts) == 3 and parts[2] in write_limits.SPECS:
        spec = write_limits.SPECS[parts[2]]
        await answer_callback(
            bot,
            callback_id,
            f"{spec.label}: {write_limits.format_value(parts[2], spec.min)}–"
            f"{write_limits.format_value(parts[2], spec.max)}",
        )
        return
    if parts == ["cw", "c"]:
        # The caps themselves do not change, so the message stays as is.
        async with sessionmaker() as session:
            pushed = await write_limits.reset_counters_and_push(session, settings, clock, client_factory)
        await answer_callback(
            bot, callback_id, LIMITS_COUNTERS_DONE_PENDING if pushed is False else LIMITS_COUNTERS_DONE
        )
        return
    if parts == ["cw", "r"]:
        key, value = "*", None
    elif parts[:2] == ["cw", "s"] and len(parts) == 4 and parts[3].isascii() and parts[3].isdigit():
        key, value = parts[2], int(parts[3])
        try:
            write_limits.check_value(key, value)
        except write_limits.LimitError:
            await answer_callback(bot, callback_id, LIMITS_STALE)
            return
    else:
        await answer_callback(bot, callback_id, LIMITS_STALE)
        return
    async with sessionmaker() as session:
        new, pushed = await write_limits.set_and_push(session, settings, clock, key, value, client_factory)
    await answer_callback(bot, callback_id, LIMITS_SAVED_PENDING if pushed is False else LIMITS_SAVED)
    await edit_keyboard(bot, chat_id, message_id, await limits_text(sessionmaker), limits_keyboard(new))


async def limits(
    sessionmaker, settings: Settings, clock: Clock, words: list[str],
    client_factory=VaultClient.from_settings,
) -> str:
    """`/claude limits [...]`: list, set, reset one, reset all, zero the
    counters. Needs no
    connection -- the caps are a standing setting, not a window."""
    if not words:
        return await limits_text(sessionmaker)
    if [word.lower() for word in words] == ["counters"]:
        async with sessionmaker() as session:
            pushed = await write_limits.reset_counters_and_push(session, settings, clock, client_factory)
        return LIMITS_COUNTERS_RESET + (LIMITS_PUSH_FAILED if pushed is False else "")
    if [word.lower() for word in words] == ["reset"]:
        async with sessionmaker() as session:
            _, pushed = await write_limits.set_and_push(
                session, settings, clock, "*", None, client_factory
            )
        return LIMITS_RESET_ALL + (LIMITS_PUSH_FAILED if pushed is False else "")
    if len(words) != 2:
        return LIMITS_USAGE
    key, raw = words[0].lower(), words[1]
    spec = write_limits.SPECS.get(key)
    if spec is None:
        return LIMITS_UNKNOWN_KEY
    value = None if raw.lower() == "reset" else write_limits.parse_value(key, raw)
    if raw.lower() != "reset" and (value is None or not spec.min <= value <= spec.max):
        return LIMITS_OUT_OF_RANGE.format(
            label=spec.label,
            min=write_limits.format_value(key, spec.min),
            max=write_limits.format_value(key, spec.max),
        )
    async with sessionmaker() as session:
        new, pushed = await write_limits.set_and_push(
            session, settings, clock, key, value, client_factory
        )
    shown = write_limits.format_value(key, getattr(new, key))
    text = (LIMITS_RESET_ONE if value is None else LIMITS_SET).format(label=spec.label, value=shown)
    return text + (LIMITS_PUSH_FAILED if pushed is False else "")


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
    if words[:2] == ["library", "write"] and len(words) == 3:
        return await library_write(sessionmaker, clock, words[2]), None
    if words[0] == "library" and len(words) == 2:
        return await library(sessionmaker, clock, words[1]), None
    if words == ["undo"]:
        return await undo(sessionmaker, settings, clock, "last"), None
    if words == ["undo", "all"]:
        return await undo(sessionmaker, settings, clock, "all"), None
    if words[0] == "limits":
        text = await limits(sessionmaker, settings, clock, words[1:])
        if len(words) > 1:
            return text, None
        async with sessionmaker() as session:
            current = await write_limits.effective(session)
        return text, limits_keyboard(current)
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


def _title(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def _changeset_markers(row, entry) -> list[str]:
    """One marker per note a write changeset touched: a plain title,
    "«X» (создана)" or "«C» → «D» (переименована)" (W2b, plan section
    6.7). Best effort from vaultd's flat per-changeset file list: the
    ledger keeps only aggregate `created`/`renamed` counts, never
    paths, so a rename is recognised by vaultd's own file ordering
    (vaultd/vaultd/knowledge.py's `perform_rename` always lists the new
    path immediately before its now-absent old path, whose exposed
    hash is null) rather than a stored role per file.
    """
    if entry is None or not entry.files:
        return []
    files = list(entry.files)
    claimed: set[int] = set()
    markers: list[str] = []
    if row.renamed:
        for i, f in enumerate(files):
            if f.sha256 is None and i > 0 and (i - 1) not in claimed:
                old_title, new_title = _title(f.path), _title(files[i - 1].path)
                markers.append(
                    f"«{old_title}»{DIGEST_RENAMED_SEP}«{new_title}»{DIGEST_RENAMED_SUFFIX}"
                )
                claimed.add(i)
                claimed.add(i - 1)
    created_left = row.created
    for i, f in enumerate(files):
        if i in claimed:
            continue
        title = _title(f.path)
        if created_left > 0:
            markers.append(f"«{title}»{DIGEST_CREATED_SUFFIX}")
            created_left -= 1
        else:
            markers.append(f"«{title}»")
    return markers


def _write_digest_text(markers: list[str], refused: int, folders: int) -> str:
    shown = markers[:DIGEST_MAX_TITLES]
    extra = len(markers) - len(shown)
    titles = ", ".join(shown)
    if extra > 0:
        titles = f"{titles}, {DIGEST_MORE.format(m=extra)}" if titles else DIGEST_MORE.format(m=extra)
    text = DIGEST_WRITE_LINE.format(
        n=len(markers), noun=_ru_plural(len(markers), DIGEST_WRITE_FORMS), titles=titles
    )
    if refused:
        text += DIGEST_REFUSED_SUFFIX.format(k=refused)
    if folders:
        text += DIGEST_FOLDERS_SUFFIX.format(n=folders, noun=_ru_plural(folders, DIGEST_FOLDER_FORMS))
    return text


def undo_all_keyboard(local_date: datetime.date, epoch: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=UNDO_ALL_BUTTON, callback_data=f"cu:{local_date.isoformat()}:{epoch}"
                )
            ]
        ]
    )


async def run_library_digest(
    session, settings: Settings, clock: Clock, bot: Bot, payload: dict,
    client_factory=VaultClient.from_settings,
) -> None:
    """The once-a-day library digest job (connector plan section 9, C3;
    W2b extends it with a write line). app/core/scheduler.py's
    `maybe_enqueue_library_digest` queues it, app/worker.py runs it.

    Content-free by construction: reads only `grants.library_read_count`
    (a date and a count) and `claude_changeset` (ids, counts, times).
    Titles are fetched from vaultd at digest time and sent to Telegram
    only -- never logged, never stored. Nothing that day at all --
    nothing is sent, and nothing is deferred: there is nothing to
    retry.

    `may_report_now` is asked here, not at enqueue time, and a "no"
    raises `Deferred` (app/core/scene.Deferred) rather than giving up --
    app/worker.py turns that into `jobs.defer_job`, which re-runs this
    same job at `DIGEST_RETRY` without spending a retry attempt, so a
    digest blocked by quiet hours or a pause goes out on the first
    allowed tick afterwards, that day or (if the block outlives
    midnight) early the next, rather than being silently dropped.
    """
    local_date = datetime.date.fromisoformat(payload["local_date"])
    read_count = await grants.library_read_count(session, local_date)
    state = await get_state(session)
    day_start = clock_module.combine_local(local_date, datetime.time(0, 0), state.timezone)
    day_end = clock_module.combine_local(
        local_date + datetime.timedelta(days=1), datetime.time(0, 0), state.timezone
    )
    write_rows = await claude_write.changesets_between(session, day_start, day_end)
    if read_count == 0 and not write_rows:
        return
    if not may_report_now(settings, clock, state):
        raise Deferred(clock.now_utc() + DIGEST_RETRY)

    lines = []
    if read_count:
        lines.append(DIGEST_TEXT.format(n=read_count, noun=_ru_plural(read_count, DIGEST_FORMS)))
    keyboard = None
    if write_rows:
        client = client_factory(settings)
        try:
            vault_index = {c.id: c for c in await client.list_changes()}
        except Exception:  # noqa: BLE001 - an unreachable vault must not drop the read line
            vault_index = {}
        markers: list[str] = []
        refused_total = 0
        folders_total = 0
        for row in write_rows:
            markers.extend(_changeset_markers(row, vault_index.get(row.vault_ref)))
            refused_total += row.refused
            folders_total += row.folders
        if markers:
            lines.append(_write_digest_text(markers, refused_total, folders_total))
            keyboard = undo_all_keyboard(local_date, state.vault_epoch)
    if not lines:
        return
    await bot.send_message(chat_id=state.chat_id, text=" ".join(lines), reply_markup=keyboard)


async def handle_undo_callback(
    sessionmaker,
    settings: Settings,
    bot: Bot,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    client_factory=VaultClient.from_settings,
) -> None:
    """`cu:<YYYY-MM-DD>:<vault_epoch>` -- the digest's own [Откатить
    всё за сутки] (W2b, plan section 6.7). Stale date, stale epoch and
    a replay (every write changeset for that date already undone) all
    answer «Устарело.» and change nothing; app/web/ingress.py's
    BLOCKED_CALLBACK_PREFIX and the router's own is_web_sink guard both
    refuse a web-sink press before this is ever called.
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "cu":
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, UNDO_STALE, None)
        return
    try:
        local_date = datetime.date.fromisoformat(parts[1])
    except ValueError:
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, UNDO_STALE, None)
        return
    epoch = parts[2]

    async with sessionmaker() as session:
        state = await get_state(session)
        connection = await oauth_store.current_connection(session, clock)
        if epoch != state.vault_epoch or connection is None:
            await answer_callback(bot, callback_id)
            await edit_keyboard(bot, chat_id, message_id, UNDO_STALE, None)
            return
        timezone = state.timezone
        day_start = clock_module.combine_local(local_date, datetime.time(0, 0), timezone)
        day_end = clock_module.combine_local(
            local_date + datetime.timedelta(days=1), datetime.time(0, 0), timezone
        )
        rows = [
            row
            for row in await claude_write.changesets_between(session, day_start, day_end)
            if row.undone_at is None
        ]
        if not rows:
            await answer_callback(bot, callback_id)
            await edit_keyboard(bot, chat_id, message_id, UNDO_STALE, None)
            return
        client = client_factory(settings)
        restored_total = 0
        for row in sorted(rows, key=lambda r: r.created_at, reverse=True):
            try:
                result = await claude_write.undo_changeset(
                    session, clock, client, connection.id, row.id
                )
            except Refused:
                continue
            restored_total += result["restored"]
    await answer_callback(bot, callback_id)
    text = UNDO_DAY_DONE.format(
        date=local_date.isoformat(), restored=restored_total, noun=_ru_plural(restored_total, UNDO_FILE_FORMS)
    )
    await edit_keyboard(bot, chat_id, message_id, text, None)
