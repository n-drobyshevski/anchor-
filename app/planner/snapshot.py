"""The local `planner_snapshot` cache: the only thing a chat turn reads.

`sync()` is called from the `PLANNER_SYNC` job body (app/planner/jobs.py)
and nowhere else -- the chat-turn path (app/core/turn.py) only ever
calls `get_snapshot()` / `render_lines()`, both plain reads with no
network call, so a slow or unreachable planner never adds latency to a
reply (design review section 2.1, P2's "done when": "chat latency is
unchanged").

`render_lines()` returns `[]` -- not an error, not a placeholder line --
whenever the snapshot is missing or older than
`PLANNER_SNAPSHOT_MAX_AGE_MIN`. `app/core/prompt.py`'s
`build_now_block(planner=None)` already omits the section entirely for
an empty/None list, so a stale planner degrades to "the plan section
just is not there today", not a stale or wrong one.

Titles pass through `app.research.injection.is_clean` before they ever
reach a prompt. The planner's own titles are member-authored, not page
text from the open web, but the check is cheap and the alternative --
"we trust every title because owners typed it" -- is exactly the
assumption plan section 3.2 item 7 flags as a real if narrow risk: a
title the user typed and the model later echoes is stored in the
transcript and can be extracted, same as anything else said in chat.

Anchor plan, "Anchor" section (PLANNER_HEALTH): `sync()` makes one
extra `get_health {date: today, days: 14}` call, best-effort -- a
failure there is logged and dropped, never allowed to lose the agenda
fetch that already succeeded. `render_lines()` turns that into at most
one line, e.g. «Сон: 6ч10м (глубокий 14%), HRV ниже твоей нормы, пульс
покоя 58», read for *tone only*: nothing here diagnoses or gives
medical advice (persona/persona.md's own "Границы" rule covers the
model's side of that).
"""

from __future__ import annotations

import datetime
import logging
import statistics

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import PlannerSnapshot
from app.research.injection import is_clean

logger = logging.getLogger(__name__)

SNAPSHOT_ID = 1

TITLE_MAX = 80
NO_TITLE = "(без названия)"
PARTNER_LABEL = "(партнёр)"
OVERDUE_TAG = " (просрочено)"

# get_health {days: 14} covers today plus a 13-day baseline window; a
# median needs at least this many non-null values to mean anything, so
# fewer than this and the comparison is simply omitted from the line.
HEALTH_BASELINE_MIN_SAMPLES = 5
HEALTH_SYNC_DAYS = 14


async def sync(
    session: AsyncSession,
    settings: Settings,
    client,
    clock: Clock,
    timezone: str,
) -> None:
    """Fetch today's agenda (partner="shared") and upsert the snapshot row.

    `client` is typed loosely (not `PlannerClient`) to keep this module
    importable without app/planner/client.py's aiohttp dependency in a
    unit test that only wants to check the upsert -- a fake with a
    matching `get_agenda` (and, under PLANNER_HEALTH, `get_health`)
    coroutine is enough.

    PLANNER_HEALTH: one extra `get_health` call, made only when the flag
    is on. It is best-effort and strictly after the agenda call
    succeeds -- any exception it raises is logged and swallowed, never
    left to propagate and cost the agenda sync its own upsert (plan:
    "a get_health failure must not break the agenda sync").
    """
    today = clock_module.local_date(clock, timezone)
    payload = await client.get_agenda(
        settings, session, clock, date=today.isoformat(), timezone=timezone, days=1, partner="shared"
    )
    if settings.PLANNER_HEALTH:
        try:
            health = await client.get_health(
                settings, session, clock, date=today.isoformat(), days=HEALTH_SYNC_DAYS
            )
            payload = {**payload, "health": health}
        except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
            logger.warning("planner health fetch failed", extra={"event": type(exc).__name__})
    now = clock.now_utc()
    values = {"id": SNAPSHOT_ID, "fetched_at": now, "payload": payload}
    await session.execute(
        pg_insert(PlannerSnapshot).values(**values).on_conflict_do_update(
            index_elements=["id"], set_={"fetched_at": now, "payload": payload}
        )
    )
    await session.commit()
    logger.info("planner snapshot synced", extra={"event": "planner_sync"})


async def get_snapshot(session: AsyncSession) -> PlannerSnapshot | None:
    return await session.get(PlannerSnapshot, SNAPSHOT_ID)


def is_stale(snapshot: PlannerSnapshot | None, clock: Clock, max_age_min: int) -> bool:
    if snapshot is None:
        return True
    age = clock.now_utc() - snapshot.fetched_at
    return age > datetime.timedelta(minutes=max_age_min)


def _clip_title(title: str | None) -> str:
    if not title or not is_clean(title):
        return NO_TITLE
    title = title.strip()
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "…"
    return title or NO_TITLE


def _event_stamp(event: dict, timezone: str) -> str:
    if event.get("allDay"):
        return "весь день"
    start = event.get("start")
    if not start:
        return "?"
    try:
        moment = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    return clock_module.to_local(moment, timezone).strftime("%H:%M")


