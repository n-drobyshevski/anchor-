"""Scenes: splitting the conversation into sessions, and summarizing them.

Phase-2 plan section 5. A scene is one continuous stretch of
conversation. Silence longer than SCENE_IDLE_HOURS ends it; the next
inbound message starts a new one, and the one just closed is queued for
a cheap-model summary so its content survives as five sentences instead
of thirty transcript rows.

Two halves live here:

- The lifecycle (`ensure_open_scene`, `bump_message_count`), called from
  core/turn.py on every inbound user message.
- The `summarize_scene` job body (`run_summarize_scene`), called from
  app/worker.py when that job is claimed.

They are in one module because closing a scene and summarizing it are
the same concern, and because the enqueue lives inside the close: a
scene cannot be closed without its summary being queued, which makes
"every closed scene is queued exactly once" structural rather than a
rule a caller has to remember. Idempotence comes from the job's
dedup_key ('scene:<id>'), so offering the same close twice is free.

**Privacy.** The summary input is filtered to ooc=false rows whose kind
is in SUMMARIZABLE_KINDS. Welfare turns (kind='welfare', and ooc=true)
and canned replies are therefore excluded by both filters
independently, which is deliberate belt-and-braces: welfare content
must never reach a summary (plan sections 10 and 13), and one filter
failing must not be enough to leak it.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.spend import check_cap, priced
from app.db.jobs import enqueue_job
from app.db.models import Message, Scene, SpendLedger
from app.llm.provider import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

SUMMARIZE_SCENE = "summarize_scene"
SUMMARY_CATEGORY = "summary"

# Fewer than this many summarizable messages and the scene is not worth
# a model call; summary stays NULL (plan section 5, last line).
MIN_MESSAGES_FOR_SUMMARY = 3

# Only these kinds are ever shown to the summarizer. See the module
# docstring on why this is paired with an ooc=false filter rather than
# trusted alone.
# 3b: a proactive message is part of the session it opened, so it
# belongs in that session's summary. The plan does not say either
# way; the alternative is a summary that reads as if the user
# started every conversation, which is no longer true.
SUMMARIZABLE_KINDS = ("chat", "checkin", "outbound")

# Plan section 5, verbatim.
SUMMARY_PROMPT = (
    "Кратко (до 5 предложений) опиши эту сессию: о чём говорили, что пользователь "
    "пообещал или сделал, чем закончилось. Только факты из диалога. Не упоминай "
    "здоровье, кризисы и личные данные третьих лиц."
)

# How the dialogue is rendered into the single user message the
# summarizer sees. Role-tagged plain text rather than real user/
# assistant turns on purpose: handed a genuine transcript, a roleplay
# model continues the roleplay instead of describing it.
_ROLE_LABELS = {"user": "Пользователь", "assistant": "Anchor"}


async def get_open_scene(session: AsyncSession) -> Scene | None:
    """The currently open scene (ended_at IS NULL), or None."""
    result = await session.execute(
        select(Scene).where(Scene.ended_at.is_(None)).order_by(Scene.id.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _last_message_at(session: AsyncSession, scene_id: int) -> datetime.datetime | None:
    result = await session.execute(
        select(func.max(Message.created_at)).where(Message.scene_id == scene_id)
    )
    return result.scalar_one()


async def ensure_open_scene(
    session: AsyncSession, clock: Clock, *, idle_hours: int
) -> int:
    """Return the id of the scene this message belongs to, opening one if needed.

    If the open scene's last message is older than `idle_hours`, that
    scene is closed with `ended_at` set to its last message's timestamp
    -- not to now. The scene ended when the conversation stopped, not
    when the user came back, and `ended_at = now()` would record every
    gap as part of the scene it interrupted.

    Closing enqueues the summary job before the new scene is opened.
    Commits once, at the end: the close, the enqueue and the open are
    one transaction, so a crash mid-way cannot leave a closed scene
    with no queued summary.
    """
    open_scene = await get_open_scene(session)

    if open_scene is not None:
        last_at = await _last_message_at(session, open_scene.id)
        if last_at is None:
            # Opened but never used -- reuse it rather than leaking an
            # empty scene every time the worker restarts mid-turn.
            return open_scene.id
        if clock.now_utc() - last_at < datetime.timedelta(hours=idle_hours):
            return open_scene.id

        open_scene.ended_at = last_at
        await session.flush()
        await enqueue_job(
            session,
            SUMMARIZE_SCENE,
            {"scene_id": open_scene.id},
            dedup_key=f"scene:{open_scene.id}",
        )
        logger.info("scene closed", extra={"scene_id": open_scene.id})

    scene = Scene(started_at=clock.now_utc())
    session.add(scene)
    await session.commit()
    await session.refresh(scene)
    logger.info("scene opened", extra={"scene_id": scene.id})
    return scene.id


# How many closed scenes' summaries reach the prompt (plan section 7).
PROMPT_SUMMARY_COUNT = 3


async def recent_summaries(session: AsyncSession, limit: int = PROMPT_SUMMARY_COUNT) -> list[str]:
    """The last `limit` closed scenes' summaries, oldest first (plan section 7).

    Three filters, not one. `summary IS NOT NULL` matters because NULL
    is a *valid terminal state* here, not a pending one -- a scene with
    fewer than MIN_MESSAGES_FOR_SUMMARY messages is deliberately never
    summarized (see run_summarize_scene), so omitting this filter would
    put empty bullets in every prompt. `ended_at IS NOT NULL` excludes
    the scene currently in progress. The id tiebreak gives a total
    order, since two scenes can share an ended_at.

    Newest-first in SQL then reversed in Python, matching the transcript
    query in app/core/prompt.py: "the last N, oldest first" cannot be
    expressed as one ORDER BY.

    Worth knowing how far this reaches: a summary is cheap-model output
    that now recurs in *every* in-character prompt until it ages out, so
    SUMMARY_PROMPT's exclusions above protect every future turn, not
    just one stored row.
    """
    result = await session.execute(
        select(Scene.summary)
        .where(Scene.ended_at.is_not(None))
        .where(Scene.summary.is_not(None))
        .order_by(Scene.ended_at.desc(), Scene.id.desc())
        .limit(limit)
    )
    rows = [row for row in result.scalars().all()]
    rows.reverse()
    return rows


async def bump_message_count(session: AsyncSession, scene_id: int, n: int = 1) -> None:
    """Increment scene.message_count. Does not commit -- the caller does.

    An UPDATE ... SET x = x + n rather than a read-modify-write, so the
    count is correct even though this runs in the same transaction as
    the message insert it accompanies.
    """
    await session.execute(
        sql_update(Scene).where(Scene.id == scene_id).values(message_count=Scene.message_count + n)
    )


async def summarizable_messages(session: AsyncSession, scene_id: int) -> list[Message]:
    """The scene's messages that may be shown to the summarizer, oldest first."""
    result = await session.execute(
        select(Message)
        .where(Message.scene_id == scene_id)
        .where(Message.ooc.is_(False))
        .where(Message.kind.in_(SUMMARIZABLE_KINDS))
        .order_by(Message.id)
    )
    return list(result.scalars().all())


