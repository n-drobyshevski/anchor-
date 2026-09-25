"""`/vault` and the `/state` line (phase-5 plan section 8), status only in 5a.

The plan's `/vault` has two parts: a first line about the sync, and a
list of files that need attention. 5a has no files to list -- nothing
is rendered or ingested yet -- so it ships the first line only, which
is also exactly what the plan says `status` mode shows.

Every line here is a reply to a command, so it is sent whatever the
pause, quiet or welfare state; `may_report_now` governs unsolicited
vault messages (5c), not this.
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
EARLY_MODE_NOTE = "Режим {mode} появится в следующей версии; пока только статус."

STATE_OFF = "Хранилище: выключено"
STATE_OK = "Хранилище: ок"
STATE_STOPPED = "Хранилище: синхронизация остановлена"
STATE_UNREACHABLE = "Хранилище: нет связи"
STATE_UNREACHABLE_SINCE = "Хранилище: нет связи с {when}"
STATE_UNAUTHORIZED = "Хранилище: нет доступа (токен)"

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


def format_vault(health: vault_status.Health, settings: Settings, clock: Clock, timezone: str) -> str:
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
    if settings.VAULT_MODE not in vault_status.IMPLEMENTED_MODES:
        line += "\n" + EARLY_MODE_NOTE.format(mode=settings.VAULT_MODE)
    return line


def format_state_line(health: vault_status.Health, clock: Clock, timezone: str) -> str:
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
