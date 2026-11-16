"""The judge checked against the user: labels, agreement, advisory items
(anchor-eval-reliability-plan.md sections 5.1-5.4, milestone E1c).

**What it guarantees.**

- **A round rates one real reply, chosen by critique's rule.** `pick_reply`
  takes a persona reply (`app/core/idle/critique.py`'s
  `persona_reply_filter`: kind `chat` or `outbound`, in character) from
  the last `LOOKBACK` (14 days), at random, that has no label yet and is
  not in a scene holding a welfare message. The welfare-scene limit is
  this module's own: critique's sample does not apply it (its isolation
  test samples a reply beside a welfare row), but a reply written while
  a welfare message was in play is not one to put in front of the user
  as an exercise.
- **The judge sees what critique's judge sees.** The reply paired with
  the nearest preceding user message (`preceding_user_text`), the five
  core items (`CORE_ITEMS`, critique's own `CRITIQUE_ITEMS`), the judge
  provider critique builds, `JUDGE_EVIDENCE` honoured the same way. The
  judge must be independent (`app/llm/models.py`'s `judge_dependence_now`)
  or the round is refused before anything is called.
- **No text is stored or logged.** `judge_label` holds a message id, an
  item name, the user's boolean, the judge's score (NULL when the
  judgement was unusable) and the judge model. The judge's quotes never
  leave `eval/judge.py`'s `judge()`. Log lines carry ids and counts. This
  module is the only one that names `judge_label` and `judge_notice`.
- **A day's rounds are capped** by `JUDGE_LABELS_PER_DAY` (a round is one
  `voice` row: every round writes exactly one row per core item), and the
  call is refused at the daily money cap and ledgered under its own
  category, `JUDGE_CATEGORY`.

**Agreement and reliability** (section 5.3) are computed over the labels
made with the *current effective judge model* and a usable judgement
only: a judge switch starts the statistics again, as `eval_history`'s
signal does for a model switch. The judge agrees on an item when
`user_ok == (judge_score >= PASS_SCORE)`. An item is unreliable when it
has at least `JUDGE_MIN_LABELS` such labels and agreement under
`JUDGE_MIN_AGREEMENT`, or when the user flagged it at least
`MIN_USER_FLAGS` (5) times and the judge caught fewer than half of those:
most replies are fine on most items, so a judge that never flags
anything would otherwise look reliable.

**Advisory items** (section 5.4) are the unreliable ones minus
`boundaries`, which never becomes advisory: the deterministic checks and
the user are the gate there, and /judge warns instead. `unreliable_items`
is read from the live database before each trial (app/worker.py) and
canary (app/core/idle/runner.py) and handed down as a set of names; the
throwaway database a trial runs in never sees a label.

**One notice per transition.** `judge_notice` stores, per item, whether
the user was last told it is advisory. `transition` compares that with
the current advisory set, and `mark_announced` records what was sent, in
the same transaction as nothing else, right after the send; a restart
therefore repeats nothing. app/tg/judge.py sends it when the bot may
speak (`may_report_now`).

This module imports no aiogram type and no app.core.state, so the idle
runner may import it. Every time comes from the injected `Clock`.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.idle import critique
from app.core.spend import check_cap
from app.db.models import JudgeLabel, JudgeNotice, Message, SpendLedger
from app.llm import models as llm_models
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

CORE_ITEMS: tuple[str, ...] = critique.CRITIQUE_ITEMS
NEVER_ADVISORY = "boundaries"
JUDGE_CATEGORY = "judge_calibration"
LOOKBACK = datetime.timedelta(days=14)
# The second unreliability rule (section 5.3): this many flags of the
# user's on an item, and the judge caught fewer than half of them.
MIN_USER_FLAGS = 5
# The `judge()` call's case title, the same role critique's plays.
CASE_TITLE = "judge calibration sample"

# Refusal codes for a round (`refusal`).
DAY_CAP = "day_cap"
SPEND_CAP = "spend_cap"
NOTHING = "nothing"
GONE = "gone"


def pass_score() -> int:
    from eval.judge import PASS_SCORE

    return PASS_SCORE


# --- picking the reply ---------------------------------------------------------


def _in_welfare_scene():
    """True for a message whose scene holds a welfare message. A message
    with no scene is in none."""
    other = Message.__table__.alias("welfare_row")
    return (
        select(other.c.id)
        .where(other.c.scene_id == Message.scene_id)
        .where(other.c.kind == "welfare")
        .exists()
    )


def _labelled():
    return select(JudgeLabel.id).where(JudgeLabel.message_id == Message.id).exists()


def _eligible(clock: Clock) -> tuple:
    return (
        *critique.persona_reply_filter(),
        Message.created_at >= clock.now_utc() - LOOKBACK,
        ~_in_welfare_scene(),
        ~_labelled(),
    )


async def pick_reply(
    session: AsyncSession, clock: Clock, *, exclude: tuple[int, ...] = ()
) -> Message | None:
    """One unlabelled qualifying reply at random (module docstring), or
    None. `exclude` holds ids just skipped, so «Пропустить» moves on."""
    query = select(Message).where(*_eligible(clock))
    if exclude:
        query = query.where(Message.id.not_in(exclude))
    result = await session.execute(query.order_by(func.random()).limit(1))
    return result.scalars().first()


async def eligible_reply(session: AsyncSession, clock: Clock, message_id: int) -> Message | None:
    """The reply `message_id` if it may still be rated now, else None (a
    forged id, a reply already rated, one that left the window)."""
    result = await session.execute(
        select(Message).where(Message.id == message_id).where(*_eligible(clock))
    )
    return result.scalars().first()


# --- the round -------------------------------------------------------------------


def _day_bounds(clock: Clock, timezone: str) -> tuple[datetime.datetime, datetime.datetime]:
    today = clock_module.local_date(clock, timezone)
    start = clock_module.combine_local(today, datetime.time(0, 0), timezone)
    end = clock_module.combine_local(today + datetime.timedelta(days=1), datetime.time(0, 0), timezone)
    return start, end


async def rounds_today(session: AsyncSession, clock: Clock, timezone: str) -> int:
    """Rounds stored this local day: one `voice` row per round."""
    start, end = _day_bounds(clock, timezone)
    result = await session.execute(
        select(func.count())
        .select_from(JudgeLabel)
        .where(JudgeLabel.item == CORE_ITEMS[0])
        .where(JudgeLabel.created_at >= start)
        .where(JudgeLabel.created_at < end)
    )
    return int(result.scalar_one())


async def refusal(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str
) -> str | None:
    """Why a round may not run now, or None: the judge's dependence code
    (`app/llm/models.py`), `DAY_CAP` or `SPEND_CAP`. Checked when a card
    is offered and again on submit, before the judge is called."""
    dependence = llm_models.judge_dependence_now(settings)
    if dependence is not None:
        return dependence
    if await rounds_today(session, clock, timezone) >= settings.JUDGE_LABELS_PER_DAY:
        return DAY_CAP
    if await check_cap(session, settings, clock, timezone):
        return SPEND_CAP
    return None


@dataclasses.dataclass(frozen=True)
class RoundResult:
    """What one submitted round came to: ids and counts only.
    `disagreed` names the items the judge saw differently; empty, and
    `usable` False, when the judgement was unusable."""

    message_id: int
    usable: bool
    agreed: int
    total: int
    disagreed: tuple[str, ...] = ()


async def stored_round(session: AsyncSession, message_id: int) -> RoundResult | None:
    """The round already stored for `message_id`, or None: a replayed or
    doubled «Готово» re-renders this instead of paying again."""
    rows = (
        await session.execute(select(JudgeLabel).where(JudgeLabel.message_id == message_id))
    ).scalars().all()
    if not rows:
        return None
    return _result(message_id, {row.item: row.user_ok for row in rows}, {
        row.item: row.judge_score for row in rows if row.judge_score is not None
    })


def _result(message_id: int, user_ok: dict[str, bool], scores: dict[str, int]) -> RoundResult:
    items = [item for item in CORE_ITEMS if item in user_ok]
    if not scores:
        return RoundResult(message_id=message_id, usable=False, agreed=0, total=len(items))
    threshold = pass_score()
    disagreed = tuple(
        item for item in items if item in scores and user_ok[item] != (scores[item] >= threshold)
    )
    return RoundResult(
        message_id=message_id, usable=True, agreed=len(items) - len(disagreed),
        total=len(items), disagreed=disagreed,
    )


async def submit(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    *,
    message_id: int,
    flagged: frozenset[str],
    timezone: str,
    judge_provider: LLMProvider | None = None,
) -> RoundResult | str:
    """Store one round (section 5.1 step 3): the user's marks (`flagged`
    are the items marked wrong), the judge's scores on the same reply,
    and a ledger row for the call, in one commit. Returns the result, or
    a refusal code (`refusal`'s, `GONE` for a reply that may not be
    rated). A round already stored is returned as it was, unpaid.

    `judge_provider` lets a test inject a fake; production builds
    critique's judge provider and closes its client afterwards."""
    from eval.judge import judge

    existing = await stored_round(session, message_id)
    if existing is not None:
        return existing
    reply = await eligible_reply(session, clock, message_id)
    if reply is None:
        return GONE
    why = await refusal(session, settings, clock, timezone)
    if why is not None:
        return why

    prompt_text = await critique.preceding_user_text(session, reply)
    reply_text = reply.content
    judge_model = llm_models.effective_model(settings, "judge")

    client = None
    provider = judge_provider
    if provider is None:
        from app.llm.openrouter import build_client

        client = build_client(settings.OPENROUTER_API_KEY)
        provider = critique.build_judge_provider(settings, client)
    try:
        verdict = await judge(
            provider,
            items=list(CORE_ITEMS),
            case_title=CASE_TITLE,
            prompt_text=prompt_text,
            reply=reply_text,
            require_evidence=settings.JUDGE_EVIDENCE == "required",
        )
    finally:
        if client is not None:
            await client.close()

    now = clock.now_utc()
    scores = dict(verdict.scores) if verdict.usable else {}
    user_ok = {item: item not in flagged for item in CORE_ITEMS}
    for item in CORE_ITEMS:
        session.add(
            JudgeLabel(
                message_id=message_id,
                item=item,
                user_ok=user_ok[item],
                judge_score=scores.get(item),
                judge_model=judge_model,
                created_at=now,
            )
        )
    session.add(
        SpendLedger(
            local_date=clock_module.local_date(clock, timezone),
            category=JUDGE_CATEGORY,
            model=judge_model,
            tokens_in=0,
            tokens_cached=0,
            tokens_out=0,
            usd_cost=decimal.Decimal(str(verdict.usd_cost)),
        )
    )
    await session.commit()
    result = _result(message_id, user_ok, scores)
    logger.info(
        "judge label stored",
        extra={"count": result.agreed, "event": "judge_label"},
    )
    return result


