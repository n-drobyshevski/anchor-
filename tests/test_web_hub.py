"""app/web/hub.py tests (web-chat plan track 1).

- Last-Event-ID replay returns only events after the given seq
- the subscriber cap (3) raises TooManySubscribers
- close_all() ends every live stream and clears the callback allowlist
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from app.web.hub import MAX_SUBSCRIBERS, TooManySubscribers, WebHub

NOW = datetime.datetime(2026, 9, 23, tzinfo=datetime.timezone.utc)


def test_seq_is_seeded_high_so_a_stale_pre_restart_id_replays_everything():
    """Low-severity finding: `seq` used to restart at 1 on every process
    boot, so a tab that reconnected with a Last-Event-ID from a previous
    process (still a low number by the new process's own count) would
    see `record.seq > last_event_id` as false for everything published
    since boot and miss it all. Seeding `seq` from the wall clock instead
    means even Last-Event-ID: 0 (an id lower than any pre-boot seq could
    plausibly still be checked against) replays the entire buffer."""
    hub = WebHub()
    hub.publish_typing()
    # A four-digit id is the shape a previous, short-lived process's
    # counter would have reached starting from 1 -- nowhere near a
    # millisecond epoch timestamp.
    sub = hub.subscribe(last_event_id=9999)
    sub.close()
    assert len(sub.backlog) == 1


def test_last_event_id_replay_returns_only_later_events():
    hub = WebHub()
    seq1 = hub.publish_message(id=-1, role="assistant", text="один", kind="chat", keyboard=None, ts=NOW)
    seq2 = hub.publish_message(id=-2, role="assistant", text="два", kind="chat", keyboard=None, ts=NOW)
    hub.publish_message(id=-3, role="assistant", text="три", kind="chat", keyboard=None, ts=NOW)

    sub = hub.subscribe(last_event_id=seq2)
    sub.close()

    assert len(sub.backlog) == 1
    assert sub.backlog[0].data["text"] == "три"
    assert sub.backlog[0].seq > seq2 > seq1


def test_subscribe_with_no_last_event_id_replays_nothing():
    hub = WebHub()
    hub.publish_typing()
    sub = hub.subscribe()
    sub.close()
    assert sub.backlog == []


async def test_live_events_are_delivered_to_the_subscriber_queue():
    hub = WebHub()
    sub = hub.subscribe()
    hub.publish_toast("привет")

    event = await asyncio.wait_for(sub.events().__anext__(), timeout=1)

    assert event.event == "toast"
    assert event.data == {"text": "привет"}
    sub.close()


def test_subscriber_cap_raises_too_many_subscribers():
    hub = WebHub()
    subs = [hub.subscribe() for _ in range(MAX_SUBSCRIBERS)]
    with pytest.raises(TooManySubscribers):
        hub.subscribe()
    for sub in subs:
        sub.close()
    # The cap is freed once a slot closes.
    freed = hub.subscribe()
    freed.close()


async def test_close_all_ends_live_streams_and_clears_allowlist():
    hub = WebHub()
    hub.register_keyboard(-1, [[{"text": "Да", "data": "w:resume"}]])
    sub = hub.subscribe()

    hub.close_all()

    assert hub.allow_press(-1, "w:resume") is False
    # The stream's queue got the poison pill: events() ends cleanly.
    remaining = [event async for event in sub.events()]
    assert remaining == []
    # A fresh subscribe works again -- the cap was released too.
    fresh = hub.subscribe()
    fresh.close()


async def test_close_all_clears_the_ring_buffer_too():
    """Medium-severity finding: close_all() used to end every live
    stream and clear the allowlist, but left the 200-event ring buffer
    itself untouched -- text a /delete or /weblogout was supposed to
    make unreachable was still replayable to any later subscriber via
    Last-Event-ID."""
    hub = WebHub()
    hub.publish_message(
        id=-1, role="assistant", text="секрет до удаления", kind="chat", keyboard=None, ts=NOW
    )

    hub.close_all()

    sub = hub.subscribe(last_event_id=0)
    sub.close()
    assert sub.backlog == []


async def test_close_all_clears_recorded_message_text():
    hub = WebHub()
    hub.register_message_text(-1, "текст до удаления")

    hub.close_all()

    assert hub.text_for(-1) == ""


def test_edit_omits_unset_fields_but_keeps_explicit_none():
    hub = WebHub()
    seq = hub.publish_edit(id=-1, keyboard=None)
    sub = hub.subscribe(last_event_id=seq - 1)
    sub.close()
    data = sub.backlog[0].data
    assert data == {"id": -1, "keyboard": None}
    assert "text" not in data


def test_register_keyboard_with_empty_rows_clears_allowlist():
    hub = WebHub()
    hub.register_keyboard(-1, [[{"text": "Да", "data": "w:resume"}]])
    assert hub.allow_press(-1, "w:resume") is True
    hub.register_keyboard(-1, [])
    assert hub.allow_press(-1, "w:resume") is False


def test_text_for_returns_the_most_recently_registered_text():
    hub = WebHub()
    assert hub.text_for(-1) == ""  # never registered: empty, not missing
    hub.register_message_text(-1, "как ты?")
    assert hub.text_for(-1) == "как ты?"
    hub.register_message_text(-1, "как ты? Хорошо.")
    assert hub.text_for(-1) == "как ты? Хорошо."
