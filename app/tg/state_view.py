"""/state as a rich message (Bot API 10.1's `sendRichMessage`/`InputRichMessage`).

Pure, like app/tg/menu.py: no DB, no I/O, no clock of its own -- it
takes exactly the inputs app/tg/router.py's `_format_state` takes and
returns an `InputRichMessage`, or (for the refresh button) an
`InlineKeyboardMarkup`. router.py owns every side effect (sending,
editing, the fallback to plain text).

The value-computing helpers below (`last_checkin_parts`, `due_text`,
`welfare_value`, ...) are shared with `_format_state`/`_format_outbound`
in router.py so the two renderers cannot silently drift apart: each one
computes the *same* answer to "what does this line say", and the two
renderers only differ in how they lay that answer out (a line of plain
text there, a table cell -- sometimes wrapped in a live `RichTextDateTime`
-- here). Where a value carries a real instant (last check-in, a quiet-
until, the next planned message, an attention deadline, a backup run),
the helper returns `(text, dt)` so this module can wrap `text` as that
entity's fallback and `dt` as its `unix_time`; a client that does not
render date_time entities then reads exactly the string
`_format_state` would have printed.

Bot API 9.5's date-time formats used here (see date_time_format's own
docstring for the full regex): `r` (live relative time -- the last
check-in, so "how long ago" updates itself without a fresh message);
`t`/`dt` (short time / short date+time) for everything else, matching
what a fixed local-time string would have shown.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichBlockDetails,
    InputRichBlockFooter,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    RichBlockTableCell,
    RichTextDateTime,
)

from app.config import Settings
from app.core import clock as clock_module
from app.core import safety_events
from app.core.clock import Clock

# Shared with _format_outbound (plan section 10's three lines) -- "—"
# for "nothing planned/nothing to show".
NOTHING = "—"

OUTBOUND_KIND_LABELS = {
    "morning": "утро",
    "evening_nag": "вечер",
    "silence": "тишина",
    "tick": "тик",
}

REFRESH_LABEL = "🔄 Обновить"
# "st:" collides with none of app/tg/router.py's other callback prefixes
# (that module's own docstring lists them: mn:, m:k:/m:p:, c:, d:, w:,
# so:, ob:, am:, p:, pa:, pl:, r:, g:, cl:, it:, idle:, nb:x:).
REFRESH_CALLBACK = "st:r"


# --- value helpers shared with router.py's _format_state/_format_outbound ---


def last_checkin_parts(
    user_state, tz: ZoneInfo, today: datetime.date
) -> tuple[str, datetime.datetime | None]:
    """"сегодня/вчера/N дн. назад HH:MM", or "давно" with no timestamp."""
    if user_state.last_checkin_at is None:
        return "давно", None
    local = user_state.last_checkin_at.astimezone(tz)
    days = (today - local.date()).days
    when = "сегодня" if days <= 0 else "вчера" if days == 1 else f"{days} дн. назад"
    return f"{when} {local.strftime('%H:%M')}", user_state.last_checkin_at


def due_text(user_state, tz: ZoneInfo, today: datetime.date) -> str:
    if not user_state.due_action:
        return "нет"
    due = f"«{user_state.due_action}»"
    if user_state.due_set_at:
        days = (today - user_state.due_set_at.astimezone(tz).date()).days
        due += " (задано сегодня)" if days <= 0 else f" (задано {days} дн. назад)"
    return due


def welfare_value(welfare_counts) -> str | None:
    if welfare_counts is None:
        return None
    ok, failures = welfare_counts
    return f"ok {ok} · сбои {failures}"


def research_value(research_counts) -> str | None:
    """None both when there is nothing to show and when research has
    never run -- see _format_state's own docstring for why the research
    line, unlike welfare's, stays hidden until there is something to say."""
    if research_counts is None:
        return None
    distill_counts, search_counts = research_counts
    if not (any(distill_counts) or any(search_counts)):
        return None
    return (
        f"разбор ok {distill_counts[0]} · сбои {distill_counts[1]} · "
        f"поиск ok {search_counts[0]} · сбои {search_counts[1]}"
    )


