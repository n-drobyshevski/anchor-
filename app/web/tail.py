"""Mirroring Telegram-origin and proactive `message` rows into the
WebHub (web-chat plan track 1, design section 3 point 2).

app/web/sink.py's WebSinkSession publishes everything a *web-origin*
turn sends, live, the instant it is sent. But the whole point of a
single-user bot with a web front end is that the web view shows the
**same conversation** Telegram does -- including messages that arrived
over Telegram, and proactive ones app/core/outbound_send.py sent
unprompted. Nothing in app/core/* or app/tg/* knows a web hub exists (by
design -- see app/web/sink.py's module docstring), so this module polls
the database instead of being told.

Runs as one background asyncio task per process, started only when
WEB_UI_ENABLED (app/main.py / track 2's wiring calls `start_tail`).
Polling every two seconds rather than a DB trigger or LISTEN/NOTIFY
keeps this module to a plain SELECT with no new Postgres feature and no
new failure mode beyond "the next poll is two seconds later than the
last one" -- acceptable for a chat, not for a stock ticker.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Message, StateChange
from app.web.hub import WebHub

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0

# state_change.field -> WebHub invalidate topic (W1 plan step 2). Built
# by reading every update_state()/record_change() call site in the repo
# (app/core/state.py's own callers, app/tg/router.py, app/core/
# proposal.py, app/core/turn.py, app/core/checkin.py, app/core/
# cards.py, app/tg/memory.py, app/core/extract.py) for the exact field
# names each one writes.
#
# This table is *not* exhaustive over every real mutation, despite an
# earlier version of this comment claiming it was (medium-severity
# finding). Several state-changing paths write no `state_change` row at
# all, or one whose field this table does not map, so no W1 screen
# should assume "the invalidate fired" means "the change happened" --
# only the reverse:
# - app/core/proposal.py's create()/accept()/reject() change
#   `proposal.status` with no state_change row of their own (a future
#   Proposals screen would need one added there, mapped to a topic --
#   INVALIDATE_TOPICS' own "proposals" entry has no producer yet and is
#   left unused rather than wired to this table's "idle_run"-shaped
#   workaround, which would be the wrong signal for a different kind of
#   row).
# - accept()'s RULE branch writes a memory row via app/core/memory.py's
#   write_memory() without a matching state_change("memory") row.
# - app/research/jobs.py inserts a new StudyCard with no state_change
#   row (only adopt/reject go through this table today).
#
# A field with no entry here publishes no event at all (see
# _tail_state_change_once below) -- deliberately, for one field that
# *is* written but does not belong on a live SSE topic:
# - "data" (app/core/purge.py's /delete): a full wipe, not an
#   incremental change any single screen's "refetch me" story covers.
STATE_CHANGE_FIELD_TOPIC: dict[str, str] = {
    # 2a/2f/3a: due date, focus mode, quiet hours, timezone, and the
    # persona/intensity flags -- command, button and welfare sources
    # alike (app/tg/router.py, app/core/proposal.py, app/core/turn.py).
    "due_action": "state",
    "due_set_at": "state",
    "focus_on": "state",
    "focus_since": "state",
    "quiet_until": "state",
    "timezone": "state",
    "persona_active": "state",
    "intensity": "state",
    # 2f: the daily check-in streak and its awaiting-note flow
    # (app/core/checkin.py).
    "streak": "checkin",
    "last_checkin_at": "checkin",
    "awaiting": "checkin",
    "awaiting_ref": "checkin",
    # 2b/2c: memory writes and hard-deletes, and journal entries
    # (app/tg/memory.py's /forget, app/core/extract.py's autowrite).
    "memory": "memory",
    "journal": "memory",
    # app/core/idle/undo.py's undo(): restores or re-deletes whatever
    # memory rows the idle run touched (design section unrelated to
    # this track, but the effect is a memory-table change all the
    # same). Mapped to "memory", not left unmapped as pure idle-track
    # bookkeeping -- a future Memory screen open during an undo would
    # otherwise sit stale with no signal that its content just changed
    # underneath it.
    "idle_run": "memory",
    # 5a (study cards): a technique proposal accepted or rejected
    # (app/core/cards.py).
    "study_card": "cards",
}


def _mirror_query(cursor: int):
    """Rows worth mirroring live, and only those (design section 3):

    - the user's own messages (`role='user'`) as soon as they exist;
    - assistant rows only once actually sent (`sent_at is not null`) --
      never a row a retry might still overwrite with different content;
    - never a web-origin row. Both `update_id` (the inbound row's FK)
      and `reply_to_update` (the assistant row's idempotency key) are
      coalesced to 0 and required to be non-negative; web ids are always
      negative (app/db/queue.py's WEB_ID_OFFSET), so this excludes
      exactly the rows WebSinkSession already published live -- with no
      gap and no double delivery either way, since every `message` row
      is exactly one of "web-origin" or "mirrorable", never both.

    This one query is what makes a proactive `outbound` row, its
    welfare/canned fallout, and a Telegram-typed message all show up in
    the web view without app/core/outbound_send.py or app/tg/router.py
    knowing this hub exists.
    """
    return (
        select(Message.id, Message.role, Message.content, Message.kind, Message.created_at)
        .where(Message.id > cursor)
        .where(or_(Message.role == "user", Message.sent_at.is_not(None)))
        .where(func.coalesce(Message.update_id, 0) >= 0)
        .where(func.coalesce(Message.reply_to_update, 0) >= 0)
        .order_by(Message.id)
    )


async def _max_message_id(session: AsyncSession) -> int:
    """The cursor's starting point: everything up to boot is already
    covered by GET /api/history (track 2), so the tail only ever needs
    to push what happens from here on (design section 3).
    """
    result = await session.execute(select(func.max(Message.id)))
    return result.scalar_one() or 0


# How long an unsent assistant row is still worth waiting for before the
# tail gives up on it and lets the cursor advance past it anyway (the
# blocking-unsent-row cap below). Bounded rather than infinite so one
# permanently-failed row (MAX_ATTEMPTS exhausted, app/db/queue.py) can
# never stall live delivery of everything after it forever -- only for
# this long, after which it is still visible on the next GET
# /api/history/page reload, just not live.
STALE_UNSENT_AFTER = datetime.timedelta(minutes=10)


async def _lowest_blocking_unsent_id(
    session: AsyncSession, *, after: int, upto: int, now: datetime.datetime
) -> int | None:
    """The smallest id of a still-unsent, still-recent assistant row in
    `(after, upto]` -- the id `_tail_once` must not let its returned
    cursor advance past yet.

    Without this, a poll that sees a mix of "still unsent" and
    "visible" rows above the old cursor (an assistant row inserted with
    sent_at NULL, followed by a later, already-sent or user row -- e.g.
    a Telegram send that raised and sent the whole update back to
    'pending', after which the claim loop moved on to a different,
    already-queued update) would still set the *cursor* to the highest
    visible id, since `_mirror_query` itself already excludes the
    unsent row from `rows`. The next poll would then query `id >
    cursor` and permanently skip that row once it does finish sending
    (a medium-severity finding: "The tail cursor can move past an
    assistant row that isn't sent yet").
    """
    result = await session.execute(
        select(func.min(Message.id))
        .where(Message.id > after)
        .where(Message.id <= upto)
        .where(Message.role == "assistant")
        .where(Message.sent_at.is_(None))
        .where(Message.created_at >= now - STALE_UNSENT_AFTER)
    )
    return result.scalar_one()


async def _tail_once(session: AsyncSession, hub: WebHub, cursor: int) -> int:
    """One poll: publish everything past `cursor`; return the new cursor.

    Publishing the same already-delivered row again is harmless (the
    client dedupes by id, app.js's `rendered` map) and is exactly what
    happens on every poll while the returned cursor is capped below a
    blocking unsent row -- see _lowest_blocking_unsent_id.

    Starts by checking the cursor is still valid for the table as it
    exists *now*: app/core/purge.py's /delete runs `TRUNCATE ... RESTART
    IDENTITY` on `message`, so a `/delete` while the process keeps
    running resets every future `message.id` back down to 1 while this
    in-memory cursor stays wherever it was -- `Message.id > cursor`
    would then filter out literally every row from then on, forever (a
    medium-severity finding: "The tail cursor never resets after
    /delete"). `max(id) < cursor` (or the table now being empty, `max`
    returning NULL/0) is exactly the signature a wipe leaves behind, and
    is cheaper to check every poll than teaching this module anything
    about /delete's existence.
    """
    if cursor > 0:
        max_id = await _max_message_id(session)
        if max_id < cursor:
            cursor = 0

    result = await session.execute(_mirror_query(cursor))
    rows = result.all()
    for row in rows:
        hub.publish_message(
            id=row.id,
            role=row.role,
            text=row.content,
            kind=row.kind,
            keyboard=None,
            ts=row.created_at,
        )
    new_cursor = rows[-1].id if rows else cursor
    if new_cursor > cursor:
        now = datetime.datetime.now(datetime.timezone.utc)
        blocking = await _lowest_blocking_unsent_id(
            session, after=cursor, upto=new_cursor, now=now
        )
        if blocking is not None:
            new_cursor = blocking - 1
    return new_cursor


# --- the second cursor: state_change -> invalidate ----------------------


def _state_change_query(cursor: int):
    return (
        select(StateChange.id, StateChange.field)
        .where(StateChange.id > cursor)
        .order_by(StateChange.id)
    )


async def _max_state_change_id(session: AsyncSession) -> int:
    """The state_change cursor's own starting point, at boot -- same
    reasoning as _max_message_id: nothing before boot needs a live
    event, a screen open at boot fetches its own current state instead.
    """
    result = await session.execute(select(func.max(StateChange.id)))
    return result.scalar_one() or 0


async def _tail_state_change_once(session: AsyncSession, hub: WebHub, cursor: int) -> int:
    """One poll of `state_change`: publish_invalidate() once per
    *distinct* topic among the rows past `cursor` whose field is in
    STATE_CHANGE_FIELD_TOPIC (an unmapped field, e.g. "data", advances
    the cursor like any other row but publishes nothing -- see that
    table's own docstring), then return the new cursor.

    One event per topic, not one per row (low-severity finding): a
    single update often writes several state_change rows for the same
    topic in one poll (a proposal accept writing due_action and
    due_set_at both map to "state"; /checkin writes up to four "checkin"
    rows). A client only ever does one thing with an `invalidate` event
    -- refetch that topic's own data -- so a second, third or fourth
    identical event in the same poll teaches it nothing new while still
    costing a slot in the hub's shared, fixed-size ring buffer
    (RING_BUFFER_SIZE), which a reconnecting tab's Last-Event-ID replay
    depends on for `message`/`edit` events too. A burst of same-topic
    rows (a bulk memory autowrite/extract) could otherwise evict real
    messages out of that buffer for no client-visible benefit.

    Same reset semantics as _tail_once's message cursor, and for the
    same reason: app/core/purge.py's /delete runs `TRUNCATE ...
    RESTART IDENTITY` on `state_change` in the very same statement as
    `message` (both are in PURGED_TABLES), so a cursor left above the
    post-wipe max id would otherwise filter out every future row
    forever. There is no analog of _lowest_blocking_unsent_id here --
    every state_change row is written, committed and immediately final
    in the same transaction as the change it records (app/core/
    state.py's update_state()/record_change()), so nothing about it is
    ever "not really there yet" the way an unsent assistant message can
    be.

    One known gap, not fully closed here: `state_change.id` is assigned
    at INSERT but only becomes visible at COMMIT, so a row from a
    slower-committing transaction can still commit *after* a poll has
    already moved the cursor past a concurrently-committed, higher id --
    the same shape of race _lowest_blocking_unsent_id closes for the
    message cursor, with no equivalent guard here. In practice this is
    a narrow window (both writers run in this same process's event
    loop) and the cost of missing an invalidate is low -- it is a hint,
    not a payload, and the next unrelated write to the same topic
    self-heals it -- so this is accepted as a known limitation rather
    than threaded through with the additional re-scan/dedupe-by-id state
    a full fix would need, pending an actual screen that reads this
    topic in production making the window worth closing for real.
    """
    if cursor > 0:
        max_id = await _max_state_change_id(session)
        if max_id < cursor:
            cursor = 0

    result = await session.execute(_state_change_query(cursor))
    rows = result.all()
    # Ordered dict semantics (Python's plain dict, insertion order) so
    # topics publish in the order their first row appeared this poll,
    # not an arbitrary one.
    topics: dict[str, None] = {}
    for row in rows:
        topic = STATE_CHANGE_FIELD_TOPIC.get(row.field)
        if topic is not None:
            topics[topic] = None
    for topic in topics:
        hub.publish_invalidate(topic)
    return rows[-1].id if rows else cursor


async def _tail_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    hub: WebHub,
    cursor: int,
    state_change_cursor: int,
) -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            async with sessionmaker() as session:
                cursor = await _tail_once(session, hub, cursor)
                state_change_cursor = await _tail_state_change_once(
                    session, hub, state_change_cursor
                )
        except Exception as exc:  # noqa: BLE001 - a tail crash must never take the process down
            logger.warning("web tail failed", extra={"event": type(exc).__name__})


async def start_tail(
    sessionmaker: async_sessionmaker[AsyncSession], hub: WebHub
) -> asyncio.Task:
    """Start the tail task. Mirrors app/worker.py's run_worker() shape:
    build both cursors, hand off to a background task, return it so the
    caller (app/main.py's cleanup, track 2) can cancel it on shutdown.
    """
    async with sessionmaker() as session:
        cursor = await _max_message_id(session)
        state_change_cursor = await _max_state_change_id(session)
    return asyncio.create_task(
        _tail_loop(sessionmaker, hub, cursor, state_change_cursor), name="anchor-web-tail"
    )


async def stop_tail(task: asyncio.Task) -> None:
    """Cancel the tail task and await its cancellation (shutdown path)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
