"""app/web/tail.py tests (web-chat plan track 1, design section 3).

- a proactive (`outbound`-kind) row is published
- web-origin rows are excluded (negative update_id or reply_to_update)
- unsent assistant rows are held back
- rows from a Telegram-origin turn (both the user's and the reply) are
  mirrored
- the cursor only moves forward and repeat polls do not republish
- the second, state_change cursor: a mapped field publishes
  publish_invalidate(topic), an unmapped one publishes nothing, and it
  gets the same startup/reset semantics as the message cursor
- the third, proposal fingerprint poll (W2): a create/accept/reject/
  expire moves the fingerprint and publishes both "proposals" and
  "state"; an unrelated poll with nothing new publishes nothing; the
  fingerprint is seeded at boot the same way the other two cursors are
"""

from __future__ import annotations

import asyncio
import datetime

from app.core import proposal as proposal_core
from app.core.clock import SystemClock
from app.db.models import Message, StateChange, TelegramUpdate
from app.web import tail as tail_module
from app.web.hub import WebHub
from app.web.tail import (
    STATE_CHANGE_FIELD_TOPIC,
    _max_message_id,
    _max_state_change_id,
    _proposals_fingerprint,
    _tail_once,
    _tail_proposals_once,
    _tail_state_change_once,
    start_tail,
    stop_tail,
)

NOW = datetime.datetime.now(datetime.timezone.utc)


async def _add(sessionmaker, *, update_id: int | None = None, **kwargs) -> Message:
    """Insert a `message` row, first seeding the `telegram_update` row its
    FK requires when `update_id` is given (message.update_id -> that
    table's primary key)."""
    async with sessionmaker() as session:
        if update_id is not None:
            existing = await session.get(TelegramUpdate, update_id)
            if existing is None:
                session.add(TelegramUpdate(update_id=update_id, payload={}))
                await session.commit()
        message = Message(ooc=False, update_id=update_id, **kwargs)
        session.add(message)
        await session.commit()
        await session.refresh(message)
        return message


async def test_telegram_turn_is_mirrored_both_sides(sessionmaker):
    user_row = await _add(
        sessionmaker, role="user", content="привет", kind="chat", update_id=100
    )
    reply_row = await _add(
        sessionmaker,
        role="assistant",
        content="и тебе привет",
        kind="chat",
        reply_to_update=100,
        sent_at=NOW,
    )

    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_once(session, hub, 0)

    assert cursor == reply_row.id
    events = hub.subscribe(last_event_id=0)
    events.close()
    texts = [e.data["text"] for e in events.backlog]
    assert texts == ["привет", "и тебе привет"]
    roles = [e.data["role"] for e in events.backlog]
    assert roles == ["user", "assistant"]
    ids = [e.data["id"] for e in events.backlog]
    assert ids == [user_row.id, reply_row.id]  # positive DB ids, not negative


async def test_proactive_outbound_row_is_mirrored(sessionmaker):
    row = await _add(
        sessionmaker,
        role="assistant",
        content="Доброе утро.",
        kind="outbound",
        sent_at=NOW,
    )

    hub = WebHub()
    async with sessionmaker() as session:
        await _tail_once(session, hub, 0)

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert len(events.backlog) == 1
    assert events.backlog[0].data["kind"] == "outbound"
    assert events.backlog[0].data["id"] == row.id


async def test_unsent_assistant_row_is_held_back(sessionmaker):
    await _add(
        sessionmaker,
        role="assistant",
        content="ещё не отправлено",
        kind="chat",
        reply_to_update=200,
        sent_at=None,
    )

    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_once(session, hub, 0)

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert events.backlog == []
    assert cursor == 0  # nothing published, cursor does not move