# --- agreement -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ItemStats:
    """Section 5.3's four cells for one item, over usable labels of the
    current judge model."""

    both_ok: int = 0
    both_bad: int = 0
    only_user: int = 0  # the user flagged it, the judge passed it
    only_judge: int = 0  # the judge failed it, the user did not flag it

    @property
    def count(self) -> int:
        return self.both_ok + self.both_bad + self.only_user + self.only_judge

    @property
    def agreed(self) -> int:
        return self.both_ok + self.both_bad

    @property
    def agreement(self) -> float | None:
        return self.agreed / self.count if self.count else None

    @property
    def user_flags(self) -> int:
        return self.both_bad + self.only_user


def is_unreliable(stats: ItemStats, settings: Settings) -> bool:
    """Either rule of section 5.3 (module docstring)."""
    if stats.count >= settings.JUDGE_MIN_LABELS and stats.agreed < settings.JUDGE_MIN_AGREEMENT * stats.count:
        return True
    return stats.user_flags >= MIN_USER_FLAGS and 2 * stats.both_bad < stats.user_flags


async def agreement(session: AsyncSession, settings: Settings) -> dict[str, ItemStats]:
    """Per core item, the four cells over the labels made with the
    current effective judge model and a usable judgement."""
    judge_model = llm_models.effective_model(settings, "judge")
    threshold = pass_score()
    judge_ok = JudgeLabel.judge_score >= threshold
    rows = await session.execute(
        select(JudgeLabel.item, JudgeLabel.user_ok, judge_ok, func.count())
        .where(JudgeLabel.judge_model == judge_model)
        .where(JudgeLabel.judge_score.is_not(None))
        .group_by(JudgeLabel.item, JudgeLabel.user_ok, judge_ok)
    )
    cells: dict[str, dict[str, int]] = {item: {} for item in CORE_ITEMS}
    for item, user_ok, judge_passed, count in rows.all():
        if item not in cells:
            continue
        key = {
            (True, True): "both_ok",
            (False, False): "both_bad",
            (False, True): "only_user",
            (True, False): "only_judge",
        }[(bool(user_ok), bool(judge_passed))]
        cells[item][key] = int(count)
    return {item: ItemStats(**values) for item, values in cells.items()}


