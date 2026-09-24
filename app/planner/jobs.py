"""The `PLANNER_SYNC` and `PLANNER_WRITE` job bodies.

**PLANNER_SYNC** (P2) makes no LLM call, like `app/research/sweeps.py`'s
daily sweep: it is a network call to the planner plus one upsert.
app/worker.py dispatches on `PLANNER_SYNC` the same way it dispatches
every other job kind, and a failure (network, or the planner returning
garbage) simply fails the job -- the normal retry/backoff machinery in
app/db/jobs.py applies, same as any other job kind.

The one exception is `PlannerAuthError`: retrying a sync against a
revoked grant cannot succeed, so this catches it, sends the user
exactly one "reconnect the planner" notice via
`app.planner.auth.mark_notice_sent`'s once-only gate, and lets the job
complete rather than fail-and-retry forever.

**PLANNER_WRITE** (P3) is the other half of app/planner/actions.py's
accept(): it loads the `planner_action` row accept() already flipped to
`accepted`, makes exactly one MCP write matching its `kind`, re-syncs
the snapshot so the new item shows up in the next `/plan`, and sends a
canned (out-of-character) confirmation. If the row is not `accepted`
-- already handled by a previous run of this same job, thanks to the
dedup key on enqueue -- it does nothing, which is what makes a replayed
job safe to run twice (design review P3's "done when": "tapping OK
creates exactly one item, including under a replayed job").
"""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.planner import actions, auth, snapshot
from app.planner.auth import PlannerAuthError
from app.planner.client import PlannerClient, PlannerToolError, PlannerUnavailable

logger = logging.getLogger(__name__)

PLANNER_SYNC = "planner_sync"
PLANNER_WRITE = "planner_write"

RELINK_NOTICE = (
    "Не получилось обновить план — доступ к планеру отозван. "
    "Набери /planner_link, чтобы подключить его заново."
)

WRITE_AUTH_FAILED_NOTICE = (
    "Не получилось записать в планер — доступ отозван. "
    "Набери /planner_link, чтобы подключить его заново."
)

# Sent by app/worker.py when a PLANNER_WRITE job exhausts its retries
# (MAX_ATTEMPTS) without ever raising PlannerAuthError -- e.g. the
# planner staying unreachable the whole time. Without this, a write
# abandoned this way leaves the action stuck `accepted` with the user
# never told it was not, in fact, written.
WRITE_ABANDONED_NOTICE = "Не записалось в планер — попробуй ещё раз."

# Canned, out-of-character confirmations (app/tg/proposals.py's ACCEPTED_TEXT
# is the model this follows): the persona never claims a write on its own,
# and this is the one place that gets to say it actually happened.
WRITE_OK_TASK = "Готово: задача «{title}» добавлена в планер."
WRITE_OK_EVENT = "Готово: событие «{title}» добавлено в планер."
WRITE_OK_DONE = "Отмечено: «{title}»."


async def run_planner_sync(
    session: AsyncSession,
    settings: Settings,
    client: PlannerClient,
    clock: Clock,
    *,
    timezone: str,
    bot: Bot | None = None,
    chat_id: int | None = None,
) -> None:
    """Refresh `planner_snapshot`. No provider call, no persona involved.

    On `PlannerAuthError` this does not re-raise: a revoked grant will
    not un-revoke itself on retry, and the point of the notice is to ask
    the user to act, not to spend the job queue's retry budget on
    something retrying cannot fix.
    """
    try:
        await snapshot.sync(session, settings, client, clock, timezone)
    except PlannerAuthError:
        if await auth.mark_notice_sent(session) and bot is not None and chat_id is not None:
            await bot.send_message(chat_id=chat_id, text=RELINK_NOTICE)
        return
    except (PlannerUnavailable, PlannerToolError) as exc:
        logger.warning("planner sync failed", extra={"event": type(exc).__name__})
        raise