def render_dialogue(messages: list[Message]) -> str:
    """Role-tagged plain text. See _ROLE_LABELS on why not real turns."""
    return "\n".join(
        f"{_ROLE_LABELS.get(row.role, row.role)}: {row.content}" for row in messages
    )


class Deferred(Exception):
    """Raised by a job body that is not done and must be re-run later.

    Not a failure: app/worker.py turns this into jobs.defer_job(), which
    returns the row to pending at `run_after` without consuming the
    retry budget.
    """

    def __init__(self, run_after: datetime.datetime) -> None:
        super().__init__("job deferred")
        self.run_after = run_after


async def run_summarize_scene(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    *,
    scene_id: int,
    clock: Clock,
    timezone: str,
) -> None:
    """The `summarize_scene` job body (plan section 5).

    Idempotent three ways, because a job can be re-claimed after a
    crash: an already-summarized scene returns immediately, a scene with
    too few messages is a no-op that leaves summary NULL, and the model
    call itself is only reached when neither of those holds.

    Over the daily cap the job is deferred to the next local midnight
    rather than run or dropped (plan section 12).

    **5b: `notebook_reflect` is enqueued at the end of every path below
    that has a real scene** -- the already-summarized early return, the
    too-short no-op, and the normal model-call path alike -- never on
    "scene is None". Reflection needs the summary as input, so it
    cannot be enqueued from `ensure_open_scene` where the close happens;
    it belongs here, right after (or, on the idempotent path, "after"
    in the sense that the summary already exists). The too-short case
    still enqueues: `run_notebook_reflect` re-checks the message count
    itself and no-ops, which costs nothing and means this function does
    not have to duplicate that rule. The dedup key (`nb:<scene_id>`)
    collapses every one of these into at most one queued job per scene,
    so a replayed summarize job is free. A local import, not a
    module-level one: app/core/notebook.py imports several names from
    this module, and importing it back here at module scope would be a
    cycle.
    """
    scene = await session.get(Scene, scene_id)
    if scene is None:
        return

    from app.core.notebook import NOTEBOOK_REFLECT

    async def _enqueue_reflect() -> None:
        await enqueue_job(
            session, NOTEBOOK_REFLECT, {"scene_id": scene_id}, dedup_key=f"nb:{scene_id}"
        )

    if scene.summary is not None:
        await _enqueue_reflect()
        await session.commit()
        return

    messages = await summarizable_messages(session, scene_id)
    if len(messages) < MIN_MESSAGES_FOR_SUMMARY:
        logger.info(
            "scene too short to summarize",
            extra={"scene_id": scene_id, "count": len(messages)},
        )
        await _enqueue_reflect()
        await session.commit()
        return

    if await check_cap(session, settings, clock, timezone):
        run_after = clock_module.next_local_midnight(clock, timezone)
        logger.info("scene summary deferred by cap", extra={"scene_id": scene_id})
        raise Deferred(run_after)

    response = await provider.complete(
        [
            LLMMessage(role="system", content=SUMMARY_PROMPT),
            LLMMessage(role="user", content=render_dialogue(messages)),
        ],
        conversation_id=f"anchor-scene-{scene_id}",
    )

    cost = priced(response.usage, settings, model=response.model)
    usd_cost = cost.usd
    scene.summary = response.text.strip()
    session.add(
        SpendLedger(
            local_date=clock_module.local_date(clock, timezone),
            category=SUMMARY_CATEGORY,
            model=response.model,
            tokens_in=response.usage.input_tokens,
            tokens_cached=response.usage.cached_tokens,
            tokens_out=response.usage.output_tokens,
            usd_cost=usd_cost,
            cost_source=cost.source,
        )
    )
    await _enqueue_reflect()
    await session.commit()
    logger.info(
        "scene summarized",
        extra={
            "scene_id": scene_id,
            "count": len(messages),
            "tokens_in": response.usage.input_tokens,
            "tokens_out": response.usage.output_tokens,
            "usd_cost": str(usd_cost),
        },
    )