async def unreliable_items(session: AsyncSession, settings: Settings) -> frozenset[str]:
    """The advisory set for a trial or a canary (section 5.4): every
    unreliable core item except `boundaries`."""
    stats = await agreement(session, settings)
    return frozenset(
        item for item, item_stats in stats.items()
        if item != NEVER_ADVISORY and is_unreliable(item_stats, settings)
    )


# --- the transition notice -------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Transition:
    """Items to announce: newly advisory, and advisory no more. Both in
    CORE_ITEMS order."""

    advisory: tuple[str, ...] = ()
    reliable: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.advisory or self.reliable)


async def _announced(session: AsyncSession) -> set[str]:
    rows = await session.execute(select(JudgeNotice.item).where(JudgeNotice.advisory.is_(True)))
    return {row[0] for row in rows.all()}


async def transition(session: AsyncSession, settings: Settings) -> Transition:
    """What changed since the last notice (module docstring)."""
    current = await unreliable_items(session, settings)
    announced = await _announced(session)
    return Transition(
        advisory=tuple(item for item in CORE_ITEMS if item in current and item not in announced),
        reliable=tuple(item for item in CORE_ITEMS if item in announced and item not in current),
    )


async def mark_announced(session: AsyncSession, clock: Clock, change: Transition) -> None:
    """Record that `change` was told to the user. Commits."""
    now = clock.now_utc()
    for item, advisory in [*((i, True) for i in change.advisory), *((i, False) for i in change.reliable)]:
        row = await session.get(JudgeNotice, item)
        if row is None:
            session.add(JudgeNotice(item=item, advisory=advisory, announced_at=now))
        else:
            row.advisory = advisory
            row.announced_at = now
    await session.commit()
    logger.info(
        "judge notice recorded",
        extra={"count": len(change.advisory) + len(change.reliable), "event": "judge_notice"},
    )


__all__ = [
    "CORE_ITEMS",
    "DAY_CAP",
    "GONE",
    "ItemStats",
    "JUDGE_CATEGORY",
    "LOOKBACK",
    "MIN_USER_FLAGS",
    "NEVER_ADVISORY",
    "NOTHING",
    "RoundResult",
    "SPEND_CAP",
    "Transition",
    "agreement",
    "eligible_reply",
    "is_unreliable",
    "mark_announced",
    "pick_reply",
    "refusal",
    "rounds_today",
    "stored_round",
    "submit",
    "transition",
    "unreliable_items",
]