def idle_value(idle) -> str:
    idle_spend, idle_cap, idle_jobs = idle
    return f"{idle_spend:.2f} / {idle_cap:.2f}, задач {idle_jobs}"


def canary_value(canary) -> str:
    canary_date, canary_passed = canary
    mark = "ок" if canary_passed else "⚠️"
    return f"{canary_date.isoformat()} {mark}"


def backup_parts(backup, tz: ZoneInfo) -> tuple[str, datetime.datetime]:
    """`pruned` counts as healthy (the backup itself succeeded); only
    `failed` gets the warning glyph -- see _format_state's own comment."""
    backup_started_at, backup_status = backup
    local_started = backup_started_at.astimezone(tz)
    if backup_status == "failed":
        return f"⚠️ ошибка {local_started.date().isoformat()}", backup_started_at
    return f"{local_started.strftime('%Y-%m-%d %H:%M')} ок", backup_started_at


def debt_value(debts) -> str | None:
    if debts is None or not debts[0]:
        return None
    overdue = f" (просрочено {debts[1]})" if debts[1] else ""
    return f"{debts[0]}{overdue} · /paid"


def attention_parts(user_state, clock: Clock, tz: ZoneInfo) -> tuple[str, datetime.datetime] | None:
    attention_until = getattr(user_state, "attention_until", None)
    if getattr(user_state, "attention", "present") != "short" or attention_until is None:
        return None
    if attention_until <= clock.now_utc():
        return None
    until_local = attention_until.astimezone(tz).strftime("%H:%M")
    return f"коротко до {until_local}", attention_until


def quiet_until_parts(
    summary, tz: ZoneInfo, now_utc: datetime.datetime
) -> tuple[str, datetime.datetime | None]:
    if summary.quiet_until is not None and summary.quiet_until > now_utc:
        return summary.quiet_until.astimezone(tz).strftime("%d.%m %H:%M"), summary.quiet_until
    return NOTHING, None


def next_outbound_parts(summary, tz: ZoneInfo) -> tuple[str, datetime.datetime | None]:
    if summary.next_kind is None:
        return NOTHING, None
    label = OUTBOUND_KIND_LABELS.get(summary.next_kind, summary.next_kind)
    when = summary.next_planned_for.astimezone(tz).strftime("%H:%M")
    return f"{label} в {when}", summary.next_planned_for


def spend_breakdown_text(by_category) -> str:
    if not by_category:
        return ""
    return " · " + " · ".join(f"{name} {total:.2f}" for name, total in by_category.items())


# --- rich layout -------------------------------------------------------


def _cell(text, *, header: bool = False, colspan: int | None = None) -> RichBlockTableCell:
    return RichBlockTableCell(
        align="left", valign="middle", text=text, is_header=header or None, colspan=colspan
    )


def _row(label: str, value) -> list[RichBlockTableCell]:
    return [_cell(label, header=True), _cell(value)]


def _vault_row(vault_line: str) -> list[RichBlockTableCell]:
    """vault_ui.format_state_line's own strings are all "Label: value"
    ("Хранилище: ..."), but a future one might not be -- split when the
    shape holds, else show it whole across both columns."""
    if ": " in vault_line:
        label, value = vault_line.split(": ", 1)
        return _row(label, value)
    return [_cell(vault_line, colspan=2)]


def _table(rows: list[list[RichBlockTableCell]]) -> InputRichBlockTable:
    return InputRichBlockTable(cells=rows, is_striped=True, is_compact=True)


def _entity_or_text(text: str, dt: datetime.datetime | None, date_time_format: str):
    if dt is None:
        return text
    return RichTextDateTime(text=text, unix_time=int(dt.timestamp()), date_time_format=date_time_format)


