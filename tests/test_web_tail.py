"""app/web/tail.py tests (web-chat plan track 1, design section 3).

- a proactive (`outbound`-kind) row is published
- web-origin rows are excluded (negative update_id or reply_to_update)
- unsent assistant rows are held back
- rows from a Telegram-origin turn (both the user's and the reply) are
  mirrored
- the cursor only moves forward and repeat polls do not republish
"""

from __future__ import annotations

import datetime

from app.db.models import Message, TelegramUpdate
from app.web.hub import WebHub
from app.web.tail import _max_message_id, _tail_once

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
