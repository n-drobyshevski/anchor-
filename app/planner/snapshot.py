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
"""

from __future__ import annotations

import datetime
import logging

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
    matching `get_agenda` coroutine is enough.
    """
    today = clock_module.local_date(clock, timezone)
    payload = await client.get_agenda(
        settings, session, clock, date=today.isoformat(), timezone=timezone, days=1, partner="shared"
    )
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
    """
    if is_stale(snapshot, clock, max_age_min):
        return []

    payload = snapshot.payload or {}
    lines = [_event_line(event, timezone) for event in payload.get("events", [])[:max_items]]
    for task in payload.get("tasks", []):
        if len(lines) >= max_items:
            break
        lines.append(_task_line(task))
    return lines[:max_items]


__all__ = [
    "SNAPSHOT_ID",
    "sync",
    "get_snapshot",
    "is_stale",
    "render_lines",
]