async def test_web_origin_rows_are_excluded(sessionmaker):
    # A web-origin user row (negative update_id, as app/web/ingress.py
    # would store it) and a web-origin assistant reply (negative
    # reply_to_update) -- both already delivered live by WebSinkSession,
    # so the tail must never republish either.
    await _add(sessionmaker, role="user", content="веб-сообщение", kind="chat", update_id=-5)
    await _add(
        sessionmaker,
        role="assistant",
        content="веб-ответ",
        kind="chat",
        reply_to_update=-6,
        sent_at=NOW,
    )
    # A genuine Telegram-origin row in the same batch, to prove the
    # exclusion is selective rather than blocking everything.
    telegram_row = await _add(
        sessionmaker, role="user", content="телеграм-сообщение", kind="chat", update_id=300
    )

    hub = WebHub()
    async with sessionmaker() as session:
        await _tail_once(session, hub, 0)

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert len(events.backlog) == 1
    assert events.backlog[0].data["id"] == telegram_row.id
    assert events.backlog[0].data["text"] == "телеграм-сообщение"


async def test_cursor_advances_and_a_repeat_poll_republishes_nothing(sessionmaker):
    row = await _add(
        sessionmaker, role="user", content="раз", kind="chat", update_id=400
    )

    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_once(session, hub, 0)
    assert cursor == row.id

    async with sessionmaker() as session:
        cursor_again = await _tail_once(session, hub, cursor)
    assert cursor_again == cursor  # unchanged: nothing new past the cursor

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert len(events.backlog) == 1  # not republished


async def test_max_message_id_is_zero_on_an_empty_table(sessionmaker):
    async with sessionmaker() as session:
        assert await _max_message_id(session) == 0


async def test_cursor_resets_when_delete_restarts_the_id_sequence(sessionmaker):
    """Medium-severity finding: app/core/purge.py's /delete runs
    `TRUNCATE ... RESTART IDENTITY` on `message`, so every future
    `message.id` starts again at 1 while the in-memory tail cursor stays
    wherever it was. `Message.id > cursor` would then filter out every
    row forever. Simulated here without actually running /delete: seed
    a high cursor, then truncate `message` (RESTART IDENTITY) exactly
    as purge.py does, and confirm the next poll still sees a
    post-wipe row."""
    from sqlalchemy import text as sql_text

    from app.core.purge import PURGED_TABLES

    # A few rows, not just one: keeps the pre-wipe cursor comfortably
    # above 1, so the post-wipe id (which restarts at 1) is unambiguously
    # *below* it rather than coincidentally equal by pure test-isolation
    # luck -- the real-world case this guards is a large, long-lived
    # cursor meeting a freshly-reset id space, never a near-tie.
    for i in range(3):
        row = await _add(
            sessionmaker, role="user", content=f"до удаления {i}", kind="chat", update_id=500 + i
        )
    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_once(session, hub, 0)
    assert cursor == row.id

    async with sessionmaker() as session:
        # The same statement app/core/purge.py's delete_everything runs
        # (every PURGED_TABLES table at once, so the FK from `outbound`
        # etc. into `message` does not block a plain single-table
        # TRUNCATE), not a hand-rolled approximation of it.
        await session.execute(sql_text(f"TRUNCATE TABLE {', '.join(PURGED_TABLES)} RESTART IDENTITY"))
        await session.commit()

    new_row = await _add(
        sessionmaker, role="user", content="после удаления", kind="chat", update_id=501
    )
    assert new_row.id <= cursor  # the sequence really did restart

    async with sessionmaker() as session:
        cursor = await _tail_once(session, hub, cursor)

    events = hub.subscribe(last_event_id=0)
    events.close()
    texts = [e.data["text"] for e in events.backlog]
    assert texts == ["до удаления 0", "до удаления 1", "до удаления 2", "после удаления"]
    assert cursor == new_row.id


