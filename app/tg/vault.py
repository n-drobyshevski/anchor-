"""`/vault` and the `/state` line (phase-8 plan section 8).

The plan's `/vault` has two parts: a first line about the sync, and a
list of files that need attention. The list is 8c's (quarantines and
holds come with ingest). 8b adds the fact count to the first line in
mirror, and says outright that edits in the vault are not applied yet.

8e adds a line about notes: off until `/vault notes on`, and then how
many notes of each class vaultd lists and how many need a look. Counts
only: vaultd never tells the bot the name of a note it may not see, so
there is nothing else to show.

Every line here is a reply to a command, so it is sent whatever the
pause, quiet or welfare state; `may_report_now` governs unsolicited
vault messages (8c), not this.
"""

from __future__ import annotations

import datetime
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.report import may_report_now
from app.core.state import get_state
from app.tg.send import answer_callback, edit_keyboard, send_keyboard
from app.vault import errors as vault_errors
from app.vault import holds as vault_holds
from app.vault import status as vault_status
from app.vault.epoch import EPOCH_RE

logger = logging.getLogger(__name__)

OFF_REPLY = "Хранилище выключено."
OK_LINE = "Хранилище: синхронизация ок (работает с {since}, перезапусков {restarts})."
STOPPED_LINE = "Хранилище: синхронизация остановлена (перезапусков {restarts}, код выхода {code})."
UNREACHABLE_LINE = "Хранилище: нет связи с сервисом хранилища."
UNREACHABLE_SINCE = " Последний ответ — {when}."
UNAUTHORIZED_LINE = (
    "Хранилище: сервис отказал в доступе — VAULT_API_TOKEN на боте и на сервисе хранилища различается."
)
FACTS_SUFFIX = " · фактов {count}"
MIRROR_NOTE = (
    "Правки в хранилище пока не применяются: следующее изменение факта в Anchor перезапишет файл."
)

STATE_OFF = "Хранилище: выключено"
STATE_OK = "Хранилище: ок"
STATE_STOPPED = "Хранилище: синхронизация остановлена"
STATE_UNREACHABLE = "Хранилище: нет связи"
STATE_UNREACHABLE_SINCE = "Хранилище: нет связи с {when}"
STATE_UNAUTHORIZED = "Хранилище: нет доступа (токен)"
STATE_PURGE_PENDING = "Хранилище: удаление файлов ожидает"

# 8e (8e plan section 6). The plan's single «не прочитано» is split so
# that each count sits under a label that is true: a conflicting note
# and a legacy `anchor: read` note are both read (as personal), and only
# an unknown value is not read at all (docs/decisions.md).
NOTES_OFF_LINE = "Заметки: выключены — /vault notes on"
NOTES_SETTINGS_INVALID_LINE = "Заметки: Anchor/settings.md с ошибкой — ни одна заметка не читается"
NOTES_COUNTS = "Заметки: личные {personal} · знания {knowledge}"
NOTES_CHECK = " · проверить: {items}"
NOTES_CHECK_CONFLICT = "конфликт {n}"
NOTES_CHECK_LEGACY = "anchor: read {n}"
NOTES_UNREAD = " · не прочитано: неизвестная метка {n}"

# The plan's text, without its Markdown backticks: every reply is plain
# text (app/tg/send.py).
NOTES_ON_REPLY = (
    "Anchor будет читать заметки с меткой anchor: personal или anchor: knowledge "
    "(и папки из Anchor/settings.md). Личные — только для разговора; знания — ещё и как "
    "справка. /vault notes off — забыть всё прочитанное."
)
NOTES_OFF_REPLY = "Заметки выключены: всё, что Anchor прочитал из заметок, удалено."
VAULT_USAGE = "Команды: /vault — состояние, /vault notes on — читать заметки, /vault notes off — забыть их."

UNKNOWN = "—"


def _when(moment: datetime.datetime | None, timezone: str, clock: Clock) -> str:
    """HH:MM today, «вчера HH:MM» yesterday, DD.MM HH:MM otherwise."""
    if moment is None:
        return UNKNOWN
    local = moment.astimezone(clock_module.zone(timezone))
    today = clock_module.now_local(clock, timezone).date()
    days = (today - local.date()).days
    if days <= 0:
        return local.strftime("%H:%M")
    if days == 1:
        return "вчера " + local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


