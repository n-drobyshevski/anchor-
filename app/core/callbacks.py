"""Callbacks: "## Можно вспомнить" (phase-5 plan sections 3, 11a, 12 and 14).

At most one callback per scene. `select_callback` is a read-only query
over `memory`: it picks one `event` memory old enough and unused for
long enough to be worth bringing up again, and never writes anything --
the write half is `mark_delivered`, called by app/core/turn.py only
after the persona reply carrying the callback has actually gone out
(the same "after delivery" rule app/core/memory.py's `mark_used` and
app/core/voice.py's `remember_nickname` already follow).

**Why `event` only.** Plan section 11a says "pick one `event` memory" --
not identity, preference or rule. Those describe who the user *is* or
what they *want*; a callback is specifically "something that happened",
the shape of thing worth bringing up unprompted ("как прошло с врачом на
той неделе?"). Widening this to every kind would let the callback
compete with retrieval for the same facts, from the same header, for no
gain: `retrieve_memories` (app/core/memory.py) already puts every
active, relevant memory of any kind under "## Может быть важно" on the
turns where the user's own words call for it.

**One query, not two.** app/core/memory.py's `retrieve_memories` needs
a whole extra top-up path because it must *never* come back empty when
there is anything to retrieve at all (section 6's stated floor). A
callback has no such floor -- plan section 11a's fallback is simply
"pick the least recently used" candidate among the *same* filtered set,
not a different, looser one, so one query with a Python `if` on whether
anything scored above the threshold is enough; there is nothing here
that needs `_topup`'s separate filters or its own cap logic.

`func.word_similarity(Memory.text, user_text)` -- **the same argument
order app/core/memory.py's `retrieve_memories` flips to**, for the same
reason: see that module's docstring for the asymmetry measurements.
`user_text` here is exactly what `retrieve_memories` and
`retrieve_techniques` are already handed for the same turn, so there is
no new argument to thread through call sites -- app/core/turn.py passes
its own `user_text` straight through unchanged.

**Why `CALLBACK_MIN_SCORE = 0.2`, not `RETRIEVAL_MIN_SCORE`'s 0.15.**
Plan section 11a states 0.2 for callbacks specifically -- a different
number from ordinary retrieval's 0.15, and this module keeps its own
constant rather than reusing memory.py's, exactly as memory.py's own
`RETRIEVAL_MIN_SCORE` and `DEDUPE_MAX_SIMILARITY` are two different
constants for two different questions. A callback is a much more
visible, much rarer interruption than an ordinary retrieved fact folded
quietly into context, so the plan sets a higher bar before it fires on
a match rather than falling back to "just pick something old".
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.db.models import Memory, UserState

# Plan section 11a, verbatim: "if nothing scores above 0.2, pick the
# least recently used." A separate constant from app/core/memory.py's
# RETRIEVAL_MIN_SCORE (0.15) -- see the module docstring for why the
# two thresholds are deliberately different numbers, not one shared one.
CALLBACK_MIN_SCORE = 0.2

_EVENT_KIND = "event"

# UserState.id is pinned to 1 by a check constraint (ck_user_state_id_
# singleton); spelled out here rather than imported from app.core.state
# (STATE_ID), which this module must not import at all -- see
# app/core/voice.py's own docstring for the identical reasoning behind
# its own _STATE_ID.
_STATE_ID = 1


async def select_callback(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    *,
    user_text: str,
    scene_id: int | None,
    callback_scene: int | None,
    exclude_ids: tuple[int, ...] = (),
) -> tuple[int, str] | None:
    """Pick this turn's callback, or None. Read-only -- see module docstring.

    None immediately, before any query, in the two cases plan section
    11a's "first persona turn of a scene" rule reduces to: there is no
    scene yet (`scene_id is None` -- an outbound send building its
    prompt before a scene exists in practice, mirroring
    app/core/persona_context.py's own `gather()` docstring on the same
    edge case), or this scene already got its one callback
    (`callback_scene == scene_id`). Every other in-scene turn re-queries
    and gets the same answer back, but that repeated read-only query
    costs nothing and is simpler than caching "did this scene already
    decide" anywhere.

    `exclude_ids` keeps the callback memory out of the ordinary
    retrieved pool for the same turn (app/core/turn.py dedupes
    `retrieve_memories`'s result against whichever id this returns), so
    the same fact never appears under both "## Может быть важно" and
    "## Можно вспомнить" at once.
    """
    if scene_id is None or callback_scene == scene_id:
        return None

    now = clock.now_utc()
    min_age_cutoff = now - datetime.timedelta(days=settings.CALLBACK_MIN_AGE_DAYS)
    unused_cutoff = now - datetime.timedelta(days=settings.CALLBACK_UNUSED_DAYS)

    score = func.word_similarity(Memory.text, user_text).label("score")
    candidates = (
        select(Memory, score)
        .where(Memory.kind == _EVENT_KIND)
        .where(Memory.superseded_by.is_(None))
        .where(Memory.created_at < min_age_cutoff)
        .where(
            (Memory.last_used_at.is_(None)) | (Memory.last_used_at < unused_cutoff)
        )
    )
    if exclude_ids:
        candidates = candidates.where(Memory.id.notin_(exclude_ids))

    # The winner by similarity, above the threshold -- same NULLS FIRST
    # reasoning as app/core/memory.py's retrieve_memories: Postgres
    # defaults ASC to NULLS LAST, which would invert "never used sorts
    # first" for any tie the score ordering leaves.
    best_match = (
        candidates.where(score > CALLBACK_MIN_SCORE)
        .order_by(score.desc(), Memory.last_used_at.asc().nullsfirst(), Memory.id.asc())
        .limit(1)
    )
    row = (await session.execute(best_match)).first()
    if row is not None:
        memory = row[0]
        return memory.id, memory.text

    # Fallback: least recently used among the same filtered candidate
    # set, never used first, then oldest created -- plan section 11a's
    # "pick the least recently used", over the very pool the similarity
    # search just found nothing worth 0.2 in, not a looser one.
    fallback = candidates.order_by(
        Memory.last_used_at.asc().nullsfirst(), Memory.created_at.asc(), Memory.id.asc()
    ).limit(1)
    row = (await session.execute(fallback)).first()
    if row is None:
        return None
    memory = row[0]
    return memory.id, memory.text


async def mark_delivered(
    session: AsyncSession,
    *,
    scene_id: int,
    memory_id: int | None,
    clock: Clock,
) -> None:
    """Record that this scene's callback check happened (plan section 11a).

    Always sets `user_state.callback_scene = scene_id`, whether or not a
    candidate was found -- that is what makes the check run only once
    per scene (plan section 14: "at most one per scene"), on the first
    persona turn regardless of outcome. Only when `memory_id` is given
    (a callback was actually offered and delivered) does this also bump
    that memory's `last_used_at`, the same "after the turn is delivered"
    timing app/core/memory.py's `mark_used` and app/core/voice.py's
    `remember_nickname` already follow -- never before the reply carrying
    it is actually on its way to the user.

    Two targeted updates, not update_state(): this module may not
    import app.core.state at all (tests/test_autonomy_isolation.py), the
    same narrow-writer pattern app/core/voice.py's `remember_nickname`
    and app/core/orders.py's `_set_awaiting` already establish. Callers
    commit -- app/core/turn.py folds this into the same transaction that
    marks the assistant message sent, exactly like `mark_used`.
    """
    await session.execute(
        sql_update(UserState).where(UserState.id == _STATE_ID).values(callback_scene=scene_id)
    )
    if memory_id is not None:
        await session.execute(
            sql_update(Memory).where(Memory.id == memory_id).values(last_used_at=clock.now_utc())
        )
