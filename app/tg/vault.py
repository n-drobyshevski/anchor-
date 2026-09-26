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

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.vault import status as vault_status

OFF_REPLY = "Хранилище выключено."
OK_LINE = "Хранилище: синхронизация ок (работает с {since}, перезапусков {restarts})."
STOPPED_LINE = "Хранилище: синхронизация остановлена (перезапусков {restarts}, код выхода {code})."
UNREACHABLE_LINE = "Хранилище: нет связи с сервисом хранилища."
UNREACHABLE_SINCE = " Последний ответ — {when}."
UNAUTHORIZED_LINE = (
    "Хранилище: сервис отказал в доступе — VAULT_API_TOKEN на боте и на сервисе хранилища различается."
)
EARLY_MODE_NOTE = "Режим {mode} появится в следующей версии; пока работает как mirror."
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
    if settings.VAULT_MODE not in vault_status.IMPLEMENTED_MODES:
        line += "\n" + EARLY_MODE_NOTE.format(mode=settings.VAULT_MODE)
    if settings.VAULT_MODE == "mirror":
        # sync (8c) applies fact/kind/pinned edits, so it no longer says
        # edits go nowhere -- the file listing (8c's own /vault work,
        # phase C) is what will explain quarantines and holds there.
        line += "\n" + MIRROR_NOTE
    if notes_line is not None:
        line += "\n" + notes_line
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