def _event_line(event: dict, timezone: str) -> str:
    stamp = _event_stamp(event, timezone)
    if event.get("owner") == "partner":
        if event.get("busy"):
            return f"{stamp} — занято {PARTNER_LABEL}"
        return f"{stamp} — «{_clip_title(event.get('title'))}» {PARTNER_LABEL}"
    return f"{stamp} — «{_clip_title(event.get('title'))}»"


def _task_line(task: dict) -> str:
    title = _clip_title(task.get("title"))
    tag = OVERDUE_TAG if task.get("overdue") else ""
    return f"· «{title}»{tag}"


def _format_duration(total_minutes: float) -> str:
    hours, minutes = divmod(int(total_minutes), 60)
    if hours:
        return f"{hours}ч{minutes:02d}м"
    return f"{minutes}м"


def _median_or_none(values: list[float]) -> float | None:
    if len(values) < HEALTH_BASELINE_MIN_SAMPLES:
        return None
    return statistics.median(values)


def _health_baseline(days: list[dict], today: str) -> dict[str, float | None]:
    """Median HRV / resting HR over the days *before* `today`.

    Each needs >=HEALTH_BASELINE_MIN_SAMPLES non-null values on its own
    -- a member with plenty of HRV readings but few resting-HR ones
    still gets an HRV comparison, just not a resting-HR one.
    """
    hrv = [d["hrvMs"] for d in days if d.get("date") != today and d.get("hrvMs") is not None]
    resting_hr = [
        d["restingHr"] for d in days if d.get("date") != today and d.get("restingHr") is not None
    ]
    return {"hrvMs": _median_or_none(hrv), "restingHr": _median_or_none(resting_hr)}


def _health_line(payload: dict, today: str) -> str | None:
    """The one PLANNER_HEALTH line, or None -- see the module docstring.

    Nothing here reads as a diagnosis: it names a duration, a share and
    a direction relative to the member's own recent median, never a
    "should"/"too little" judgement. Missing pieces are dropped one at
    a time rather than blanking the whole line, except last night's
    sleep itself -- with no sleep object at all there is nothing to
    report, so the whole line is omitted (plan: "no data for last
    night").
    """
    health = payload.get("health")
    if not health or not health.get("connected"):
        return None
    days = health.get("days") or []
    last = next((day for day in days if day.get("date") == today), None)
    if last is None:
        return None
    sleep = last.get("sleep")
    if not sleep:
        return None

    parts: list[str] = []
    minutes_asleep = sleep.get("minutesAsleep")
    if minutes_asleep is not None:
        duration = _format_duration(minutes_asleep)
        deep = sleep.get("deep")
        if deep is not None and minutes_asleep:
            parts.append(f"{duration} (глубокий {round(deep / minutes_asleep * 100)}%)")
        else:
            parts.append(duration)

    baseline = _health_baseline(days, today)
    hrv = last.get("hrvMs")
    baseline_hrv = baseline["hrvMs"]
    if hrv is not None and baseline_hrv is not None:
        if hrv < baseline_hrv:
            parts.append("HRV ниже твоей нормы")
        elif hrv > baseline_hrv:
            parts.append("HRV выше твоей нормы")

    resting_hr = last.get("restingHr")
    if resting_hr is not None:
        parts.append(f"пульс покоя {round(resting_hr)}")

    if not parts:
        return None
    return "Сон: " + ", ".join(parts)


def render_lines(
    snapshot: PlannerSnapshot | None,
    clock: Clock,
    timezone: str,
    *,
    max_age_min: int,
    max_items: int = 6,
) -> list[str]:
    """The now-block's "## План на сегодня" bullets, oldest fetch first.

    `[]` for a missing or stale snapshot -- see the module docstring.
    Events are listed before tasks, each capped so the combined line
    count never exceeds `max_items`; a day with many events simply does
    not show every task, which mirrors `get_agenda`'s own cap on its
    side (design review section 2.2).

    PLANNER_HEALTH's one health line, when there is one, is appended
    after that cap rather than competing for a slot in it -- it is not
    an event or a task, and "stale snapshot -> []" already covers it
    (it lives in the same payload, on the same fetched_at).
    """
    if is_stale(snapshot, clock, max_age_min):
        return []

    payload = snapshot.payload or {}
    today = clock_module.local_date(clock, timezone).isoformat()
    # `is_stale` only checks age, so right after local midnight a
    # snapshot fetched a few minutes ago (well within max_age_min) can
    # still hold yesterday's agenda -- the next PLANNER_SYNC has not
    # run yet. Check the calendar date it actually covers too.
    if payload.get("date") != today:
        return []
    lines = [_event_line(event, timezone) for event in payload.get("events", [])[:max_items]]
    for task in payload.get("tasks", []):
        if len(lines) >= max_items:
            break
        lines.append(_task_line(task))
    lines = lines[:max_items]
    health_line = _health_line(payload, today)
    if health_line is not None:
        lines.append(health_line)
    return lines


__all__ = [
    "SNAPSHOT_ID",
    "sync",
    "get_snapshot",
    "is_stale",
    "render_lines",
]
