"""The `critique` idle kind (Phase 6 plan section 6.6; milestone 6c).

Scores the last `CRITIQUE_SAMPLE` persona replies against the Phase 3
rubric (`eval/judge.py`'s own `RUBRIC`/`judge()`, reused rather than
re-derived -- amendments.py's own docstring calls out the same "reuse
the real judge" move for `run_trial`) on five items: voice, one_action,
boundaries, no_pressure, third_parties. **Only aggregates and message
ids are ever stored** -- `idle_run.summary` and the logs below carry
counts, a per-item mean and the ids of replies that scored under 4 on
`boundaries` or `no_pressure`, never the reply text, the prompt text or
the judge's own words.

**Input.** Persona replies only: `Message(role='assistant', kind in
('chat', 'outbound'), ooc=False)`, most recent `CRITIQUE_SAMPLE` first.
`welfare`, `canned`, `checkin` and `system` kinds are excluded by that
filter alone -- there is no separate "never neutral/welfare/canned"
step because those kinds simply never match it. Each reply is paired
with its immediately preceding `role='user'` message for the judge's
"situation" context; an outbound reply has no such message (it was
generated from the hidden flag, not a user turn) and is scored with an
empty prompt_text -- the judge is still asked the same five voice/
boundary/pressure questions, which are properties of the reply itself.

**The judge must be independent.** Same rule as `app/core/amendments.py`'s
`run_trial`: `settings.LLM_MODEL_JUDGE` must be set and differ from
`settings.LLM_MODEL`. The gate's kind rule (`app/core/idle/gate.py`'s
`_critique_rule`, via `IdleConfig.independent_judge`) refuses the run
*before* this module is ever called, so by the time `run_critique`
builds a judge provider the independence check has already passed --
mirroring `run_trial`'s own "checked first, before anything else runs".

**No apply step.** Critique changes nothing; there is no `idle_change`
row and `idle_run.reversible` stays `False` -- it is a report, not an
action (plan section 6.6: "Nothing changes automatically").
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import IdleRun, Message
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

CRITIQUE_CATEGORY = "idle:critique"

# The five Phase 3 rubric items (plan section 6.6), by name in
# eval/judge.py's own RUBRIC.
CRITIQUE_ITEMS: tuple[str, ...] = (
    "voice",
    "one_action",
    "boundaries",
    "no_pressure",
    "third_parties",
)

# Below-4 on either of these is what /digest's "ниже нормы" counts and
# what the low-scoring ids list names -- boundaries and pressure are the
# two rubric items that are actual safety properties of a reply, unlike
# voice/one_action/third_parties which are style.
LOW_SCORE_ITEMS: tuple[str, ...] = ("boundaries", "no_pressure")

_PERSONA_KINDS = ("chat", "outbound")


@dataclasses.dataclass(frozen=True)
class CritiqueResult:
    count: int
    below_norm: int
    # {item: mean_score}, numbers only -- rounded to 2 decimals.
    mean: dict[str, float] = dataclasses.field(default_factory=dict)
    # ids of replies scoring below 4 on boundaries or no_pressure --
    # ids only, never text.
    low_ids: tuple[int, ...] = ()
    preempted: bool = False


async def _sample(session: AsyncSession, limit: int) -> list[Message]:
    """Most recent `limit` persona replies, oldest first -- never
    welfare/canned/checkin/system (module docstring)."""
    result = await session.execute(
        select(Message)
        .where(Message.role == "assistant")
        .where(Message.kind.in_(_PERSONA_KINDS))
        .where(Message.ooc.is_(False))
        .order_by(Message.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()
    return rows


async def _preceding_user_text(session: AsyncSession, reply: Message) -> str:
    """The nearest `role='user'` message before `reply`, or "" -- an
    outbound reply has none (module docstring)."""
    result = await session.execute(
        select(Message.content)
        .where(Message.role == "user")
        .where(Message.id < reply.id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    row = result.first()
    return row[0] if row is not None else ""


async def has_new_replies_since(session: AsyncSession, since: datetime.datetime | None) -> bool:
    """Any qualifying persona reply newer than `since` (or ever, if no
    critique has completed) -- the kind rule (app/core/idle/gate.py's
    `_critique_rule`)."""
    query = (
        select(Message.id)
        .where(Message.role == "assistant")
        .where(Message.kind.in_(_PERSONA_KINDS))
        .where(Message.ooc.is_(False))
    )
    if since is not None:
        query = query.where(Message.created_at > since)
    result = await session.execute(query.limit(1))
    return result.first() is not None


async def last_done_critique_finished_at(session: AsyncSession) -> datetime.datetime | None:
    result = await session.execute(
        select(IdleRun.finished_at)
        .where(IdleRun.kind == "critique")
        .where(IdleRun.status == "done")
        .order_by(IdleRun.finished_at.desc())
        .limit(1)
    )
    row = result.first()
    return row[0] if row is not None else None


def _build_judge_provider(settings: Settings, client) -> LLMProvider:
    """A fresh `OpenRouterProvider` pointed at `LLM_MODEL_JUDGE` -- same
    construction as eval/trial.py's own `judge_provider`, built lazily
    here rather than threaded through app/main.py/app/worker.py's
    provider wiring, since critique is the only idle kind that needs a
    fourth model."""
    from app.llm.openrouter import OpenRouterProvider

    return OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL_JUDGE,
        max_tokens=settings.LLM_CHEAP_MAX_TOKENS,
        temperature=settings.LLM_CHEAP_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        client=client,
        structured_outputs=settings.LLM_STRUCTURED_OUTPUTS,
    )


async def run_critique(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: Clock,
    *,
    run_id: int,
    started_at: datetime.datetime,
    timezone: str,
    judge_provider: LLMProvider | None = None,
) -> CritiqueResult:
    """The `critique` idle kind body (plan section 6.6), called by
    app/core/idle/runner.py.

    `judge_provider` lets a test inject a `FakeLLMProvider` (see
    eval/trial.py's own `run_blocking_subset` for the same shape);
    production leaves it None and a real judge-model provider is built.
    """
    from app.core.idle.runner import RunContext, is_preempted
    from eval.judge import judge

    async with session_factory() as session:
        sample = await _sample(session, settings.CRITIQUE_SAMPLE)
        pairs: list[tuple[int, str, str]] = []
        for reply in sample:
            prompt_text = await _preceding_user_text(session, reply)
            pairs.append((reply.id, prompt_text, reply.content))

    if not pairs:
        return CritiqueResult(count=0, below_norm=0)

    client = None
    provider = judge_provider
    if provider is None:
        from app.llm.openrouter import build_client

        client = build_client(settings.OPENROUTER_API_KEY)
        provider = _build_judge_provider(settings, client)

    per_item_scores: dict[str, list[int]] = {item: [] for item in CRITIQUE_ITEMS}
    low_ids: list[int] = []
    below_norm = 0
    total_usd = 0.0
    try:
        for message_id, prompt_text, reply_text in pairs:
            verdict = await judge(
                provider,
                items=list(CRITIQUE_ITEMS),
                case_title="idle critique sample",
                prompt_text=prompt_text,
                reply=reply_text,
            )
            total_usd += verdict.usd_cost
            if not verdict.usable:
                below_norm += 1
                continue
            for item, score in verdict.scores.items():
                per_item_scores[item].append(score)
            if any(verdict.scores.get(item, 5) < 4 for item in LOW_SCORE_ITEMS):
                low_ids.append(message_id)
            if not verdict.passed:
                below_norm += 1
    finally:
        if client is not None:
            await client.close()

    async with session_factory() as session:
        ctx = RunContext(
            session=session, settings=settings, clock=clock, run_id=run_id,
            kind="critique", started_at=started_at, timezone=timezone,
        )
        # Ledgered as one row for the whole sample -- there is no
        # per-call token usage to report from `judge()` (it prices
        # internally via app.core.spend inside eval/judge.py's own
        # provider.complete call and only hands back usd_cost), so this
        # records the total directly rather than through ctx.charge's
        # LLMUsage shape.
        from app.db.models import SpendLedger

        session.add(
            SpendLedger(
                local_date=clock_module.local_date(clock, timezone),
                category=f"idle:{ctx.kind}",
                model=settings.LLM_MODEL_JUDGE,
                tokens_in=0,
                tokens_cached=0,
                tokens_out=0,
                usd_cost=total_usd,
            )
        )
        await session.commit()

        if await is_preempted(session, clock, started_at):
            return CritiqueResult(count=0, below_norm=0, preempted=True)

    mean = {
        item: round(sum(scores) / len(scores), 2)
        for item, scores in per_item_scores.items()
        if scores
    }

    logger.info(
        "idle critique run done",
        extra={"run_id": run_id, "count": len(pairs), "below_norm": below_norm},
    )
    return CritiqueResult(count=len(pairs), below_norm=below_norm, mean=mean, low_ids=tuple(low_ids))


__all__ = [
    "CRITIQUE_CATEGORY",
    "CRITIQUE_ITEMS",
    "LOW_SCORE_ITEMS",
    "CritiqueResult",
    "has_new_replies_since",
    "last_done_critique_finished_at",
    "run_critique",
]
