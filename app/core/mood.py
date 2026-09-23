"""Mood: a pure, code-computed tone color, never a punishment (phase-5 plan section 4).

`mood()` is a five-rule pure function -- no session, no clock read of
its own, nothing but arithmetic over values the caller hands it. It is
never stored: app/core/persona_context.py recomputes it every persona
turn, and app/tg/router.py's `/state` recomputes it again the same way,
so there is exactly one place ("the rules") rather than a cached value
that can drift from them.

**Mood colors tone only.** It renders as one line in the "## Сейчас"
block -- `Настроение: <mood> — <gloss>` -- and nothing else. It never
touches intensity, focus, the due action, streak, persona_active, or
any outbound gate; §12 states that as an invariant and
tests/test_autonomy_isolation.py enforces it by AST, the same way
tests/test_research_isolation.py already does for app/research/.

**There is no punitive mood.** `MOODS` names exactly the plan's four
values, and none of them is "angry", "disappointed" or "strict" -- the
closest this gets is настороже ("собранно, без упрёков"), which its own
gloss tells the model not to use as license to scold. Rule 1 exists
specifically to *force* a calm mood down over anything sharper: a
welfare trigger or intensity <= 2 always wins, regardless of what the
check-in history would otherwise say.

**Rule order is precedence, not a menu.** The five rules are checked in
order and the first match wins -- a user who just tripped the welfare
check but also has a 3-day streak of done actions still gets ровный,
never доволен, because rule 1 is checked first. `mood()` is exhaustive:
rule 5 is `else: ровный`, so there is no path that returns nothing.

`load_mood_facts()` is the only part of this module that touches the
database, and it is read-only -- it queries `checkin` and `message`,
never writes either. Its one subtlety is *which* user message counts as
"the last one": by the time a persona turn's prompt is built,
`user_state.last_user_msg_at` has already been bumped to now (app/
worker.py's `record_inbound()` runs before the router even sees the
update), so rule 4 cannot read that column -- every turn would compute
"the user just wrote", which is true and useless. Instead it asks the
`message` table for the newest role='user' row *excluding this turn's
own*, the same `update_id.is_distinct_from()` guard app/core/prompt.py
uses for the transcript query and for the same reason (see that
module's docstring): a bare `!=` would also drop every historical row
whose update_id is NULL.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import Checkin, Message, UserState

Mood = Literal["доволен", "ровный", "настороже", "ждёт"]

MOODS: tuple[Mood, ...] = ("доволен", "ровный", "настороже", "ждёт")

# Plan section 4, verbatim. Tone color only -- see the module docstring.
GLOSS: dict[Mood, str] = {
    "доволен": "теплее, коротко похвали по делу",
    "ровный": "спокойно",
    "настороже": "собранно, без упрёков",
    "ждёт": "спокойно, без давления",
}

# Rule thresholds, named rather than inlined so the precedence reads as
# English in mood() below.
_WELFARE_FORCE_HOURS = 24
_CALM_INTENSITY_CEILING = 2
_STREAK_FOR_DOVOLEN = 3
_NO_USER_MSG_HOURS = 24

# checkin.due_result values that count as "did not do it" for rule 3.
# Spelled out here rather than imported from app.core.checkin: that
# module is off limits to this one (tests/test_autonomy_isolation.py --
# it is a state writer, and mood.py must not be able to reach one even
# transitively through an import it has no real need of).
_DUE_MISSED = ("partial", "no")
_DUE_EXCLUDED = (None, "none")


@dataclasses.dataclass(frozen=True)
class MoodFacts:
    """Everything `mood()` needs that is not already on `user_state`.

    `state.welfare_at`, `state.intensity` and `state.streak` are read
    straight off the `UserState` row `mood()` is given -- duplicating
    them onto this dataclass as well would only invite the two copies
    to drift. What lives here is exactly what a plain column read
    cannot answer: it takes a query to know.

    `due_results` is newest first, with NULL and 'none' already
    filtered out by `load_mood_facts()` -- plan section 4's "ignore
    'none'" -- so `mood()` never has to re-check that here.
    """

    due_results: tuple[str, ...] = ()
    missed_evening_yesterday: bool = False
    last_user_msg_before_now: datetime.datetime | None = None


def mood(state: UserState, facts: MoodFacts, now: datetime.datetime) -> Mood:
    """The five rules, in order. Pure: no I/O, no clock of its own.

    `state` needs only `.welfare_at`, `.intensity` and `.streak` --
    tests may pass anything with those three attributes, not
    necessarily a row that came out of the database.
    """
    # Rule 1: a recent welfare trigger or a low intensity forces calm,
    # regardless of anything else below. This is the one rule that
    # exists to *override* a sharper mood the other rules would
    # otherwise compute, so it is checked first and nothing after it
    # can undo it.
    welfare_recent = (
        state.welfare_at is not None
        and now - state.welfare_at < datetime.timedelta(hours=_WELFARE_FORCE_HOURS)
    )
    if welfare_recent or state.intensity <= _CALM_INTENSITY_CEILING:
        return "ровный"

    # Rule 2: a real streak, capped off by yesterday's due action
    # actually being done.
    if (
        state.streak >= _STREAK_FOR_DOVOLEN
        and facts.due_results
        and facts.due_results[0] == "done"
    ):
        return "доволен"

    # Rule 3: a missed evening check-in, or the last two due results
    # both landing on partial/no. Neither implies the other, so this is
    # `or`, not `and`.
    last_two_missed = len(facts.due_results) >= 2 and all(
        result in _DUE_MISSED for result in facts.due_results[:2]
    )
    if facts.missed_evening_yesterday or last_two_missed:
        return "настороже"

    # Rule 4: silence. `None` (never wrote at all) counts the same as
    # "too long ago" -- both mean there is nothing recent to point to.
    if facts.last_user_msg_before_now is None or (
        now - facts.last_user_msg_before_now >= datetime.timedelta(hours=_NO_USER_MSG_HOURS)
    ):
        return "ждёт"

    # Rule 5: the fallback. Exhaustive -- every earlier rule either
    # returned or fell through, so this always fires when nothing else
    # matched.
    return "ровный"


async def load_mood_facts(
    session: AsyncSession,
    state: UserState,
    clock: Clock,
    *,
    exclude_update_id: int | None = None,
) -> MoodFacts:
    """Read-only. Never writes `checkin`, `message`, or anything else.

    `exclude_update_id` should be the current turn's `update_id` on a
    chat/check-in turn (see the module docstring on why), and `None` on
    an outbound send, which has no current user message to exclude.
    """
    today_local = clock_module.local_date(clock, state.timezone)
    yesterday = today_local - datetime.timedelta(days=1)

    due_result_rows = await session.execute(
        select(Checkin.due_result)
        .where(Checkin.due_result.is_not(None))
        .where(Checkin.due_result != "none")
        .order_by(Checkin.local_date.desc())
        .limit(2)
    )
    due_results = tuple(due_result_rows.scalars().all())

    yesterday_row = await session.execute(
        select(Checkin.id).where(Checkin.local_date == yesterday).limit(1)
    )
    has_yesterday = yesterday_row.scalar_one_or_none() is not None

    # "at least one earlier check-in exists" -- a first-day user with no
    # history at all must not start настороже just because yesterday
    # has no row (plan section 15's decision).
    earlier_row = await session.execute(
        select(Checkin.id).where(Checkin.local_date < yesterday).limit(1)
    )
    has_earlier = earlier_row.scalar_one_or_none() is not None
    missed_evening_yesterday = (not has_yesterday) and has_earlier

    last_msg_stmt = select(Message.created_at).where(Message.role == "user")
    # Conditional, exactly as app/core/prompt.py's transcript query is:
    # `is_distinct_from(None)` is true for every non-null row, so an
    # unconditional filter here would (wrongly) exclude every
    # historical message whose update_id happens to be NULL.
    if exclude_update_id is not None:
        last_msg_stmt = last_msg_stmt.where(
            Message.update_id.is_distinct_from(exclude_update_id)
        )
    last_msg_stmt = last_msg_stmt.order_by(Message.id.desc()).limit(1)
    last_msg_row = await session.execute(last_msg_stmt)
    last_user_msg_before_now = last_msg_row.scalar_one_or_none()

    return MoodFacts(
        due_results=due_results,
        missed_evening_yesterday=missed_evening_yesterday,
        last_user_msg_before_now=last_user_msg_before_now,
    )
