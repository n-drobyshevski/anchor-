"""/lens: the lens's status, and Claude Code's switch (anchor-lens-plan.md section 11).

L1 has two commands:

- `/lens` shows whether the lens is on (LENS_ENABLED), how many notes it
  holds (people and concepts), a warning when there are more than
  LENS_CATALOG_MAX_NOTES (the selector, from L2, will not use an
  oversized lens at all rather than drop notes silently), whether
  Claude Code may log in as `anchor_lens`, and how many times the
  `lens` functions were read today. L2 adds when the newest lens round
  ran (the weekly review's self-selection, app/core/lens_review.py) and
  how it ended -- the outcome, never the selector's `why` or a title.
  L3 adds the lens garden (app/tg/garden.py): «Сад: <дата>, открыто N»,
  the newest run's local date and the open gaps across all runs.
- `/lens code on|off` flips the role's LOGIN. Off also ends the
  sessions already open. When the bot's database user may not alter
  the role, or the role does not exist, the reply says so and points
  at docs/claude-access.md: the user does it by hand, once.

Telegram only, like /claude: opening a database door is an access
decision, and a web session must not be able to make it
(app/web/ingress.py's BLOCKED_COMMANDS, and the router's is_web_sink
guard).

Counts only. This module never reads a title or a body: it asks
app/vault/lens.py for numbers and a switch's state.
"""

from __future__ import annotations

import datetime

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.state import get_state
from app.tg.vault import _ru_plural
from app.vault import lens

ON_LINE = "Линза: включена."
OFF_LINE = "Линза: выключена (LENS_ENABLED). Заметки линзы индексируются как знания, но целиком не хранятся."
COUNTS_LINE = "Заметок в линзе: {total} — людей {people} · понятий {concepts}."
OVER_LIMIT_LINE = (
    "Больше {limit} заметок (LENS_CATALOG_MAX_NOTES): Echo не будет опираться на линзу, "
    "пока их не станет меньше."
)
CODE_ON_LINE = "Claude Code: доступ к линзе открыт (роль anchor_lens может войти)."
CODE_OFF_LINE = "Claude Code: доступ к линзе закрыт."
CODE_MISSING_LINE = "Claude Code: роли anchor_lens нет — см. docs/claude-access.md."
READS_LINE = "Чтений сегодня: {n}."
# L2: the newest `lens_round`, in the user's local date; one outcome
# phrase per ck_lens_round_outcome value (app/vault/lens.py's ROUND_OUTCOMES).
LAST_ROUND_LINE = "Последний разбор: {date}, {outcome}."
# L3: the newest garden run, in the user's local date, and the gaps
# still open across all runs (app/vault/lens.py's `garden_status`).
GARDEN_LINE = "Сад: {date}, открыто {n}."
ROUND_OUTCOME_TEXT = {
    "grounded": "предложения опираются на линзу",
    "empty": "подходящих заметок не нашлось",
    "fallback": "линза не сработала, предложения без неё",
}
UNRECORDED_LINE = (
    "Чтений без записи: {n} — транзакция чтения была откачена (или база упала и сбила "
    "счётчик). Такое чтение могло вернуть заметки; см. docs/claude-access.md."
)
USAGE = (
    "/lens — состояние линзы\n"
    "/lens code on — открыть Claude Code доступ к линзе (только к ней)\n"
    "/lens code off — закрыть доступ и оборвать открытые сессии"
)

# L3: lens.gaps() joins the doors. Its proposals were written from the
# lens alone, so they are no more than lens.notes() already shows; the
# reply says so, and that a gap closed by the recheck reads «closed».
CODE_SET_ON = (
    "Доступ открыт: Claude Code может читать линзу через lens.notes(), lens.graph(), "
    "lens.rounds() и lens.gaps(). lens.rounds() отдаёт, какие заметки выбрал еженедельный "
    "разбор, но не объяснение почему: оно написано по твоей неделе и видно только тебе. "
    "lens.gaps() — предложения сада линзы, написанные только по самой линзе, и их статус "
    "(открыто, сделано, не нужно или закрыто). Больше ничего из заметок. Каждое чтение "
    "считается. Закрыть: /lens code off"
)
CODE_SET_OFF = "Доступ закрыт."
CODE_SET_OFF_TERMINATED = "Доступ закрыт, оборвано сессий: {n}."
CODE_SET_OFF_TERMINATE_FAILED = (
    "Вход закрыт, но открытые сессии оборвать не вышло: не хватает прав. Они закончатся "
    "сами при отключении; сразу — вручную, см. docs/claude-access.md."
)
CODE_MISSING = (
    "Роли anchor_lens нет в базе: миграция не смогла её создать. Создай её вручную — "
    "см. docs/claude-access.md."
)
CODE_ERROR = (
    "Не вышло изменить доступ: ошибка базы. Попробуй ещё раз; вручную — "
    "ALTER ROLE anchor_lens {word}, см. docs/claude-access.md."
)
CODE_DENIED = (
    "Не хватает прав: пользователь базы бота не может менять роль anchor_lens. "
    "Сделай это вручную (ALTER ROLE anchor_lens {word}) — см. docs/claude-access.md."
)