async def test_cursor_does_not_advance_past_a_blocking_unsent_assistant_row(sessionmaker):
    """Low-severity finding: a poll that saw a mix of "still unsent" and
    "visible" rows above the old cursor used to still set the returned
    cursor to the highest *visible* id, since `_mirror_query` already
    excludes the unsent row -- permanently skipping that row once it
    did finish sending, because the next poll would query `id >
    cursor` and the now-sent row's id would already be below it."""
    unsent = await _add(
        sessionmaker,
        role="assistant",
        content="ещё не отправлено",
        kind="chat",
        reply_to_update=600,
        sent_at=None,
    )
    later_visible = await _add(
        sessionmaker, role="user", content="более новое сообщение", kind="chat", update_id=601
    )
    assert later_visible.id > unsent.id

    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_once(session, hub, 0)

    # The cursor must stay capped below the still-unsent row, even
    # though a later, visible row already got published live.
    assert cursor == unsent.id - 1
    events = hub.subscribe(last_event_id=0)
    events.close()
    assert [e.data["text"] for e in events.backlog] == ["более новое сообщение"]

    # The unsent row finally gets sent...
    async with sessionmaker() as session:
        db_row = await session.get(Message, unsent.id)
        db_row.sent_at = NOW
        await session.commit()

    # ...and the very next poll, from the capped cursor, picks it up --
    # instead of it being permanently skipped.
    async with sessionmaker() as session:
        cursor = await _tail_once(session, hub, cursor)
    assert cursor == later_visible.id

    events = hub.subscribe(last_event_id=0)
    events.close()
    texts = [e.data["text"] for e in events.backlog]
    assert texts == ["более новое сообщение", "ещё не отправлено", "более новое сообщение"]


async def test_a_stale_unsent_row_no_longer_blocks_the_cursor_forever(sessionmaker):
    """The cap on how long an unsent row is waited for: otherwise one
    permanently-failed row (MAX_ATTEMPTS exhausted) would stall live
    delivery of everything after it forever."""
    old = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    stale_unsent = await _add(
        sessionmaker,
        role="assistant",
        content="навсегда не отправлено",
        kind="chat",
        reply_to_update=700,
        sent_at=None,
        created_at=old,
    )
    later_visible = await _add(
        sessionmaker, role="user", content="новое", kind="chat", update_id=701
    )
    assert later_visible.id > stale_unsent.id

    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_once(session, hub, 0)

    assert cursor == later_visible.id  # not held back by the stale row


# --- the second cursor: state_change -> invalidate ------------------------


async def _add_state_change(sessionmaker, *, field: str, source: str = "command") -> StateChange:
    async with sessionmaker() as session:
        row = StateChange(field=field, old_value=None, new_value=None, source=source)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def test_a_mapped_field_publishes_invalidate_with_its_topic(sessionmaker):
    await _add_state_change(sessionmaker, field="focus_on")

    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_state_change_once(session, hub, 0)

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert [e.event for e in events.backlog] == ["invalidate"]
    assert events.backlog[0].data == {"topic": STATE_CHANGE_FIELD_TOPIC["focus_on"]}
    assert cursor > 0


async def test_every_distinct_topic_publishes_once_per_poll_deduplicated(sessionmaker):
    """Low-severity finding: one `invalidate` per *distinct* topic among
    the rows a single poll sees, not one per row -- several
    STATE_CHANGE_FIELD_TOPIC fields share a topic (all of "state"'s
    eight fields, "checkin"'s four, "memory"'s three), and a real update
    routinely writes more than one of them in the same transaction/poll
    window. Publishing every one of them would flood the hub's shared,
    fixed-size ring buffer with events a client cannot tell apart from
    each other and gains nothing from receiving twice."""
    for field in STATE_CHANGE_FIELD_TOPIC:
        await _add_state_change(sessionmaker, field=field)

    hub = WebHub()
    async with sessionmaker() as session:
        await _tail_state_change_once(session, hub, 0)

    events = hub.subscribe(last_event_id=0)
    events.close()
    topics = [e.data["topic"] for e in events.backlog]
    # One event per distinct topic, in first-seen order -- not one per
    # row/field.
    expected = list(dict.fromkeys(STATE_CHANGE_FIELD_TOPIC.values()))
    assert topics == expected


async def test_idle_run_is_mapped_to_the_memory_topic(sessionmaker):
    """app/core/idle/undo.py's undo() restores or re-deletes memory
    rows; a Memory screen open during an undo needs the same "refetch
    me" signal a /forget or an extractor autowrite gets."""
    assert STATE_CHANGE_FIELD_TOPIC["idle_run"] == "memory"
    await _add_state_change(sessionmaker, field="idle_run", source="undo")

    hub = WebHub()
    async with sessionmaker() as session:
        await _tail_state_change_once(session, hub, 0)

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert [e.data["topic"] for e in events.backlog] == ["memory"]


