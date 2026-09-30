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

**Lens attribution (L5; anchor-lens-plan.md section 10, the L5 spec's
section 4).** With `LENS_ENABLED` on, critique also records, for each
sampled reply at `t = reply.created_at`, which lens notes stood behind
the grounded changes that were in the persona prompt at `t`: an adopted
persona amendment (`PersonaAmendment` -> `ReviewProposal` by
`proposal_id`, live while `activated_at <= t < revoked_at`), a
review-proposed standing order (`StandingOrder` -> `ReviewProposal` by
`review_proposal_id`, status `active` or `retired`, live while
`decided_at <= t < retired_at`; a counter-proposal is the user's own
text and carries no link) and a notebook entry the idle reflect
grounded (`NotebookEntry`, live while `updated_at <= t < closed_at` --
an entry updated after `t` is missed rather than misattributed, since
its old text is gone). A source counts only when its `lens_note_ids`
is non-empty.

This is attribution, not selection: critique gets no catalog, no lens
text, no `app.vault.lens` and no `app.core.lens_select` (plan section
10; tests/test_idle_isolation.py and tests/test_vault_notes_isolation.py
pin that). It reads the ids through `app.db.models` only, and the judge
call is unchanged -- the judge never learns a reply was grounded. The
ids keep notes that have since left the lens: a note's id is what the
source recorded, and `/export` is where the user reads them.

The two keys (`lens_note_ids`, `lens_grounded`) reach `idle_run.summary`
through `CritiqueResult.summary_extra()` only when the lens is on and at
least one sampled reply had a source, so with the lens off (or nothing
grounded) the summary and the digest stay byte-identical. Neither key is
in app/log.py's `SAFE_EXTRA_KEYS`: the runner's "idle run done" line
spreads the summary into `extra`, and the formatter drops both, so note
ids never reach a log Claude Code reads. `review._week_critique_
aggregates` still sums only `count` and `below_norm`.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import (
    IdleRun,
    Message,
    NotebookEntry,
    PersonaAmendment,
    ReviewProposal,
    StandingOrder,
)
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
    # L5 (module docstring, "Lens attribution"): the lens note ids behind
    # the grounded sources live at the sampled replies, first-seen order,
    # and how many sampled replies had at least one such source. Both
    # stay empty/0 with the lens off -- run_critique never looks then.
    lens_note_ids: tuple[int, ...] = ()
    lens_grounded: int = 0

    def summary_extra(self) -> dict:
        """The two lens keys for `idle_run.summary`, or `{}` when no
        sampled reply was grounded (which includes the lens being off:
        run_critique only attributes with `LENS_ENABLED` on). `{}` keeps
        the summary byte-identical to a pre-L5 run's."""
        if self.lens_grounded <= 0:
            return {}
        return {"lens_note_ids": list(self.lens_note_ids), "lens_grounded": self.lens_grounded}


@dataclasses.dataclass(frozen=True)
class _LensSource:
    """One grounded change's live window in the persona prompt: from
    `start` up to (not including) `end`, or open-ended while `end` is
    None."""

    start: datetime.datetime
    end: datetime.datetime | None
    note_ids: tuple[int, ...]

    def live_at(self, t: datetime.datetime) -> bool:
        return self.start <= t and (self.end is None or t < self.end)


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


async def _lens_sources(
    session: AsyncSession, *, earliest: datetime.datetime, latest: datetime.datetime
) -> list[_LensSource]:
    """Every grounded source whose live window meets `[earliest,
    latest]` (module docstring, "Lens attribution"), amendments, then
    orders, then notebook entries, each by id -- the order the ids are
    first seen in. Ids only: no text column is selected."""
    sources: list[_LensSource] = []

    def overlaps(start_col, end_col):
        return (
            start_col.is_not(None),
            start_col <= latest,
            or_(end_col.is_(None), end_col > earliest),
        )

    amendments = await session.execute(
        select(PersonaAmendment.activated_at, PersonaAmendment.revoked_at, ReviewProposal.lens_note_ids)
        .join(ReviewProposal, PersonaAmendment.proposal_id == ReviewProposal.id)
        .where(func.cardinality(ReviewProposal.lens_note_ids) > 0)
        .where(*overlaps(PersonaAmendment.activated_at, PersonaAmendment.revoked_at))
        .order_by(PersonaAmendment.id)
    )
    orders = await session.execute(
        select(StandingOrder.decided_at, StandingOrder.retired_at, ReviewProposal.lens_note_ids)
        .join(ReviewProposal, StandingOrder.review_proposal_id == ReviewProposal.id)
        .where(StandingOrder.status.in_(("active", "retired")))
        .where(func.cardinality(ReviewProposal.lens_note_ids) > 0)
        .where(*overlaps(StandingOrder.decided_at, StandingOrder.retired_at))
        .order_by(StandingOrder.id)
    )
    entries = await session.execute(
        select(NotebookEntry.updated_at, NotebookEntry.closed_at, NotebookEntry.lens_note_ids)
        .where(func.cardinality(NotebookEntry.lens_note_ids) > 0)
        .where(*overlaps(NotebookEntry.updated_at, NotebookEntry.closed_at))
        .order_by(NotebookEntry.id)
    )
    for result in (amendments, orders, entries):
        for start, end, note_ids in result.all():
            sources.append(_LensSource(start=start, end=end, note_ids=tuple(note_ids or ())))
    return sources


async def _lens_attribution(
    session: AsyncSession, reply_times: list[datetime.datetime]
) -> tuple[tuple[int, ...], int]:
    """(`lens_note_ids`, `lens_grounded`) for replies sent at
    `reply_times`, oldest first (module docstring, "Lens attribution"):
    the union of the live sources' note ids in first-seen order, and the
    number of replies with at least one live source."""
    if not reply_times:
        return (), 0
    sources = await _lens_sources(session, earliest=min(reply_times), latest=max(reply_times))
    seen: dict[int, None] = {}
    grounded = 0
    for t in reply_times:
        live = [source for source in sources if source.live_at(t)]
        if not live:
            continue
        grounded += 1
        for source in live:
            for note_id in source.note_ids:
                seen.setdefault(int(note_id), None)
    return tuple(seen), grounded


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
        # L5: read-only, ids only, and never passed to the judge (module
        # docstring, "Lens attribution"). Skipped with the lens off, so
        # such a run issues exactly the queries it did before L5.
        lens_note_ids: tuple[int, ...] = ()
        lens_grounded = 0
        if settings.LENS_ENABLED and sample:
            lens_note_ids, lens_grounded = await _lens_attribution(
                session, [reply.created_at for reply in sample]
            )

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
    return CritiqueResult(
        count=len(pairs), below_norm=below_norm, mean=mean, low_ids=tuple(low_ids),
        lens_note_ids=lens_note_ids, lens_grounded=lens_grounded,
    )


__all__ = [
    "CRITIQUE_CATEGORY",
    "CRITIQUE_ITEMS",
    "LOW_SCORE_ITEMS",
    "CritiqueResult",
    "has_new_replies_since",
    "last_done_critique_finished_at",
    "run_critique",
]