# «Claude Code прочитал линзу: N раз» -- the daily Claude digest's line
# (app/tg/claude.py's run_library_digest).
DIGEST_LINE = "Claude Code прочитал линзу: {n} {noun}."
DIGEST_FORMS = ("раз", "раза", "раз")
DIGEST_UNRECORDED_LINE = "Ещё чтений линзы без записи (транзакция откачена): {n}."


def _day_bounds(day: datetime.date, timezone: str) -> tuple[datetime.datetime, datetime.datetime]:
    start = clock_module.combine_local(day, datetime.time(0, 0), timezone)
    end = clock_module.combine_local(day + datetime.timedelta(days=1), datetime.time(0, 0), timezone)
    return start, end


async def status(sessionmaker, settings: Settings, clock: Clock) -> str:
    async with sessionmaker() as session:
        timezone = (await get_state(session)).timezone
        per_kind = await lens.counts(session)
        can_login = await lens.code_access(session)
        start, end = _day_bounds(clock_module.local_date(clock, timezone), timezone)
        reads = await lens.reads_between(session, start, end)
        unrecorded = await lens.unrecorded_reads(session)
        last = await lens.last_round(session)
        garden = await lens.garden_status(session)
    people, concepts = per_kind.get("person", 0), per_kind.get("concept", 0)
    lines = [
        ON_LINE if settings.LENS_ENABLED else OFF_LINE,
        COUNTS_LINE.format(total=people + concepts, people=people, concepts=concepts),
    ]
    if people + concepts > settings.LENS_CATALOG_MAX_NOTES:
        lines.append(OVER_LIMIT_LINE.format(limit=settings.LENS_CATALOG_MAX_NOTES))
    if can_login is None:
        lines.append(CODE_MISSING_LINE)
    else:
        lines.append(CODE_ON_LINE if can_login else CODE_OFF_LINE)
    lines.append(READS_LINE.format(n=reads))
    if last is not None:
        lines.append(
            LAST_ROUND_LINE.format(
                date=clock_module.local_date_of(last.created_at, timezone).strftime("%d.%m.%Y"),
                outcome=ROUND_OUTCOME_TEXT.get(last.outcome, last.outcome),
            )
        )
    if garden is not None:
        lines.append(
            GARDEN_LINE.format(
                date=clock_module.local_date_of(garden.last_run_at, timezone).strftime("%d.%m.%Y"),
                n=garden.open,
            )
        )
    if unrecorded:
        lines.append(UNRECORDED_LINE.format(n=unrecorded))
    return "\n".join(lines)


async def code(sessionmaker, on: bool) -> str:
    async with sessionmaker() as session:
        outcome = await lens.set_code_access(session, on)
    if outcome.state == "missing":
        return CODE_MISSING
    if outcome.state == "denied":
        return CODE_DENIED.format(word="LOGIN" if on else "NOLOGIN")
    if outcome.state == "error":
        return CODE_ERROR.format(word="LOGIN" if on else "NOLOGIN")
    if on:
        return CODE_SET_ON
    if outcome.terminate_failed:
        return CODE_SET_OFF_TERMINATE_FAILED
    if outcome.terminated:
        return CODE_SET_OFF_TERMINATED.format(n=outcome.terminated)
    return CODE_SET_OFF


async def command(sessionmaker, settings: Settings, clock: Clock, args: str | None) -> str:
    words = (args or "").split()
    if not words:
        return await status(sessionmaker, settings, clock)
    if words == ["code", "on"]:
        return await code(sessionmaker, True)
    if words == ["code", "off"]:
        return await code(sessionmaker, False)
    return USAGE


async def digest_line(session, start: datetime.datetime, end: datetime.datetime) -> str | None:
    """The daily Claude digest's lens line for [start, end), or None when
    Claude Code read nothing in it. A count, never what was read -- plus
    any read whose record was rolled back before a recorded one in the
    window (app/vault/lens.py's `unrecorded_between`)."""
    n = await lens.reads_between(session, start, end)
    hidden = await lens.unrecorded_between(session, start, end)
    lines = []
    if n:
        lines.append(DIGEST_LINE.format(n=n, noun=_ru_plural(n, DIGEST_FORMS)))
    if hidden:
        lines.append(DIGEST_UNRECORDED_LINE.format(n=hidden))
    return " ".join(lines) or None