async def test_an_unmapped_field_advances_the_cursor_but_publishes_nothing(sessionmaker):
    # "data" (app/core/purge.py's /delete) is written in the real app
    # but deliberately absent from STATE_CHANGE_FIELD_TOPIC -- a full
    # wipe, not an incremental change any screen's "refetch me" story
    # covers.
    assert "data" not in STATE_CHANGE_FIELD_TOPIC
    row = await _add_state_change(sessionmaker, field="data")

    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_state_change_once(session, hub, 0)

    assert cursor == row.id
    events = hub.subscribe(last_event_id=0)
    events.close()
    assert events.backlog == []


async def test_state_change_cursor_advances_and_a_repeat_poll_republishes_nothing(sessionmaker):
    row = await _add_state_change(sessionmaker, field="timezone")

    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_state_change_once(session, hub, 0)
    assert cursor == row.id

    async with sessionmaker() as session:
        cursor_again = await _tail_state_change_once(session, hub, cursor)
    assert cursor_again == cursor

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert len(events.backlog) == 1


async def test_max_state_change_id_is_zero_on_an_empty_table(sessionmaker):
    async with sessionmaker() as session:
        assert await _max_state_change_id(session) == 0


async def test_state_change_cursor_resets_when_delete_restarts_the_id_sequence(sessionmaker):
    """Same reasoning as the message cursor's own reset test:
    app/core/purge.py's /delete TRUNCATEs `state_change` (it is in
    PURGED_TABLES, in the same statement as `message`) with RESTART
    IDENTITY, so a stale in-memory cursor above the post-wipe max id
    must reset to 0 rather than filtering out every future row."""
    from sqlalchemy import text as sql_text

    from app.core.purge import PURGED_TABLES

    for _ in range(3):
        row = await _add_state_change(sessionmaker, field="streak")
    hub = WebHub()
    async with sessionmaker() as session:
        cursor = await _tail_state_change_once(session, hub, 0)
    assert cursor == row.id

    async with sessionmaker() as session:
        await session.execute(sql_text(f"TRUNCATE TABLE {', '.join(PURGED_TABLES)} RESTART IDENTITY"))
        await session.commit()

    new_row = await _add_state_change(sessionmaker, field="streak")
    assert new_row.id <= cursor

    async with sessionmaker() as session:
        cursor = await _tail_state_change_once(session, hub, cursor)

    assert cursor == new_row.id
    events = hub.subscribe(last_event_id=0)
    events.close()
    # Three pre-wipe rows collapse into a single "checkin" invalidate
    # (one poll, one topic, per _tail_state_change_once's per-poll
    # dedup), and the post-wipe row is a second poll -- two events, not
    # four.
    assert [e.data["topic"] for e in events.backlog] == ["checkin", "checkin"]


# --- start_tail: both cursors seed at the current max id at boot --------


async def test_start_tail_seeds_both_cursors_so_pre_boot_rows_are_never_replayed(
    sessionmaker, monkeypatch
):
    """Low-severity finding: nothing regression-tested that start_tail()
    actually reads `_max_message_id`/`_max_state_change_id` at boot
    rather than starting both cursors at 0 -- if it stopped doing that,
    every pre-boot message/state_change row would be replayed as
    live/invalidate events on the first poll after every deploy, and no
    test would have failed."""
    await _add(sessionmaker, role="user", content="до старта", kind="chat", update_id=900)
    await _add_state_change(sessionmaker, field="timezone")

    monkeypatch.setattr(tail_module, "POLL_INTERVAL_SECONDS", 0.01)
    hub = WebHub()
    task = await start_tail(sessionmaker, hub)
    try:
        await asyncio.sleep(0.05)  # let at least one poll run
    finally:
        await stop_tail(task)

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert events.backlog == []


# --- the proposal fingerprint poll (W2) ----------------------------------


