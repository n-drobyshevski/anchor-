"""`planner_action`: the confirmation gate for a planner write (P3).

Modelled on app/core/proposal.py's shape -- create() then a button-only
accept()/reject() -- but deliberately its **own** table and its own
module, not a reuse of `proposal`. `proposal.create()` expires any
other outstanding proposal, and a planner action must not silently
expire, or be expired by, an unrelated extractor proposal sitting in
the chat (design review, table 1: "reusing proposal for planner writes
is harmless" -- it is not). Several planner actions may also be pending
at once (a `/task` and a `/event` typed back to back), which the
extractor's one-pending rule does not allow for either.

`accept()` makes no network call. It only flips the row to `accepted`
and enqueues a `PLANNER_WRITE` job under a dedup key derived from the
row's own id -- so a replayed callback (the worker re-runs an update
after a crash, exactly as app/core/proposal.py's docstring describes)
finds the row already decided and enqueues nothing a second time, and
even if it somehow reached enqueue_job twice, the dedup key would
collapse the two into one job.

`complete_task` actions (the `/done` list's buttons) are created and
accepted in the same call, by the caller (app/tg/planner.py) -- picking
a task from a list the user was just shown *is* the confirmation, so
there is no separate pending card for it. `create_task` and
`create_event` actions (from `/task` and `/event`, whose parse can be
ambiguous) go through the ordinary pending -> accepted/rejected/expired
lifecycle instead.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.jobs import enqueue_job
from app.db.models import PlannerAction

logger = logging.getLogger(__name__)

_MIDNIGHT = datetime.time(0, 0)
_ONE_DAY = datetime.timedelta(days=1)

__all__ = [
    "PlannerAction",
    "PENDING",
    "ACCEPTED",
    "REJECTED",
    "EXPIRED",
    "CREATE_TASK",
    "CREATE_EVENT",
    "COMPLETE_TASK",
    "KINDS",
    "PLANNER_WRITE_DEDUP_PREFIX",
    "create",
    "get",
    "set_message_id",
    "accept",
    "reject",
    "count_today",
    "pending",
]

PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
EXPIRED = "expired"

CREATE_TASK = "create_task"
CREATE_EVENT = "create_event"
COMPLETE_TASK = "complete_task"

KINDS = (CREATE_TASK, CREATE_EVENT, COMPLETE_TASK)

# app/planner/jobs.py's run_planner_write reads the same id back out of
# this key; kept as one string constant so the two modules cannot drift.
PLANNER_WRITE_DEDUP_PREFIX = "planner_write:"


def _write_dedup_key(action_id: int) -> str:
    return f"{PLANNER_WRITE_DEDUP_PREFIX}{action_id}"


async def create(session: AsyncSession, clock: Clock, *, kind: str, payload: dict) -> PlannerAction:
    """Insert a pending planner_action. No expiry of any other row -- see
    the module docstring for why this differs from proposal.create().

    `created_at` is stamped from `clock` explicitly, overriding the
    column's `server_default=func.now()` -- count_today() below reads
    this column as logic (the daily write cap), and app/core/clock.py's
    own docstring is explicit that a `server_default=func.now()` stamp
    is for audit trails a test's FrozenClock cannot see, never for
    something a rule is computed from.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown planner_action kind: {kind}")

    action = PlannerAction(kind=kind, payload=payload, created_at=clock.now_utc())
    session.add(action)
    await session.commit()
    await session.refresh(action)
    logger.info("planner_action created", extra={"planner_action_id": action.id, "kind": kind})
    return action


async def get(session: AsyncSession, action_id: int) -> PlannerAction | None:
    return await session.get(PlannerAction, action_id)


async def set_message_id(session: AsyncSession, action_id: int, message_id: int) -> None:
    action = await session.get(PlannerAction, action_id)
    if action is not None:
        action.tg_message_id = message_id
        await session.commit()


async def accept(session: AsyncSession, clock: Clock, action_id: int) -> PlannerAction | None:
    """Confirm a pending action: flip it to `accepted` and enqueue its write.

    Returns None (and enqueues nothing) if the row is not pending --
    already decided, or gone -- which is what makes a replayed `pa:y:`
    callback, or a replayed worker update, idempotent (design review
    P3's "done when": "tapping OK creates exactly one item, including
    under a replayed job").
    """
    action = await session.get(PlannerAction, action_id)
    if action is None or action.status != PENDING:
        return None

    action.status = ACCEPTED
    action.decided_at = clock.now_utc()
    await session.commit()
    await session.refresh(action)

    # The literal kind string, not an import of app.planner.jobs.PLANNER_WRITE:
    # jobs.py imports this module (to load the action a claimed job names),
    # so importing jobs.py back here would be circular. tests/test_planner_actions.py
    # pins that this string equals jobs.PLANNER_WRITE.
    await enqueue_job(
        session,
        "planner_write",
        {"planner_action_id": action.id},
        dedup_key=_write_dedup_key(action.id),
    )
    logger.info("planner_action accepted", extra={"planner_action_id": action_id, "kind": action.kind})
    return action


async def reject(session: AsyncSession, clock: Clock, action_id: int) -> PlannerAction | None:
    """Mark a pending action rejected. Returns None if it is not pending."""
    action = await session.get(PlannerAction, action_id)
    if action is None or action.status != PENDING:
        return None
    action.status = REJECTED
    action.decided_at = clock.now_utc()
    await session.commit()
    await session.refresh(action)
    logger.info("planner_action rejected", extra={"planner_action_id": action_id, "kind": action.kind})
    return action


async def pending(session: AsyncSession) -> list[PlannerAction]:
    """Every still-`pending` row, oldest first.

    P4 (app/core/turn.py): backs the "ждёт подтверждения" now-block
    note, so the persona is never mid-conversation with a card sitting
    unanswered and no idea it exists. Ordered by id (== creation order,
    ids are a strictly increasing serial) so a user who typed two
    `/task`s back to back sees them in the order they asked.
    """
    result = await session.execute(
        select(PlannerAction).where(PlannerAction.status == PENDING).order_by(PlannerAction.id)
    )
    return list(result.scalars().all())


async def count_today(session: AsyncSession, clock: Clock, timezone: str) -> int:
    """How many planner_action rows were created "today" (local date).

    Backs `PLANNER_MAX_WRITES_PER_DAY` (app/tg/planner.py): counted at
    creation time, not at accept -- a card the user never confirms still
    cost a parse (and, on the LLM fallback path, a provider call), and
    a day of unconfirmed cards is exactly the spam the cap exists to
    bound. `/done`'s directly-accepted actions count the same way, since
    they insert a row here too.

    The window is `[local midnight today, local midnight tomorrow)`,
    computed with ZoneInfo and compared against `created_at` as aware
    UTC instants -- the same DST-correct local-day boundary
    app/core/clock.py's combine_local uses, not a UTC-day approximation.
    """
    day_start = clock_module.combine_local(
        clock_module.local_date(clock, timezone), _MIDNIGHT, timezone
    )
    day_end = clock_module.combine_local(
        clock_module.local_date(clock, timezone) + _ONE_DAY, _MIDNIGHT, timezone
    )
    result = await session.execute(
        select(func.count())
        .select_from(PlannerAction)
        .where(PlannerAction.created_at >= day_start, PlannerAction.created_at < day_end)
    )
    return result.scalar_one()