def format_notes_line(consent: bool, notes: vault_status.NotesOverview | None) -> str | None:
    """The notes line, or None when there is nothing true to say."""
    if not consent:
        return NOTES_OFF_LINE
    if notes is None:
        return None
    if notes.settings == "invalid":
        return NOTES_SETTINGS_INVALID_LINE
    line = NOTES_COUNTS.format(personal=notes.personal, knowledge=notes.knowledge)
    check = []
    if notes.conflict:
        check.append(NOTES_CHECK_CONFLICT.format(n=notes.conflict))
    if notes.legacy_read:
        check.append(NOTES_CHECK_LEGACY.format(n=notes.legacy_read))
    if check:
        line += NOTES_CHECK.format(items=", ".join(check))
    if notes.unknown_value:
        line += NOTES_UNREAD.format(n=notes.unknown_value)
    return line


def format_vault(
    health: vault_status.Health,
    settings: Settings,
    clock: Clock,
    timezone: str,
    *,
    facts: int | None = None,
    notes_line: str | None = None,
    problems: tuple[list[vault_status.ProblemRow], int] | None = None,
) -> str:
    if health.state == vault_status.OFF:
        return OFF_REPLY
    if health.state == vault_status.OK:
        line = OK_LINE.format(
            since=_when(health.running_since, timezone, clock), restarts=health.restarts
        )
    elif health.state == vault_status.STOPPED:
        code = UNKNOWN if health.last_exit_code is None else health.last_exit_code
        line = STOPPED_LINE.format(restarts=health.restarts, code=code)
    elif health.state == vault_status.UNAUTHORIZED:
        line = UNAUTHORIZED_LINE
    else:
        line = UNREACHABLE_LINE
        if health.last_ok_at is not None:
            line += UNREACHABLE_SINCE.format(when=_when(health.last_ok_at, timezone, clock))
    mirroring = settings.VAULT_MODE in ("mirror", "sync")
    if mirroring and facts is not None:
        line = line.rstrip(".") + FACTS_SUFFIX.format(count=facts)
    if settings.VAULT_MODE == "mirror":
        # sync (8c) applies fact/kind/pinned edits, so it no longer says
        # edits go nowhere -- the file listing (8c's own /vault work,
        # phase C) is what will explain quarantines and holds there.
        line += "\n" + MIRROR_NOTE
    if notes_line is not None:
        line += "\n" + notes_line
    if problems is not None:
        block = format_problems(*problems)
        if block is not None:
            line += "\n" + block
    return line


def format_state_line(
    health: vault_status.Health, clock: Clock, timezone: str, *, purge_pending: bool = False
) -> str:
    # 8b: a /delete whose vault purge is still retrying outranks every
    # other state -- it is the one thing the user asked for and has not
    # got yet.
    if purge_pending:
        return STATE_PURGE_PENDING
    if health.state == vault_status.OFF:
        return STATE_OFF
    if health.state == vault_status.OK:
        return STATE_OK
    if health.state == vault_status.STOPPED:
        return STATE_STOPPED
    if health.state == vault_status.UNAUTHORIZED:
        return STATE_UNAUTHORIZED
    if health.last_ok_at is not None:
        return STATE_UNREACHABLE_SINCE.format(when=_when(health.last_ok_at, timezone, clock))
    return STATE_UNREACHABLE


# --- /vault's problem list (8c, plan section 8) -----------------------------

PROBLEMS_HEADER = "Требуют внимания:"
PROBLEM_LINE = "- {path} — {label}"
PROBLEMS_MORE = "…и ещё {n}"
PROBLEMS_SHOWN = 5

HELD_LABEL = "ждёт ответа в Telegram"
DIVERGED_LABEL = "изменён вручную, больше не обновляю"
UNKNOWN_REASON_LABEL = "требует проверки"

# One label per app/vault/errors.QUARANTINE_CODES entry -- checked
# against that frozenset by tests/test_vault_notices.py, so a new code
# there cannot go live without a Russian label here. An unknown code
# (there should never be one) fails closed to UNKNOWN_REASON_LABEL
# rather than echoing it.
QUARANTINE_LABELS = {
    vault_errors.NAME_TAKEN: "имя файла занято другим файлом",
    vault_errors.BAD_YAML: "свойства не читаются (YAML)",
    vault_errors.BAD_TYPE: "свойство неверного типа: fact — в кавычках, pinned — true или false",
    vault_errors.BAD_KIND: "неизвестный kind: можно identity, preference, event или rule",
    vault_errors.TECHNIQUE: "технику нельзя создать или сменить из хранилища",
    vault_errors.EMPTY: "пустой факт",
    vault_errors.TOO_LONG: "слишком длинный факт (больше 300 символов)",
    vault_errors.UNSAFE: "похоже на пароль или номер карты — не сохраняю",
    vault_errors.INSTRUCTION: "похоже на команду для бота — не принято",
    vault_errors.PIN_CAP: "слишком много закреплённых фактов",
    vault_errors.DUPLICATE_FILE: "копия другого файла факта",
    vault_errors.DUPLICATE_FACT: "такой факт уже есть",
    vault_errors.PROTECTED: "часть принятой техники — не забывается",
}


