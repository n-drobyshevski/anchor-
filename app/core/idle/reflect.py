"""The `reflect` idle kind (Phase 6 plan section 6.3; milestone 6b).

A deeper, aggregate version of `app/core/notebook.py`'s per-scene
`notebook_reflect`: instead of one scene, its input is the last 7 local
days of scene summaries, journal lines, check-ins with their standing-
order results, and every active notebook entry (with ids). Its output
is the **same** schema (`notebook.REFLECT_SCHEMA`), validated by the
**same** function (`notebook.validate`) and applied by the **same**
shared apply step (`notebook.apply_plan`) -- so the ownership rule
("Anchor can never close/edit a user- or review-sourced entry") and
"reflection never adds an intention" hold here for exactly the reason
they hold for the per-scene job: it is the same code.

**Why this module does not import `app.core.review`, even read-only.**
`tests/test_idle_isolation.py`'s AST scan bans the whole module
(`"app.core.review": "the weekly review -- writes WeeklyReview/
ReviewProposal"`), and the coordinator's resolution keeps that ban
literal rather than narrowed. `review.py`'s own `_week_checkins`/
`_week_journal`/`_week_scene_summaries` do the right welfare-exclusion
and windowing, but they are private to that module and importing them
would still be importing `app.core.review`. So the (short, read-only)
queries below are written directly against `app.db.models`, following
the exact same shape those helpers use -- **same welfare-exclusion
join** as `review._week_scene_summaries` and `app/core/notebook.py`'s
own `_has_welfare_message`, applied consistently to both the input
builder and the kind rule's own "is there anything new" check, so the
two can never disagree about which scenes exist.

Same reasoning rules out `app.core.orders` (also banned -- "standing
orders -- not an idle-writable table"): the check-in/order-result query
below reads `StandingOrder`/`CheckinOrderResult` directly rather than
calling `orders.results_for_checkin`/`orders.active_orders`.

**Preemption and the single transaction.** Same shape as
`app/core/idle/consolidate.py`: reads and the model call happen with no
open write transaction; `apply_plan` runs inside one transaction opened
only after the model call returns and after the in-job preemption
re-check, via `RunContext`.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core import notebook as notebook_module
from app.core import safety_events
from app.core.clock import Clock
from app.core.extract import parse_json
from app.db.models import (
    Checkin,
    CheckinOrderResult,
    IdleRun,
    Journal,
    Message,
    Scene,
    StandingOrder,
    UserState,
)
from app.llm.provider import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

REFLECT_CATEGORY = "idle:reflect"
REFLECT_WINDOW_DAYS = 7

# Standing order fields, duplicated from app/core/orders.py rather than
# imported -- see the module docstring.
_ORDER_ACTIVE = "active"
_ORDER_WEEKLY = "weekly"
_WEEKDAY_NAMES = {
    1: "понедельникам", 2: "вторникам", 3: "средам", 4: "четвергам",
    5: "пятницам", 6: "субботам", 7: "воскресеньям",
}
_CADENCE_LABELS = {"daily": "ежедневно", "weekdays": "по будням", "once": "один раз"}


def _cadence_label(cadence: str, weekday: int | None) -> str:
    if cadence == _ORDER_WEEKLY and weekday in _WEEKDAY_NAMES:
        return f"по {_WEEKDAY_NAMES[weekday]}"
    return _CADENCE_LABELS.get(cadence, cadence)


REFLECT_PROMPT = (
    "Ты ведёшь рабочие заметки Anchor о пользователе. Это недельный взгляд назад: "
    "по итогам последних 7 дней добавь наблюдения (устойчивые закономерности) и "
    "незакрытые темы (что обещано, начато или стоит спросить позже). Закрой темы, "
    "которые решены. Пиши по-русски, коротко, фактами.\n"
    "Запрещено: диагнозы, психологические ярлыки и типы личности, здоровье, "
    "кризисы, догадки о мотивах, заметки об ужесточении, наказаниях или "
    "повышении интенсивности, подробности о третьих лицах, намерения (intention) "
    "-- их пишет только пользователь."
)


@dataclasses.dataclass(frozen=True)
class ReflectResult:
    added: int
    closed: int
    updated: int
    dropped: int
    preempted: bool = False


def _window(clock: Clock, timezone: str) -> tuple[datetime.datetime, datetime.datetime]:
    today = clock_module.local_date(clock, timezone)
    start_day = today - datetime.timedelta(days=REFLECT_WINDOW_DAYS - 1)
    start_at = clock_module.combine_local(start_day, datetime.time(0, 0), timezone)
    end_at = clock_module.combine_local(
        today + datetime.timedelta(days=1), datetime.time(0, 0), timezone
    )
    return start_at, end_at


async def _welfare_scene_ids(session: AsyncSession, scene_ids: list[int]) -> set[int]:
    if not scene_ids:
        return set()
    result = await session.execute(
        select(Message.scene_id)
        .where(Message.scene_id.in_(scene_ids))
        .where(Message.kind == "welfare")
        .distinct()
    )
    return {row[0] for row in result.all()}


async def _window_scene_summaries(
    session: AsyncSession, *, start_at: datetime.datetime, end_at: datetime.datetime
) -> list[str]:
    """Closed scenes' summaries in the window, welfare scenes excluded --
    same filter as review._week_scene_summaries and
    notebook._has_welfare_message, applied independently here (module
    docstring: importing review.py is not allowed)."""
    result = await session.execute(
        select(Scene.id, Scene.summary)
        .where(Scene.ended_at.is_not(None))
        .where(Scene.summary.is_not(None))
        .where(Scene.ended_at >= start_at)
        .where(Scene.ended_at < end_at)
        .order_by(Scene.ended_at)
    )
    rows = result.all()
    if not rows:
        return []
    welfare_ids = await _welfare_scene_ids(session, [scene_id for scene_id, _ in rows])
    return [summary for scene_id, summary in rows if scene_id not in welfare_ids]


async def _window_journal(
    session: AsyncSession, *, start_day: datetime.date, end_day: datetime.date
) -> list[str]:
    result = await session.execute(
        select(Journal.text)
        .where(Journal.local_date >= start_day)
        .where(Journal.local_date <= end_day)
        .order_by(Journal.local_date, Journal.id)
    )
    return [row[0] for row in result.all()]


async def _window_checkins(
    session: AsyncSession, *, start_day: datetime.date, end_day: datetime.date
) -> list[tuple[Checkin, list[tuple[str, str]]]]:
    result = await session.execute(
        select(Checkin)
        .where(Checkin.local_date >= start_day)
        .where(Checkin.local_date <= end_day)
        .order_by(Checkin.local_date)
    )
    checkins = list(result.scalars().all())
    out: list[tuple[Checkin, list[tuple[str, str]]]] = []
    for checkin in checkins:
        results = await session.execute(
            select(StandingOrder.text, CheckinOrderResult.result)
            .join(CheckinOrderResult, CheckinOrderResult.order_id == StandingOrder.id)
            .where(CheckinOrderResult.checkin_id == checkin.id)
            .order_by(StandingOrder.id)
        )
        out.append((checkin, [(text, value) for text, value in results.all()]))
    return out


async def _active_order_lines(session: AsyncSession) -> list[str]:
    result = await session.execute(
        select(StandingOrder)
        .where(StandingOrder.status == _ORDER_ACTIVE)
        .order_by(StandingOrder.id)
    )
    return [
        f"«{row.text}» ({_cadence_label(row.cadence, row.weekday)})"
        for row in result.scalars().all()
    ]


def build_idle_reflect_input(
    *,
    window_start: datetime.date,
    window_end: datetime.date,
    scene_summaries: list[str],
    journal_lines: list[str],
    checkins: list[tuple[Checkin, list[tuple[str, str]]]],
    view: notebook_module.NotebookView,
    due_action: str | None,
    order_lines: list[str],
) -> str:
    """The user-role message -- same id-exposure rule as
    `notebook.build_input`'s own docstring: ids appear here and only
    here, non-Anchor entries marked as a hint, `validate()` is the
    actual boundary."""
    lines = [f"## Последние 7 дней: {window_start.isoformat()} — {window_end.isoformat()}"]

    lines.append("")
    lines.append("## Сессии")
    if scene_summaries:
        lines.extend(f"- {line}" for line in scene_summaries)
    else:
        lines.append("(нет закрытых сессий)")

    lines.append("")
    lines.append("## Журнал")
    if journal_lines:
        lines.extend(f"- {line}" for line in journal_lines)
    else:
        lines.append("(пусто)")

    lines.append("")
    lines.append("## Чек-ины и договорённости")
    if checkins:
        for row, results in checkins:
            parts = [row.local_date.isoformat()]
            if row.day_rating:
                parts.append(f"день {row.day_rating}/5")
            if row.due_result:
                parts.append(f"действие: {row.due_result}")
            if results:
                tail = "; ".join(f"«{text}» — {result}" for text, result in results)
                parts.append(f"договорённости: {tail}")
            lines.append("- " + " · ".join(parts))
    else:
        lines.append("(чек-инов не было)")

    lines.append("")
    lines.append("## Твои текущие заметки (id — текст)")
    entries = [*view.intentions, *view.observations, *view.threads]
    if entries:
        for entry_id, text, source in entries:
            marker = "" if source == "anchor" else f" ({source})"
            lines.append(f"{entry_id} — {text}{marker}")
    else:
        lines.append("(пока пусто)")

    lines.append("")
    lines.append(f"Главное действие: {due_action or 'нет'}")
    if order_lines:
        lines.append("Договорённости: " + "; ".join(order_lines))

    return "\n".join(lines)


async def has_new_summary_since(session: AsyncSession, since: datetime.datetime | None) -> bool:
    """Any welfare-free scene summary that appeared after `since` (or
    ever, if `since` is None -- no reflect has ever completed). The
    kind rule (app/core/idle/gate.py's `_reflect_rule`)."""
    query = (
        select(Scene.id)
        .where(Scene.ended_at.is_not(None))
        .where(Scene.summary.is_not(None))
    )
    if since is not None:
        query = query.where(Scene.ended_at > since)
    result = await session.execute(query.limit(50))
    scene_ids = [row[0] for row in result.all()]
    if not scene_ids:
        return False
    welfare_ids = await _welfare_scene_ids(session, scene_ids)
    return bool(set(scene_ids) - welfare_ids)


async def last_done_reflect_finished_at(session: AsyncSession) -> datetime.datetime | None:
    """`finished_at` of the most recent *done* reflect run -- a skipped
    or failed run never advances this watermark (module docstring)."""
    result = await session.execute(
        select(IdleRun.finished_at)
        .where(IdleRun.kind == "reflect")
        .where(IdleRun.status == "done")
        .order_by(IdleRun.finished_at.desc())
        .limit(1)
    )
    row = result.first()
    return row[0] if row is not None else None


async def run_reflect(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    safety_provider: LLMProvider,
    clock: Clock,
    *,
    run_id: int,
    started_at: datetime.datetime,
    timezone: str,
) -> ReflectResult:
    """The `reflect` idle kind body (plan section 6.3), called by
    `app/core/idle/runner.py`."""
    # Lazy, same reason as app/core/idle/consolidate.py's own
    # run_consolidate: app/core/idle/facts.py imports this module at
    # module level (for has_new_summary_since), and facts.py is itself
    # imported at module level by app/core/idle/runner.py -- a
    # module-level import of runner.py here would close that into a
    # cycle.
    from app.core.idle.runner import RunContext, is_preempted

    start_at, end_at = _window(clock, timezone)
    start_day, end_day = start_at.date(), (end_at - datetime.timedelta(days=1)).date()

    async with session_factory() as session:
        scene_summaries = await _window_scene_summaries(session, start_at=start_at, end_at=end_at)
        journal_lines = await _window_journal(session, start_day=start_day, end_day=end_day)
        checkins = await _window_checkins(session, start_day=start_day, end_day=end_day)
        view = await notebook_module.active_entries(session)
        entries_by_id = {
            entry_id: source
            for entry_id, _, source in [*view.intentions, *view.observations, *view.threads]
        }
        state = (await session.execute(select(UserState).where(UserState.id == 1))).scalar_one_or_none()
        due_action = state.due_action if state is not None else None
        order_lines = await _active_order_lines(session)

        user_text = build_idle_reflect_input(
            window_start=start_day, window_end=end_day,
            scene_summaries=scene_summaries, journal_lines=journal_lines,
            checkins=checkins, view=view, due_action=due_action, order_lines=order_lines,
        )

    response = await safety_provider.complete(
        [
            LLMMessage(role="system", content=REFLECT_PROMPT),
            LLMMessage(role="user", content=user_text),
        ],
        conversation_id=f"anchor-idle-reflect-{run_id}",
        json_schema=notebook_module.REFLECT_SCHEMA,
    )

    async with session_factory() as session:
        ctx = RunContext(
            session=session, settings=settings, clock=clock, run_id=run_id,
            kind="reflect", started_at=started_at, timezone=timezone,
        )
        await ctx.charge(response.usage, response.model)

        payload = parse_json(response.text)
        await safety_events.record_in(
            session, clock=clock, timezone=timezone, kind=safety_events.NOTEBOOK,
            outcome=safety_events.PARSE_FAIL if payload is None else "ok", model=response.model,
        )
        await session.commit()

        if payload is None:
            logger.warning("idle reflect returned unparseable output", extra={"run_id": run_id})
            return ReflectResult(added=0, closed=0, updated=0, dropped=0)

        plan = notebook_module.validate(payload, entries=entries_by_id)

    async with session_factory() as session:
        if await is_preempted(session, clock, started_at):
            return ReflectResult(added=0, closed=0, updated=0, dropped=0, preempted=True)

        ctx = RunContext(
            session=session, settings=settings, clock=clock, run_id=run_id,
            kind="reflect", started_at=started_at, timezone=timezone,
        )
        result = await notebook_module.apply_plan(
            session, settings, plan, clock=clock, scene_id=None, on_change=ctx.record_change,
        )
        await session.commit()

    logger.info(
        "idle reflect run done",
        extra={
            "run_id": run_id, "added": result.added, "closed": result.closed,
            "updated": result.updated, "dropped": result.dropped,
        },
    )
    return ReflectResult(
        added=result.added, closed=result.closed, updated=result.updated, dropped=result.dropped,
    )


__all__ = [
    "REFLECT_CATEGORY",
    "REFLECT_PROMPT",
    "REFLECT_WINDOW_DAYS",
    "ReflectResult",
    "build_idle_reflect_input",
    "has_new_summary_since",
    "last_done_reflect_finished_at",
    "run_reflect",
]
