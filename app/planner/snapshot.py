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

PLANNER_FLOWS: `sync()` makes one more best-effort call, `get_flows
{date: today, groupBy: "tag", mine: true, limit: 3}`, strictly after the
agenda (and health) call and with the same swallow-and-log discipline;
the result is stored under `payload["flows"]`. `render_lines()` turns it
into at most one «Требует внимания: …» line (tasks with an attention
score >= ATTENTION_MIN_SCORE, three at most), and `/flows`
(app/tg/planner.py) renders the streams from the same cached payload.
Every flows field is read defensively (`flows_streams`, `flow_*`): the
planner's reply is outside this process, and a render path must never
raise on a missing or oddly typed field.
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
OVERDUE_WORD = "просрочено"

# get_health {days: 14} covers today plus a 13-day baseline window; a
# median needs at least this many non-null values to mean anything, so
# fewer than this and the comparison is simply omitted from the line.
HEALTH_BASELINE_MIN_SAMPLES = 5
HEALTH_SYNC_DAYS = 14

# PLANNER_FLOWS. A task needs attention from this score on (the planner
# scores 0..1, most urgent highest); the sync asks for 3 tasks a stream.
ATTENTION_MIN_SCORE = 0.5
FLOWS_SYNC_LIMIT = 3
ATTENTION_LINE_MAX = 3
ATTENTION_TITLE_MAX = 40
ATTENTION_PREFIX = "Требует внимания: "


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
    if settings.PLANNER_FLOWS:
        try:
            flows = await client.get_flows(
                settings,
                session,
                clock,
                date=today.isoformat(),
                group_by="tag",
                mine=True,
                limit=FLOWS_SYNC_LIMIT,
            )
            payload = {**payload, "flows": flows}
        except Exception as exc:  # noqa: BLE001 -- best-effort, same as health
            logger.warning("planner flows fetch failed", extra={"event": type(exc).__name__})
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


def _clip_title(title: str | None, limit: int = TITLE_MAX) -> str:
    if not title or not is_clean(title):
        return NO_TITLE
    title = title.strip()
    if len(title) > limit:
        title = title[: limit - 1].rstrip() + "…"
    return title or NO_TITLE


def clip_flow_title(title: object, limit: int = TITLE_MAX) -> str:
    """A flows title (task or stream label) made safe to show or prompt.

    Same rules as `_clip_title` (injection check, clip) on top of a
    type check and whitespace collapse: a flows title is rendered on one
    line, so a newline in it must not be able to start a line of its own.
    """
    if not isinstance(title, str):
        return NO_TITLE
    return _clip_title(" ".join(title.split()), limit)


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


# --- PLANNER_FLOWS ---------------------------------------------------------


def _as_int(value: object) -> int:
    """A non-negative count from whatever the planner sent; 0 otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value != value or value in (float("inf"), float("-inf")):
        return 0
    return max(0, int(value))


def flow_score(item: object) -> float:
    """`item["score"]` as a float, or 0.0 when absent, non-numeric or NaN."""
    if not isinstance(item, dict):
        return 0.0
    score = item.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return 0.0
    if score != score:
        return 0.0
    return float(score)


def flow_date(value: object) -> datetime.date | None:
    """A `yyyy-MM-dd` string as a date; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def flows_for(payload: object, today: str) -> dict | None:
    """The cached `flows` object if it is a dict covering `today`, else None."""
    if not isinstance(payload, dict):
        return None
    flows = payload.get("flows")
    if not isinstance(flows, dict) or flows.get("date") != today:
        return None
    return flows


def flows_streams(flows: dict) -> list[dict]:
    """The stream dicts of a flows object, malformed entries dropped."""
    streams = flows.get("streams")
    if not isinstance(streams, list):
        return []
    return [stream for stream in streams if isinstance(stream, dict)]


def stream_tasks(stream: dict) -> list[dict]:
    tasks = stream.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def stream_count(stream: dict, key: str) -> int:
    return _as_int(stream.get(key))


def attention_tasks(stream: dict) -> list[dict]:
    """The tasks of one stream that need attention now, order kept."""
    return [task for task in stream_tasks(stream) if flow_score(task) >= ATTENTION_MIN_SCORE]


def deadline_phrase(task_or_date: object, today: datetime.date) -> str | None:
    """A short Russian phrase for a task's deadline, or None without one.

    «просрочено» for an overdue task (the planner's own `overdue` flag
    or a due date before today), «сегодня», «завтра», «через N дн.» up
    to a week out, otherwise `ДД.ММ`.
    """
    overdue = False
    due: datetime.date | None
    if isinstance(task_or_date, dict):
        overdue = task_or_date.get("overdue") is True
        due = flow_date(task_or_date.get("dueDate"))
    else:
        due = flow_date(task_or_date)
    if due is None:
        return OVERDUE_WORD if overdue else None
    days = (due - today).days
    if overdue or days < 0:
        return OVERDUE_WORD
    if days == 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    if days <= 7:
        return f"через {days} дн."
    return due.strftime("%d.%m")


def _attention_line(payload: dict, today: str) -> str | None:
    """The one PLANNER_FLOWS line, or None when nothing needs attention."""
    flows = flows_for(payload, today)
    if flows is None:
        return None
    today_date = datetime.date.fromisoformat(today)
    picked: list[dict] = []
    seen: set[object] = set()
    for stream in flows_streams(flows):
        for task in attention_tasks(stream):
            # A task with two tags is in two streams; list it once.
            key = task.get("id") if isinstance(task.get("id"), str) and task.get("id") else id(task)
            if key in seen:
                continue
            seen.add(key)
            picked.append(task)
    if not picked:
        return None
    picked.sort(key=flow_score, reverse=True)  # stable: stream order breaks ties
    parts = []
    for task in picked[:ATTENTION_LINE_MAX]:
        title = clip_flow_title(task.get("title"), ATTENTION_TITLE_MAX)
        phrase = deadline_phrase(task, today_date)
        parts.append(f"{title} ({phrase})" if phrase else title)
    return ATTENTION_PREFIX + ", ".join(parts)


def render_lines(
    snapshot: PlannerSnapshot | None,
    clock: Clock,
    timezone: str,
    *,
    max_age_min: int,
    max_items: int = 6,
    flows: bool = True,
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

    PLANNER_FLOWS' «Требует внимания» line follows the same rule, after
    the health line. `flows=False` (callers pass `settings.PLANNER_FLOWS`)
    leaves it out even if a cached payload still holds flows -- the
    flag turned off must stop the titles reaching a prompt at once, not
    after the next sync overwrites the row.
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
    if flows:
        attention_line = _attention_line(payload, today)
        if attention_line is not None:
            lines.append(attention_line)
    return lines


__all__ = [
    "SNAPSHOT_ID",
    "sync",
    "get_snapshot",
    "is_stale",
    "render_lines",
    "clip_flow_title",
    "deadline_phrase",
    "flows_for",
    "flows_streams",
    "attention_tasks",
    "stream_count",
    "ATTENTION_MIN_SCORE",
]