def problem_label(state: str, reason: str | None) -> str:
    """The plain-Russian reason /vault shows for one row.

    `state` decides for `held` and `diverged` (the reason column plays
    no part there); `quarantined` reads it from `reason`, failing
    closed to UNKNOWN_REASON_LABEL for a code this build does not know
    -- never the code itself (plan section 8: file names and reasons
    appear in this reply, never a raw value from elsewhere).
    """
    if state == "held":
        return HELD_LABEL
    if state == "diverged":
        return DIVERGED_LABEL
    return QUARANTINE_LABELS.get(reason, UNKNOWN_REASON_LABEL)


def format_problems(rows: list[vault_status.ProblemRow], total: int) -> str | None:
    """The "Требуют внимания" block, or None when there is nothing to show."""
    if not rows:
        return None
    lines = [PROBLEMS_HEADER]
    shown = rows[:PROBLEMS_SHOWN]
    lines += [PROBLEM_LINE.format(path=row.path, label=problem_label(row.state, row.reason)) for row in shown]
    if total > len(shown):
        lines.append(PROBLEMS_MORE.format(n=total - len(shown)))
    return "\n".join(lines)


# --- the sync-pass notice (8c, plan section 8) ------------------------------

NOTICE_PREFIX = "Хранилище"
NOTICE_NEW_PART = "новых {c}"
NOTICE_CHANGED_PART = "изменено {u}"
NOTICE_FORGOTTEN_PART = "забыто {f}"
NOTICE_QUARANTINED_PART = "Не принято: {q}"
NOTICE_SUFFIX = " — /vault"


def notice_text(result) -> str | None:
    """The at-most-one-per-pass notice (plan section 8), or None.

    None both when nothing changed (the caller then queues nothing) and
    as a plain formatting function otherwise -- whether it may actually
    be sent right now is `may_report_now`'s question, asked by the
    caller (`send_pass_updates` below), not this one.
    """
    created, changed, forgotten, quarantined = (
        result.created_facts,
        result.changed_facts,
        result.forgotten_facts,
        result.quarantined,
    )
    if not (created or changed or forgotten or quarantined):
        return None
    parts = []
    if created:
        parts.append(NOTICE_NEW_PART.format(c=created))
    if changed:
        parts.append(NOTICE_CHANGED_PART.format(u=changed))
    if forgotten:
        parts.append(NOTICE_FORGOTTEN_PART.format(f=forgotten))
    base = NOTICE_PREFIX + (": " + ", ".join(parts) if parts else "")
    if quarantined:
        base += (". " if parts else ": ") + NOTICE_QUARANTINED_PART.format(q=quarantined)
    return base + NOTICE_SUFFIX


# --- Russian plurals (reused by the mass_delete hold text below) -----------


def _ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    """The standard Russian plural rule -- see app/tg/research.py's
    `card_noun`, which this generalises to any noun's three forms:
    11-14 always take the genitive plural, then the last digit decides
    (1 singular, 2-4 genitive singular, else genitive plural)."""
    if n % 100 in range(11, 15):
        return forms[2]
    last = n % 10
    if last == 1:
        return forms[0]
    if last in (2, 3, 4):
        return forms[1]
    return forms[2]


FACT_FORMS = ("факт", "факта", "фактов")


# --- hold messages (8c, plan section 8) -------------------------------------

MASS_DELETE_TEXT = "Из хранилища пропало {n} {noun}. Забыть {pronoun}?"
RULE_EDIT_TEXT = "В хранилище изменено правило: «{text}». Принять?"
RULE_NEW_TEXT = "В хранилище новое правило: «{text}». Принять?"

YES_BUTTON = "Да"
NO_BUTTON = "Нет, вернуть"

