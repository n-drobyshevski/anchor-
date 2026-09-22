"""Durable memory: storage, retrieval, dedupe and supersede (plan sections 4 and 6).

A fact is never edited in place. A correction is a new row, and the row
it replaces is pointed at it via `superseded_by`, so what the bot used
to believe stays readable. "Active" means `superseded_by IS NULL`
throughout this module.

This module knows nothing about Telegram. app/tg/memory.py owns the
commands and their Russian strings; app/core/turn.py owns *when*
retrieval happens and when the results are marked used.

**Two deviations from plan section 6, both about `word_similarity`.**

Section 6 specifies `word_similarity(user_text, memory.text)` with a
cutoff of 0.3. `word_similarity(a, b)` is asymmetric: the whole of `a`
must be matched by some continuous extent of `b`. With the user's
message as `a`, the score therefore falls as the message gets longer --
no short memory can contain all of a long message -- so retrieval
quality would be inversely proportional to how much the user typed.
Measured against a memory "пользователь живёт в Лилле":

    user text                                  section 6   flipped
    "Лилль"                                      0.667      0.179
    "я сегодня думал про Лилль"                  0.154      0.194
    "слушай, я сегодня ехал домой ... Лилль"     0.075      0.194

So the arguments are flipped to `word_similarity(memory.text,
user_text)` -- "does this message contain something that looks like
this memory" -- which is stable in message length.

That flip moves the whole score range down, which is why the cutoff
moves with it. Measured over four memories and six messages, the
correct memory ranked first every time, with true positives at
0.194-0.317 and unrelated pairs at or below 0.089 (an entirely
off-topic message peaked at 0.049). RETRIEVAL_MIN_SCORE = 0.15 sits in
that gap. Section 6's 0.3 was calibrated for the un-flipped
orientation and rejects five of six true positives under this one.

Both numbers are worth re-measuring once there is a real corpus;
tests/test_memory.py asserts the ranking and the separation, not the
floats themselves.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import delete, func, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Memory, PendingMemory

logger = logging.getLogger(__name__)

# Tuning constants, deliberately not Settings: a deploy should not be
# able to set the dedupe threshold to 0 and start duplicating every
# fact. Same call as MIN_MESSAGES_FOR_SUMMARY in app/core/scene.py.
RETRIEVAL_MIN_SCORE = 0.15  # see the module docstring for the measurements
DEDUPE_MAX_SIMILARITY = 0.6  # plan section 6, unchanged
TOPUP_MIN_MATCHES = 3
TOPUP_KINDS = ("identity", "rule")

# Below this many characters, skip retrieval entirely. "ок", "да", "ага"
# produce three or four padded trigrams that match almost anything at a
# respectable score, so a short acknowledgement would inject noise into
# the prompt rather than context.
MIN_QUERY_CHARS = 8

KINDS = ("identity", "preference", "event", "rule", "technique")


async def pinned_memories(session: AsyncSession, limit: int) -> list[Memory]:
    """All active pinned memories, newest first, capped at `limit` (plan section 7)."""
    result = await session.execute(
        select(Memory)
        .where(Memory.superseded_by.is_(None))
        .where(Memory.pinned.is_(True))
        .order_by(Memory.created_at.desc(), Memory.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_pinned(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Memory)
        .where(Memory.superseded_by.is_(None))
        .where(Memory.pinned.is_(True))
    )
    return result.scalar_one()


async def retrieve_memories(session: AsyncSession, user_text: str, limit: int) -> list[Memory]:
    """Active, unpinned memories relevant to `user_text` (plan section 6).

    Pinned memories are excluded: they are already injected as their own
    prompt block, and including them here would render duplicate bullets
    and double-count `use_count` for a single injection.

    Two statements rather than one. The top-up is conditional on a count
    over the first result set, has different ordering and different
    filters, and fusing the two would need a materialized CTE, a
    synthetic rank column to survive a UNION, and NOT IN semantics that
    return empty silently on a NULL. The condition is a Python `if`.

    The `memory_trgm` GIN index does **not** serve this query and is not
    expected to: a bare function call in WHERE cannot drive a GIN scan
    (that needs the `<%` operator, whose threshold is a session GUC
    defaulting to 0.6, which would have to be set transaction-locally on
    every call or leak across the pooled connection). At one user's
    scale a sequential scan costs microseconds. The index is kept
    because plan section 4 specifies it and because the dedupe path
    could use it later.
    """
    if len(user_text.strip()) < MIN_QUERY_CHARS:
        return []

    score = func.word_similarity(Memory.text, user_text).label("score")
    scored = (
        select(Memory, score)
        .where(Memory.superseded_by.is_(None))
        .where(Memory.pinned.is_(False))
        .where(score > RETRIEVAL_MIN_SCORE)
        # NULLS FIRST is explicit on purpose: Postgres defaults ASC to
        # NULLS LAST, which would silently invert section 6's "ties go
        # to the older last_used_at (nulls first)". The trailing id is
        # not cosmetic -- without a total order the result is genuinely
        # nondeterministic and ordering tests flake.
        .order_by(score.desc(), Memory.last_used_at.asc().nullsfirst(), Memory.id.asc())
        .limit(limit)
    )
    result = await session.execute(scored)
    rows = [row[0] for row in result.all()]

    if len(rows) >= TOPUP_MIN_MATCHES:
        return rows

    return rows + await _topup(session, exclude_ids=[row.id for row in rows], limit=limit)


async def _topup(session: AsyncSession, *, exclude_ids: list[int], limit: int) -> list[Memory]:
    """Fill the retrieved set up to TOPUP_MIN_MATCHES (plan section 6).

    "Top up with the 3 most recent identity/rule memories" is read as
    fill *to* 3, not add 3: the pool is specified as exactly 3, which is
    precisely the worst case of zero matches. Adding 3 would give five
    items on two matches against a cap of six, a shape nobody designs.

    Known distortion, relevant to later milestones: because top-ups come
    from the same small newest-identity/rule pool on every low-match
    turn, those rows accumulate `last_used_at` and `use_count` that
    measure how often *retrieval failed*, not how useful they were. Do
    not read `use_count` as a pure usefulness signal in 2c/2d without
    accounting for that.

    `created_at DESC, id DESC`: rows inserted in one transaction share
    `now()` exactly, so created_at alone is not a total order.
    """
    needed = min(TOPUP_MIN_MATCHES - len(exclude_ids), limit - len(exclude_ids))
    if needed <= 0:
        return []

    stmt = (
        select(Memory)
        .where(Memory.superseded_by.is_(None))
        .where(Memory.pinned.is_(False))
        .where(Memory.kind.in_(TOPUP_KINDS))
        .order_by(Memory.created_at.desc(), Memory.id.desc())
        .limit(needed)
    )
    if exclude_ids:
        stmt = stmt.where(Memory.id.notin_(exclude_ids))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def mark_used(session: AsyncSession, memory_ids: list[int]) -> None:
    """Record that these memories were injected into a **delivered** turn.

    Does not commit: the caller (app/core/turn.py) folds this into the
    same transaction that marks the assistant message sent, so the two
    facts cannot diverge.
    """
    if not memory_ids:
        return
    await session.execute(
        sql_update(Memory)
        .where(Memory.id.in_(memory_ids))
        .values(last_used_at=func.now(), use_count=Memory.use_count + 1)
    )


async def _near_duplicate(
    session: AsyncSession, text: str, *, ignore_id: int | None
) -> Memory | None:
    """An active memory too similar to `text` to be worth storing separately.

    `similarity()` here, not `word_similarity()`: this is a whole-string
    symmetric question ("are these the same fact?"), not the asymmetric
    extent question retrieval asks. The two metrics answer different
    questions and deliberately do not share a helper.
    """
    score = func.similarity(Memory.text, text).label("score")
    stmt = (
        select(Memory)
        .where(Memory.superseded_by.is_(None))
        .where(score > DEDUPE_MAX_SIMILARITY)
        .order_by(score.desc(), Memory.id.asc())
        .limit(1)
    )
    if ignore_id is not None:
        stmt = stmt.where(Memory.id != ignore_id)
    result = await session.execute(stmt)
    return result.scalars().first()


async def write_memory(
    session: AsyncSession,
    *,
    kind: str,
    text: str,
    source: str,
    pinned: bool = False,
    confidence: float | None = None,
    supersedes_id: int | None = None,
) -> Memory | None:
    """Insert a memory, or return None if it duplicates an active one.

    Dedupe applies to every source (plan section 6).

    **Dedupe vs supersede.** Section 6 gives both rules without saying
    which wins. When `supersedes_id` names a valid active row, that row
    is excluded from the dedupe scan while every other active memory is
    still checked. Without this, a correction worded closely to the fact
    it corrects -- "живёт в Лилле" -> "живёт в Руане" -- would be
    discarded as a near-duplicate and the supersede silently lost, which
    is the exact case plan section 16 lists as acceptance criteria.

    The insert and the pointer update are one transaction. A crash
    between them would leave both rows active, which is precisely the
    duplicate this function exists to prevent.
    """
    old: Memory | None = None
    if supersedes_id is not None:
        old = await session.get(Memory, supersedes_id)
        # Only an active row may be superseded, which is what keeps
        # chains linear and makes hard_delete's relink unambiguous.
        if old is None or old.superseded_by is not None:
            old = None

    duplicate = await _near_duplicate(session, text, ignore_id=old.id if old else None)
    if duplicate is not None:
        logger.info(
            "memory not written, near-duplicate",
            extra={"memory_id": duplicate.id, "kind": kind},
        )
        return None

    memory = Memory(
        kind=kind,
        text=text,
        source=source,
        pinned=pinned,
        confidence=confidence,
    )
    session.add(memory)
    await session.flush()

    if old is not None:
        old.superseded_by = memory.id

    await session.commit()
    await session.refresh(memory)
    logger.info(
        "memory written",
        extra={
            "memory_id": memory.id,
            "kind": kind,
            "superseded_id": old.id if old else None,
        },
    )
    return memory


async def hard_delete(session: AsyncSession, memory_id: int) -> bool:
    """Delete a memory outright, relinking any chain through it (plan section 11).

    Section 11 says "clears any `superseded_by` pointers to it". Taken
    literally that resurrects retired facts: given a chain a -> b -> c,
    deleting b and clearing pointers to b sets `a.superseded_by = NULL`,
    making `a` -- a fact the user explicitly replaced -- active again and
    eligible for the prompt, because they deleted a *different* row. An
    ON DELETE SET NULL constraint would have the identical bug.

    So the predecessors are relinked to the successor rather than
    cleared. When the deleted row was the head of its chain its
    successor is NULL, and relinking degenerates into exactly the clear
    section 11 describes.
    """
    memory = await session.get(Memory, memory_id)
    if memory is None:
        return False

    successor = memory.superseded_by
    await session.execute(
        sql_update(Memory)
        .where(Memory.superseded_by == memory_id)
        .values(superseded_by=successor)
    )
    await session.delete(memory)
    await session.commit()
    logger.info("memory deleted", extra={"memory_id": memory_id})
    return True


async def set_pinned(session: AsyncSession, memory_id: int, pinned: bool) -> Memory | None:
    """Pin or unpin an active memory. Returns None if there is no such row."""
    memory = await session.get(Memory, memory_id)
    if memory is None or memory.superseded_by is not None:
        return None
    memory.pinned = pinned
    await session.commit()
    await session.refresh(memory)
    logger.info("memory pin toggled", extra={"memory_id": memory_id, "pinned": pinned})
    return memory


async def get_active(session: AsyncSession, memory_id: int) -> Memory | None:
    memory = await session.get(Memory, memory_id)
    if memory is None or memory.superseded_by is not None:
        return None
    return memory


async def list_active(
    session: AsyncSession, *, offset: int, limit: int
) -> tuple[list[Memory], int]:
    """One page of active memories, oldest first, plus the total count."""
    total = await session.execute(
        select(func.count()).select_from(Memory).where(Memory.superseded_by.is_(None))
    )
    rows = await session.execute(
        select(Memory)
        .where(Memory.superseded_by.is_(None))
        .order_by(Memory.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), total.scalar_one()


# --- pending_memory: /remember's parked text (plan section 11) ---


async def add_pending(session: AsyncSession, text: str) -> PendingMemory:
    pending = PendingMemory(text=text)
    session.add(pending)
    await session.commit()
    await session.refresh(pending)
    return pending


async def take_pending(session: AsyncSession, pending_id: int) -> str | None:
    """Consume a pending row, returning its text, or None if it is gone.

    Deleting on read is what makes the kind-button callback idempotent:
    a replayed press finds nothing and is answered "Устарело" rather
    than writing a second memory. RETURNING makes the read and the
    delete a single statement, so two concurrent presses cannot both win
    -- true even though the worker's concurrency is 1, which keeps the
    guarantee from resting on that.
    """
    result = await session.execute(
        delete(PendingMemory)
        .where(PendingMemory.id == pending_id)
        .returning(PendingMemory.text)
    )
    row = result.first()
    await session.commit()
    return row[0] if row is not None else None


async def purge_pending_older_than(
    session: AsyncSession, older_than: datetime.timedelta
) -> int:
    """Drop pending rows whose keyboard was never pressed.

    # TODO(phase-3): call this from the scheduled-job kinds the tick adds.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - older_than
    result = await session.execute(
        delete(PendingMemory).where(PendingMemory.created_at < cutoff).returning(PendingMemory.id)
    )
    rows = result.fetchall()
    await session.commit()
    return len(rows)