async def _do_write(client: PlannerClient, settings: Settings, session: AsyncSession, clock: Clock, action, timezone: str) -> str:
    """Make the one MCP call `action.kind` names. Returns the confirmation text."""
    payload = action.payload
    # Derived from action.request_key, not action.id: an id can be
    # reused after a /delete purge (purge.py TRUNCATEs with RESTART
    # IDENTITY), which would otherwise collide with the planner's
    # unique index on (owner_id, client_request_id) and make a genuine
    # new write silently return the old, pre-purge row instead.
    request_id = f"anchor:{action.request_key}"

    if action.kind == actions.CREATE_TASK:
        await client.create_task(
            settings, session, clock,
            title=payload["title"], due_date=payload.get("due_date"),
            client_request_id=request_id,
        )
        return WRITE_OK_TASK.format(title=payload["title"])

    if action.kind == actions.CREATE_EVENT:
        await client.create_event(
            settings, session, clock,
            title=payload["title"], start=payload["start"], end=payload["end"],
            all_day=payload.get("all_day", False), is_private=settings.PLANNER_WRITE_PRIVATE,
            client_request_id=request_id, timezone=timezone,
        )
        return WRITE_OK_EVENT.format(title=payload["title"])

    if action.kind == actions.COMPLETE_TASK:
        await client.complete_task(settings, session, clock, task_id=payload["task_id"])
        return WRITE_OK_DONE.format(title=payload.get("title") or "задача")

    raise ValueError(f"unknown planner_action kind: {action.kind}")


async def run_planner_write(
    session: AsyncSession,
    settings: Settings,
    client: PlannerClient,
    clock: Clock,
    *,
    planner_action_id: int,
    timezone: str,
    bot: Bot | None = None,
    chat_id: int | None = None,
) -> None:
    """The `PLANNER_WRITE` job body. See the module docstring for the shape.

    `PlannerUnavailable` re-raises (the queue's ordinary retry/backoff
    applies -- a down planner is worth retrying). `PlannerAuthError`
    does not: exactly like run_planner_sync, a revoked grant will not
    un-revoke itself, so this sends the one-shot notice and completes
    the job rather than retrying forever. Any other `PlannerToolError`
    (a malformed call, a tool the planner rejected) also re-raises --
    unlike an auth failure, nothing here can say with confidence that a
    retry is futile, and the alternative is a silently dropped write.
    """
    action = await actions.get(session, planner_action_id)
    if action is None or action.status != actions.ACCEPTED:
        # Already written by an earlier run of this same job (the dedup
        # key collapsed the re-enqueue), or the action was never
        # accepted in the first place. Either way, nothing to do.
        return

    try:
        confirmation = await _do_write(client, settings, session, clock, action, timezone)
    except PlannerAuthError:
        await actions.mark_failed(session, planner_action_id)
        if await auth.mark_notice_sent(session) and bot is not None and chat_id is not None:
            await bot.send_message(chat_id=chat_id, text=WRITE_AUTH_FAILED_NOTICE)
        return
    except (PlannerUnavailable, PlannerToolError) as exc:
        logger.warning(
            "planner write failed",
            extra={"event": type(exc).__name__, "planner_action_id": planner_action_id},
        )
        raise

    # Flipped out of `accepted` right after the one MCP call succeeds,
    # before anything else that could crash -- a replayed job then hits
    # the `action.status != ACCEPTED` guard above and does nothing,
    # instead of calling MCP again and sending a second confirmation.
    await actions.mark_written(session, planner_action_id)

    # Best-effort: a stale snapshot after a successful write is a minor
    # inconvenience (the next PLANNER_SYNC catches it up), not worth
    # failing an otherwise-successful write over.
    try:
        await snapshot.sync(session, settings, client, clock, timezone)
    except (PlannerUnavailable, PlannerToolError, PlannerAuthError) as exc:
        logger.warning("post-write planner sync failed", extra={"event": type(exc).__name__})

    if bot is not None and chat_id is not None:
        await bot.send_message(chat_id=chat_id, text=confirmation)
    logger.info(
        "planner write completed",
        extra={"planner_action_id": planner_action_id, "kind": action.kind},
    )


__all__ = [
    "PLANNER_SYNC",
    "PLANNER_WRITE",
    "RELINK_NOTICE",
    "WRITE_AUTH_FAILED_NOTICE",
    "WRITE_ABANDONED_NOTICE",
    "WRITE_OK_TASK",
    "WRITE_OK_EVENT",
    "WRITE_OK_DONE",
    "run_planner_sync",
    "run_planner_write",
]