CONFIRMED_TEXT = "Принято."
REVERTED_TEXT = "Вернул как было."
STALE_PRESS_TEXT = "Устарело"
STALE_APPLY_TEXT = "Устарело: факт уже изменился."
DUPLICATE_TEXT = "Такой факт уже есть — файл отмечен в /vault."

_OUTCOME_TEXT = {
    vault_holds.CONFIRMED_RESULT: CONFIRMED_TEXT,
    vault_holds.REVERTED_RESULT: REVERTED_TEXT,
    vault_holds.STALE_PRESS: STALE_PRESS_TEXT,
    vault_holds.STALE_APPLY: STALE_APPLY_TEXT,
    vault_holds.DUPLICATE_RESULT: DUPLICATE_TEXT,
}


def mass_delete_text(n: int) -> str:
    return MASS_DELETE_TEXT.format(n=n, noun=_ru_plural(n, FACT_FORMS), pronoun="его" if n == 1 else "их")


def hold_text(hold) -> str:
    """The Russian message for one pending hold's card."""
    if hold.kind == vault_holds.MASS_DELETE:
        return mass_delete_text(len(hold.payload.get("file_ids", [])))
    text = hold.payload["text"]
    if hold.payload.get("supersedes_id") is not None:
        return RULE_EDIT_TEXT.format(text=text)
    return RULE_NEW_TEXT.format(text=text)


def hold_keyboard(hold_id: int, epoch: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=YES_BUTTON, callback_data=f"v:y:{hold_id}:{epoch}"),
                InlineKeyboardButton(text=NO_BUTTON, callback_data=f"v:n:{hold_id}:{epoch}"),
            ]
        ]
    )


async def send_pass_updates(sessionmaker, bot: Bot, settings: Settings, clock: Clock, result) -> None:
    """After one VAULT_SYNC pass: the notice, then any unsent pending holds.

    Both obey `may_report_now`, asked once, right now -- not at the
    time the pass ran, which may have been moments earlier while the
    job's own transaction was still open. When it says no, the notice
    for this pass's changes is simply not sent (plan section 8: "/vault
    shows the same information" -- there is nothing to queue), and
    every still-pending hold is left exactly as `pending_unsent` found
    it, to be tried again on the next pass that allows it.
    """
    async with sessionmaker() as session:
        user_state = await get_state(session)
        if not may_report_now(settings, clock, user_state):
            logger.info("vault pass updates not sent", extra={"event": "quiet"})
            return
        text = notice_text(result)
        if text is not None:
            await bot.send_message(chat_id=user_state.chat_id, text=text)
            logger.info("vault notice sent", extra={"event": "notice"})
        pending = await vault_holds.pending_unsent(session)
        for hold in pending:
            message_id = await send_keyboard(
                bot, user_state.chat_id, hold_text(hold), hold_keyboard(hold.id, user_state.vault_epoch)
            )
            await vault_holds.mark_sent(session, hold.id, message_id)
            logger.info("vault hold sent", extra={"hold_id": hold.id, "kind": hold.kind})


# --- the `v:` callback (8c, plan section 8) ---------------------------------


def _parse_callback(data: str) -> tuple[bool, int, str] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "v" or parts[1] not in ("y", "n"):
        return None
    try:
        hold_id = int(parts[2])
    except ValueError:
        return None
    epoch = parts[3]
    if not EPOCH_RE.match(epoch):
        return None
    return parts[1] == "y", hold_id, epoch


async def handle_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    message_text: str | None = None,
) -> None:
    """`v:y:<hold_id>:<epoch>` / `v:n:<hold_id>:<epoch>` -- the [Да]/[Нет, вернуть] buttons.

    A malformed press, a replay, a non-pending hold, and a stale epoch
    (a button left over from before `/delete`) all answer «Устарело»
    and change nothing -- the router never needs to tell these apart;
    `holds.decide` (and the parse below) already do. Only a stale press
    leaves the message exactly as it was; every other outcome removes
    the keyboard.
    """
    parsed = _parse_callback(data)
    if parsed is None:
        await answer_callback(bot, callback_id, STALE_PRESS_TEXT)
        return
    confirm, hold_id, epoch = parsed
    async with sessionmaker() as session:
        result = await vault_holds.decide(session, hold_id, epoch, confirm, clock)
    text = _OUTCOME_TEXT[result.outcome]
    await answer_callback(bot, callback_id, text)
    if result.outcome == vault_holds.STALE_PRESS:
        return
    await edit_keyboard(bot, chat_id, message_id, message_text or text, None)
