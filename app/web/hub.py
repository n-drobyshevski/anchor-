"""WebHub: the in-process event bus between web-origin bot output and
the browser (web-chat plan track 1, design sections 3 and 7).

Two producers publish through one hub, so the browser never sees a
message twice:

- app/web/sink.py's WebSinkSession, for everything a web-origin turn
  sends (a reply, an edit, the typing indicator, a callback toast).
- app/web/tail.py, for Telegram-origin and proactive `message` rows.

Track 2's GET /api/events (app/web/routes.py, out of this track's
scope) is the only consumer: it calls subscribe(), turns each HubEvent
into an SSE frame with an `id: <seq>` line, and replays the ring buffer
on `Last-Event-ID` reconnect. app/main.py builds exactly one WebHub per
process, alongside the one `web_bot` -- see app/web/sink.py's
module docstring for why a fresh hub per web_bot would be wrong.

Everything here is plain in-memory state behind ordinary dict/deque
operations, never awaited on its own: a single asyncio event loop, one
worker claim loop, and at most a handful of SSE requests are the only
callers, so there is no concurrency to guard against beyond what the
event loop already serializes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import itertools
import time
from collections import deque
from typing import AsyncIterator

RING_BUFFER_SIZE = 200
MAX_SUBSCRIBERS = 3

# A sentinel distinct from `None`, which is itself a meaningful value for
# `keyboard` (an edit that *clears* the buttons). publish_edit() uses this
# to tell "this field did not change, omit it" apart from "this field
# changed to nothing".
_OMITTED = object()


class TooManySubscribers(Exception):
    """Raised by subscribe() at MAX_SUBSCRIBERS live streams.

    Track 2's GET /api/events catches this and answers 429 (the design's
    "SSE: 3 streams" rate limit, section 6) -- the hub enforces the cap
    itself rather than trusting the route to count correctly, since a
    stream that outlives its request handler (a slow client, a dropped
    TCP connection the OS has not noticed yet) would otherwise leak past
    any counter the route kept on its own.
    """


@dataclasses.dataclass(frozen=True)
class HubEvent:
    """One SSE frame. `seq` is both the `id:` line and the replay key."""

    seq: int
    event: str  # "message" | "edit" | "typing" | "toast"
    data: dict


class Subscription:
    """One live SSE stream's handle: async-iterate `events()`, then `close()`.

    Returned by WebHub.subscribe(). Not a context manager on its own
    because track 2's route needs to keep streaming from inside an
    aiohttp StreamResponse loop; the route is responsible for calling
    close() in a `finally`, exactly as app/tg/send.py's typing task
    documents the same contract for its own cancel-in-finally caller.
    """

    def __init__(self, hub: "WebHub", subscriber_id: int, backlog: list[HubEvent]) -> None:
        self._hub = hub
        self._id = subscriber_id
        self.backlog = backlog
        self._closed = False

    async def events(self) -> AsyncIterator[HubEvent]:
        """Yields the replay backlog first, then live events until close()."""
        for record in self.backlog:
            yield record
        queue = self._hub._queue_for(self._id)
        if queue is None:
            return
        while True:
            record = await queue.get()
            if record is None:  # close_all()'s poison pill, or this stream's own close()
                return
            yield record

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._hub._unsubscribe(self._id)


class WebHub:
    """The single process-lifetime hub. See the module docstring."""

    def __init__(self) -> None:
        # Seeded from the wall clock (ms since epoch), not 1: a process
        # restart would otherwise start `seq` over from 1 too, and GET
        # /api/events' replay filter (`record.seq > last_event_id`)
        # would then have no way to tell "an id from a previous boot,
        # replay everything newer" apart from "an id from a stream that
        # is still ahead of the whole buffer, replay nothing" -- a tab
        # that reconnects with a pre-restart Last-Event-ID would
        # silently miss every event published since boot (low-severity
        # finding: "Hub event seq restarts at 1 on every boot"). Seeding
        # from the clock instead keeps `seq` a plain increasing integer
        # (the wire format is still one bare `id: <seq>` line, per the
        # HTTP contract) while guaranteeing it starts above every seq
        # the previous process ever handed out, as long as the wall
        # clock itself does not run backwards across the restart -- so
        # a stale, pre-restart id now compares less than the *entire*
        # current buffer and correctly triggers a full replay instead
        # of none.
        self._seq = itertools.count(int(time.time() * 1000))
        self._buffer: deque[HubEvent] = deque(maxlen=RING_BUFFER_SIZE)
        self._next_subscriber_id = itertools.count(1)
        self._subscribers: dict[int, asyncio.Queue[HubEvent | None]] = {}
        # message_id -> the set of callback_data values the sink most
        # recently issued for it and has not since edited away.
        # Consulted by app/web/ingress.py before a button press may
        # become a synthetic callback_query (design section 2, "Button
        # presses"; design section 7's allowlist).
        self._allowlist: dict[int, set[str]] = {}
        # message_id -> the text the sink most recently sent/edited that
        # message to. Consulted by app/web/ingress.py's press() so a
        # synthetic callback_query's `message.text` carries the real
        # text of the message the button lives under, instead of the
        # empty string WebSinkSession has no other way to reconstruct --
        # app/tg/welfare.py's handle_callback reads `callback.message.
        # text` as the base it appends an acknowledgement to, and an
        # empty base means the whole welfare reply (crisis-support text
        # included) is replaced by just the ack (a correctness finding).
        self._text: dict[int, str] = {}

    # --- publishing: app/web/sink.py, app/web/tail.py ---

    def _publish(self, event: str, data: dict) -> int:
        record = HubEvent(seq=next(self._seq), event=event, data=data)
        self._buffer.append(record)
        for queue in self._subscribers.values():
            queue.put_nowait(record)
        return record.seq

    def publish_message(
        self,
        *,
        id: int,
        role: str,
        text: str,
        kind: str,
        keyboard: list[list[dict]] | None,
        ts: datetime.datetime,
    ) -> int:
        """A new message bubble. `id` is negative for a sink-issued one
        (the aiogram message_id WebSinkSession minted) and the positive
        `message.id` for one app/web/tail.py mirrored from Telegram.
        """
        return self._publish(
            "message",
            {
                "id": id,
                "role": role,
                "text": text,
                "kind": kind,
                "keyboard": keyboard,
                "ts": ts.isoformat(),
            },
        )

    def publish_edit(
        self, *, id: int, text: object = _OMITTED, keyboard: object = _OMITTED
    ) -> int:
        """An in-place edit of message `id`.

        `text` and `keyboard` are each omitted from the event entirely
        when not passed -- not sent as null -- so the client can tell
        "this field is unchanged" from EditMessageReplyMarkup-only edits
        (text unchanged) apart from "this field is now empty"
        (keyboard=None clears the buttons, per the HTTP contract).
        """
        data: dict = {"id": id}
        if text is not _OMITTED:
            data["text"] = text
        if keyboard is not _OMITTED:
            data["keyboard"] = keyboard
        return self._publish("edit", data)

    def publish_typing(self) -> int:
        return self._publish("typing", {})

    def publish_toast(self, text: str) -> int:
        return self._publish("toast", {"text": text})

    # --- subscribing: track 2's GET /api/events ---

    def subscribe(self, last_event_id: int | None = None) -> Subscription:
        """Open a new SSE stream. Raises TooManySubscribers past the cap.

        `last_event_id` replays every buffered event with a greater
        `seq` -- the ring buffer is `RING_BUFFER_SIZE` events deep, so a
        client that reconnects after a longer gap misses the difference
        (design section 3: an accepted, documented gap, not a bug; the
        client's own GET /api/history covers it on reload).
        """
        if len(self._subscribers) >= MAX_SUBSCRIBERS:
            raise TooManySubscribers()
        backlog = (
            [record for record in self._buffer if record.seq > last_event_id]
            if last_event_id is not None
            else []
        )
        subscriber_id = next(self._next_subscriber_id)
        self._subscribers[subscriber_id] = asyncio.Queue()
        return Subscription(self, subscriber_id, backlog)

    def _queue_for(self, subscriber_id: int):
        return self._subscribers.get(subscriber_id)

    def _unsubscribe(self, subscriber_id: int) -> None:
        self._subscribers.pop(subscriber_id, None)

    # --- the callback allowlist: app/web/sink.py writes, app/web/ingress.py reads ---

    def register_keyboard(self, message_id: int, keyboard: list[list[dict]] | None) -> None:
        """Replace the live allowlist for `message_id`; `None` (or an
        empty keyboard) clears it entirely.

        Called by WebSinkSession on every SendMessage, EditMessageText
        and EditMessageReplyMarkup that carries an inline keyboard, so
        the allowlist always reflects the buttons currently live in the
        browser -- a press on a button a later edit replaced or removed
        is refused (design section 2).
        """
        data = {button["data"] for row in (keyboard or []) for button in row}
        if data:
            self._allowlist[message_id] = data
        else:
            self._allowlist.pop(message_id, None)

    def allow_press(self, message_id: int, data: str) -> bool:
        return data in self._allowlist.get(message_id, set())

    def register_message_text(self, message_id: int, text: str) -> None:
        """Remember `message_id`'s current text. Called by WebSinkSession
        on every SendMessage and EditMessageText (never
        EditMessageReplyMarkup, which carries no text of its own and
        must not blank out what is already recorded)."""
        self._text[message_id] = text

    def text_for(self, message_id: int) -> str:
        """The text last recorded for `message_id`, or "" if none --
        matching what a synthetic callback_query would carry for a
        message this hub never saw (design section 2: the field is
        `""` for an unknown id, never missing)."""
        return self._text.get(message_id, "")

    # --- the kill switch: track 2's auth.revoke_all (/weblogout, /delete) ---

    def close_all(self) -> None:
        """End every live SSE stream and drop the callback allowlist.

        The other half of the kill switch the design names (section 4:
        "/weblogout ... closes live SSE streams and the in-memory
        allowlists"; the second adversarial critique's finding 5 makes
        this explicit): revoking every `web_session` row in the database
        does nothing about a stream already open in a browser tab, or a
        button a compromised session could still press against a stale
        but still-registered allowlist entry. This is called from
        track 2's auth.revoke_all, not here, because this hub has no
        idea which streams belong to which session -- it does not need
        to: a revoke ends *all* of them, which is correct for a
        single-user app where "revoked" means "nobody should still be
        watching".
        """
        for queue in self._subscribers.values():
            queue.put_nowait(None)
        self._subscribers.clear()
        self._allowlist.clear()
        self._text.clear()
        # The ring buffer itself, too (the second adversarial critique's
        # finding 4/priority-fix-list item 5, revisited): without this,
        # text a /delete or /weblogout was supposed to make unreachable
        # stayed available to replay via Last-Event-ID to any later
        # subscriber -- verified concretely as `publish_message(...)`
        # then `close_all()` still leaving the published text in
        # `subscribe(0).backlog`. A revoke means "nobody should still be
        # able to see this", which has to include the backlog a brand
        # new stream would otherwise replay, not only the streams that
        # happened to be open at the moment of the call.
        self._buffer.clear()