def render(
    user_state,
    spend,
    settings: Settings,
    clock: Clock,
    *,
    by_category=None,
    memories: int = 0,
    outbound=None,
    welfare_counts=None,
    research_counts=None,
    mood=None,
    idle=None,
    canary=None,
    backup=None,
    debts=None,
    persona_version=None,
    vault_line=None,
) -> InputRichMessage:
    """The same facts _format_state shows, as blocks rather than lines.

    Same kwargs, same conditions for when an optional row appears --
    read _format_state alongside this if the two ever look like they
    disagree, since that would mean a drift between renderers.
    """
    tz = ZoneInfo(user_state.timezone)
    now_local = clock_module.now_local(clock, user_state.timezone)
    today = now_local.date()

    persona_value = ("вкл" if user_state.persona_active else "выкл") + (
        f" · v{persona_version}" if persona_version else ""
    )
    checkin_text, checkin_dt = last_checkin_parts(user_state, tz, today)

    core_rows = [
        _row("Персона", persona_value),
        _row("Интенсивность", f"{user_state.intensity}/5"),
        _row("Фокус", "вкл" if user_state.focus_on else "выкл"),
        _row("Серия", f"{user_state.streak} дн."),
        _row("Последний чек-ин", _entity_or_text(checkin_text, checkin_dt, "r")),
        _row("Настроение", str(mood)),
        _row("Главное действие", due_text(user_state, tz, today)),
    ]
    debt = debt_value(debts)
    if debt is not None:
        core_rows.append(_row("Долг", debt))
    attention = attention_parts(user_state, clock, tz)
    if attention is not None:
        att_text, att_dt = attention
        core_rows.append(_row("Внимание", _entity_or_text(att_text, att_dt, "t")))

    today_rows = [
        _row(
            "Потрачено сегодня",
            f"{spend:.2f} / {settings.DAILY_USD_CAP:.2f} USD{spend_breakdown_text(by_category)}",
        )
    ]
    if outbound is not None:
        quiet_text, quiet_dt = quiet_until_parts(outbound, tz, clock.now_utc())
        next_text, next_dt = next_outbound_parts(outbound, tz)
        today_rows.extend(
            [
                _row("Тихо до", _entity_or_text(quiet_text, quiet_dt, "dt")),
                _row("Без ответа подряд", str(outbound.ignored_in_row)),
                _row("Сам написал сегодня", f"{outbound.sent_today} / {outbound.max_per_day}"),
                _row("Следующее", _entity_or_text(next_text, next_dt, "t")),
                _row("Последний отказ", outbound.last_skip_reason or NOTHING),
            ]
        )

    blocks: list = [
        InputRichBlockSectionHeading(text="Состояние", size=2),
        _table(core_rows),
        InputRichBlockSectionHeading(text="Сегодня", size=4),
        _table(today_rows),
    ]

    system_rows = []
    welfare = welfare_value(welfare_counts)
    if welfare is not None:
        system_rows.append(_row(f"Проверка благополучия ({safety_events.WINDOW_DAYS} дн.)", welfare))
    research = research_value(research_counts)
    if research is not None:
        system_rows.append(_row(f"Исследования ({safety_events.WINDOW_DAYS} дн.)", research))
    if idle is not None:
        system_rows.append(_row("Фон", idle_value(idle)))
    if canary is not None:
        system_rows.append(_row("Канарейка", canary_value(canary)))
    if backup is not None:
        backup_text, backup_dt = backup_parts(backup, tz)
        system_rows.append(_row("Бэкап", _entity_or_text(backup_text, backup_dt, "dt")))
    if vault_line:
        system_rows.append(_vault_row(vault_line))
    system_rows.append(_row("Помню", f"{memories} записей"))
    # No live "current local time" here (unlike _format_state's
    # "Локальное время" line): a snapshot instant would read stale in a
    # message meant to be refreshed rather than re-sent, and the footer
    # already carries a live "Обновлено" timestamp -- see docs/decisions.md.
    system_rows.append(_row("Часовой пояс", user_state.timezone))
    system_rows.append(_row("Модель", settings.LLM_MODEL))
    blocks.append(InputRichBlockDetails(summary="Система", is_open=False, blocks=[_table(system_rows)]))

    now_utc = clock.now_utc()
    blocks.append(
        InputRichBlockFooter(
            text=[
                "Обновлено ",
                RichTextDateTime(
                    text=now_local.strftime("%H:%M"), unix_time=int(now_utc.timestamp()), date_time_format="r"
                ),
            ]
        )
    )

    return InputRichMessage(blocks=blocks)


def refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=REFRESH_LABEL, callback_data=REFRESH_CALLBACK)]]
    )