async def test_proposals_fingerprint_is_zero_on_an_empty_table(sessionmaker):
    async with sessionmaker() as session:
        assert await _proposals_fingerprint(session) == (0, None, 0)


async def test_new_pending_proposal_publishes_proposals_and_state(sessionmaker):
    clock = SystemClock()
    hub = WebHub()
    async with sessionmaker() as session:
        fp = await _tail_proposals_once(session, hub, (0, None, 0))

    async with sessionmaker() as session:
        await proposal_core.create(
            session, clock, field=proposal_core.DUE_ACTION, value="сдать отчёт", reason=None
        )

    async with sessionmaker() as session:
        fp = await _tail_proposals_once(session, hub, fp)

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert [e.data["topic"] for e in events.backlog] == ["proposals", "state"]


async def test_a_repeat_poll_with_no_change_publishes_nothing(sessionmaker):
    clock = SystemClock()
    hub = WebHub()
    async with sessionmaker() as session:
        await proposal_core.create(
            session, clock, field=proposal_core.DUE_ACTION, value="сдать отчёт", reason=None
        )
    async with sessionmaker() as session:
        fp = await _tail_proposals_once(session, hub, (0, None, 0))

    async with sessionmaker() as session:
        fp_again = await _tail_proposals_once(session, hub, fp)
    assert fp_again == fp

    events = hub.subscribe(last_event_id=0)
    events.close()
    # One poll's worth of events only -- the repeat poll published nothing.
    assert [e.data["topic"] for e in events.backlog] == ["proposals", "state"]


async def test_accept_moves_the_fingerprint(sessionmaker):
    from app.db.models import UserState

    clock = SystemClock()
    hub = WebHub()
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=4242, timezone="Europe/Paris"))
        await session.commit()
        created, _ = await proposal_core.create(
            session, clock, field=proposal_core.FOCUS_ON, value="on", reason=None
        )
        pid = created.id
    async with sessionmaker() as session:
        fp = await _tail_proposals_once(session, hub, (0, None, 0))

    async with sessionmaker() as session:
        await proposal_core.accept(session, clock, pid)

    async with sessionmaker() as session:
        fp2 = await _tail_proposals_once(session, hub, fp)
    assert fp2 != fp

    events = hub.subscribe(last_event_id=0)
    events.close()
    # Two polls, each moving the fingerprint: create, then accept.
    assert [e.data["topic"] for e in events.backlog] == [
        "proposals", "state", "proposals", "state"
    ]


async def test_expire_via_a_second_create_moves_the_fingerprint(sessionmaker):
    """create() itself expires any outstanding pending proposal (one
    pending at a time), which flips the pending count 1 -> 1 but moves
    max(id) -- the fingerprint still catches it."""
    clock = SystemClock()
    hub = WebHub()
    async with sessionmaker() as session:
        await proposal_core.create(
            session, clock, field=proposal_core.DUE_ACTION, value="первое", reason=None
        )
    async with sessionmaker() as session:
        fp = await _tail_proposals_once(session, hub, (0, None, 0))

    async with sessionmaker() as session:
        await proposal_core.create(
            session, clock, field=proposal_core.FOCUS_ON, value="on", reason=None
        )
    async with sessionmaker() as session:
        fp2 = await _tail_proposals_once(session, hub, fp)
    assert fp2 != fp


async def test_start_tail_also_seeds_the_proposals_fingerprint(sessionmaker, monkeypatch):
    """The same low-severity-finding shape as the message/state_change
    cursors' own boot test: a pre-boot proposal must not be replayed as
    a live invalidate on the first poll after start."""
    clock = SystemClock()
    async with sessionmaker() as session:
        await proposal_core.create(
            session, clock, field=proposal_core.DUE_ACTION, value="до старта", reason=None
        )

    monkeypatch.setattr(tail_module, "POLL_INTERVAL_SECONDS", 0.01)
    hub = WebHub()
    task = await start_tail(sessionmaker, hub)
    try:
        await asyncio.sleep(0.05)
    finally:
        await stop_tail(task)

    events = hub.subscribe(last_event_id=0)
    events.close()
    assert events.backlog == []
