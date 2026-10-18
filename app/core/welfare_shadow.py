"""The welfare shadow: a decision model asked the welfare question, and never obeyed.

When WELFARE_SHADOW_MODEL is set, every turn the welfare classifier
runs on (app/core/welfare.py) also goes to a typed-decision model --
TypeSafe's Jev, through OpenRouter's Decisions API -- as one `choice`
question over the same three levels and the same transcript. Both
answers are written side by side to `welfare_shadow`, and nothing else
happens: the verdict, the keyword backstop, the reply and the persona
switch are all the classifier's, exactly as when the shadow is off.

**Why a shadow and not a swap.** This is the check that decides whether
someone in real distress gets the persona or a warm out-of-character
message, and every number published about the decision model so far is
the vendor's or a stranger's. The shadow collects the only number that
matters here -- how often the two disagree on this user's own turns,
and in which direction -- before anyone decides whether the decision
model should ever hold the verdict.

**Why keep `p_real`.** The classifier is told to choose `real` when torn
(welfare.py's "fail toward real"). A choice question has no such rule:
it reports a probability for every option, and the most probable one
wins. So the row keeps the probability the decision model put on
`real`, whatever it picked -- "fail toward real" for a decision model
is a threshold on that number, and the rows are what it would be set
from.

**Fails silent.** A timeout or an error is an `outcome`, never an
exception: the shadow cannot fail a turn, slow one past
WELFARE_SHADOW_TIMEOUT_SECONDS, or change a verdict. The row write is
best-effort like app/core/safety_events.py's, for the same reason.

**Privacy.** The decision model sees the classifier's input and nothing
more, under `zdr` (app/llm/openrouter.py's OpenRouterDecisionProvider).
The row and the log line carry levels, numbers and times -- the same
things welfare.py already logs -- and never any text.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, NamedTuple, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core import spend, welfare
from app.core.clock import Clock
from app.db.models import SpendLedger, WelfareShadow
from app.llm.provider import Choice, DecisionProvider, DecisionResponse

logger = logging.getLogger(__name__)

# The spend_ledger category for the shadow's calls: billed, so recorded,
# and apart from `welfare` so the comparison's cost can be read alone.
LEDGER_CATEGORY = "welfare_shadow"

QUESTION = "level"

# The classifier prompt's own definitions (welfare.CLASSIFIER_PROMPT),
# split into the three options a choice question names. Its tie rule
# ("if torn, choose real") has no place in a choice question; see the
# module docstring on `p_real`.
CHOICE = Choice(
    instructions=(
        "Это переписка пользователя с ролевым ботом-челленджем. Определи по "
        "последнему сообщению пользователя, с учётом предыдущих, испытывает ли "
        "он настоящий дистресс ВНЕ ролевой игры/челленджа."
    ),
    criteria={
        welfare.NONE: "Признаков дистресса нет: обычный разговор, отчёт, шутка, планы.",
        welfare.SCENE: (
            "Трудность, недовольство или напряжение, которые явно часть игры/челленджа."
        ),
        welfare.REAL: (
            "Признаки реальной беды: безнадёжность, паника, упоминание самоповреждения "
            "или суицида, реальный жизненный кризис, прямое «мне реально плохо», «стоп, "
            "это уже не игра». Любое упоминание самоповреждения — это сюда."
        ),
    },
)

OUTCOMES = (welfare.OK, welfare.TIMEOUT, welfare.ERROR)

T = TypeVar("T")


class Shadow(NamedTuple):
    """What the decision model answered, or how it failed to."""

    outcome: str
    level: str | None
    confidence: float | None
    p_real: float | None
    ms: int
    response: DecisionResponse | None


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


async def timed(awaitable: Awaitable[T]) -> tuple[T, int]:
    """`awaitable`'s result and how long it took, in milliseconds. For
    the classifier's side of the comparison, timed the same way."""
    started = time.monotonic()
    result = await awaitable
    return result, _elapsed_ms(started)


async def classify(
    decider: DecisionProvider,
    settings: Settings,
    context: list,
    user_text: str,
) -> Shadow:
    """Ask the decision model. Never raises."""
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            decider.decide(welfare.transcript(context, user_text), {QUESTION: CHOICE}),
            timeout=settings.WELFARE_SHADOW_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("welfare shadow timed out", extra={"event": "TimeoutError"})
        return Shadow(welfare.TIMEOUT, None, None, None, _elapsed_ms(started), None)
    except Exception as exc:  # noqa: BLE001 - the shadow fails silent, by design
        logger.warning("welfare shadow failed", extra={"event": type(exc).__name__})
        return Shadow(welfare.ERROR, None, None, None, _elapsed_ms(started), None)

    ms = _elapsed_ms(started)
    answer = response.answers[QUESTION]
    # The numbers go to the row, not the log: app/log.py keeps only its
    # SAFE_EXTRA_KEYS, and a confidence is not one of them.
    logger.info("welfare shadow classified", extra={"event": answer.choice, "latency_ms": ms})
    return Shadow(
        welfare.OK,
        answer.choice,
        answer.confidence,
        answer.probabilities.get(welfare.REAL, 0.0),
        ms,
        response,
    )


async def beside(
    primary_call: Awaitable[welfare.Classification],
    decider: DecisionProvider,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    clock: Clock,
    timezone: str,
    context: list,
    user_text: str,
) -> welfare.Classification:
    """Run the classifier and the shadow together; return the classifier's
    result, untouched.

    app/core/turn.py puts this where `welfare.classify(...)` was, inside
    the gather it already has with the reply. The shadow's timeout is
    shorter than the classifier's, so the two together end no later than
    the classifier alone could.
    """
    (primary, primary_ms), shadow = await asyncio.gather(
        timed(primary_call), classify(decider, settings, context, user_text)
    )
    await record(
        sessionmaker,
        settings,
        clock=clock,
        timezone=timezone,
        primary=primary,
        primary_ms=primary_ms,
        shadow=shadow,
    )
    return primary


async def record(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    clock: Clock,
    timezone: str,
    primary: welfare.Classification,
    primary_ms: int,
    shadow: Shadow,
) -> None:
    """Write the comparison row, and the shadow call's ledger row when it
    was billed, in their own transaction. Never raises."""
    answered = primary.outcome == welfare.OK
    local_date = clock_module.local_date(clock, timezone)
    try:
        async with sessionmaker() as session:
            if shadow.response is not None:
                cost = spend.priced(shadow.response.usage, settings, model=shadow.response.model)
                session.add(
                    SpendLedger(
                        local_date=local_date,
                        category=LEDGER_CATEGORY,
                        model=shadow.response.model,
                        tokens_in=shadow.response.usage.input_tokens,
                        tokens_cached=shadow.response.usage.cached_tokens,
                        tokens_out=shadow.response.usage.output_tokens,
                        usd_cost=cost.usd,
                        cost_source=cost.source,
                    )
                )
            session.add(
                WelfareShadow(
                    local_date=local_date,
                    primary_outcome=primary.outcome,
                    primary_level=primary.verdict.level if answered else None,
                    primary_confidence=primary.verdict.confidence if answered else None,
                    primary_ms=primary_ms,
                    shadow_model=settings.WELFARE_SHADOW_MODEL,
                    shadow_outcome=shadow.outcome,
                    shadow_level=shadow.level,
                    shadow_confidence=shadow.confidence,
                    shadow_p_real=shadow.p_real,
                    shadow_ms=shadow.ms,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - observability never fails the turn
        logger.warning("welfare shadow not recorded", extra={"event": type(exc).__name__})
