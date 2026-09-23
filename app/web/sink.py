"""WebSinkSession: a synthetic aiogram BaseSession that turns Bot-API
method calls into WebHub events instead of network requests (web-chat
plan track 1, design sections 0 and 2).

This is the load-bearing trick the whole design rests on, and it is not
a new one: tests/conftest.py's `FakeSession(BaseSession)` has done
exactly this since Phase 1, so that turn-pipeline tests never touch the
network. app/core/turn.py, app/tg/router.py and friends send through
`message.bot` / `callback.bot` without knowing or caring what kind of
Bot they were handed -- every write path is already this decoupled from
transport, which is what makes swapping the session sufficient and
`app/core/*` genuinely unchanged.

`make_web_bot()` marks the resulting `Bot` with `is_web_sink = True`
(aiogram's `Bot` has no `__slots__`, so a plain attribute works). Two
things key off that flag elsewhere in this codebase:

- app/tg/send.py's `edit_keyboard`: a negative message_id sent to any
  *other* Bot is refused outright, closing the cross-transport bug the
  design's adversarial review found (a check-in or proposal issued
  through the web sink, later retired over Telegram, would otherwise
  400 three times and lose the note).
- app/tg/router.py's `export_command`/`delete_command`/`delete_decision`
  guards: the second, independent layer of defense against /export and
  /delete, so their safety does not rest solely on app/web/ingress.py's
  ingress-side text match staying in sync with the router forever.
"""

from __future__ import annotations

import datetime
import itertools
import time
from typing import AsyncGenerator

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendChatAction,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message as TgMessage

from app.web.hub import WebHub

# Buttons whose callback_data starts with this prefix are the welfare
# reply's own keyboard (app/tg/welfare.py's welfare_keyboard). Detecting
# it is the one place this module infers `kind` at all -- see
# _infer_kind's docstring for why it is inference, not ground truth.
_WELFARE_PREFIX = "w:"


def _keyboard_to_hub(markup: InlineKeyboardMarkup | None) -> list[list[dict]] | None:
    """InlineKeyboardMarkup -> the HTTP contract's [[{text, data}]] shape.

    Only callback-data buttons are carried across (design section 3:
    "keyboard as [{text, data}] for callback buttons only") -- this
    codebase never sends a URL or web_app button, so filtering to
    `callback_data is not None` is a no-op for every real call site and
    a safety net against ever silently forwarding one that isn't.
    Rows that end up with no buttons at all are dropped; a markup that
    ends up with no rows collapses to `None`, matching "no keyboard"
    exactly the way a genuinely absent one would.
    """
    if markup is None:
        return None
    rows = []
    for row in markup.inline_keyboard:
        buttons = [
            {"text": button.text, "data": button.callback_data}
            for button in row
            if button.callback_data is not None
        ]
        if buttons:
            rows.append(buttons)
    return rows or None


def _infer_kind(keyboard: list[list[dict]] | None) -> str:
    """A best-effort UI hint for the browser (plan section 10's muted
    styling for welfare/canned rows), never ground truth.

    WebSinkSession only ever sees a raw Bot-API method call -- it has no
    access to the `message.kind` column app/core/turn.py writes
    alongside the same reply, in a separate DB write this session never
    touches. Detecting the welfare keyboard's own `w:` callback prefix
    is the one signal cheap and reliable enough to use here; everything
    else (a plain persona reply, a canned command reply, a checkin
    reply) is indistinguishable from this vantage point and reported as
    "chat". app/web/tail.py's mirrored rows read the real column and are
    authoritative wherever the two would disagree -- but a sink-issued
    live event is by definition never also mirrored (design section 3:
    tail.py excludes web-origin rows), so no event this module publishes
    is ever contradicted by a later, more accurate one.
    """
    if keyboard and any(
        button["data"].startswith(_WELFARE_PREFIX) for row in keyboard for button in row
    ):
        return "welfare"
    return "chat"


class WebSinkSession(BaseSession):
    """Captures outgoing Bot-API methods as WebHub events; never makes an
    HTTP request. `close()` is a no-op, exactly like FakeSession's.

    Handles exactly the methods a web-origin turn can produce: sending a
    reply (with or without an inline keyboard), editing one in place,
    the typing indicator, and answering a callback (whose `text`, if
    any, becomes a toast). Anything else -- SendDocument above all,
    since /export's only route to the network is through it -- raises
    NotImplementedError, loudly, the same contract FakeSession already
    holds tests to. A `/export` that somehow reached this far (both of
    app/tg/router.py's and app/web/ingress.py's blocks would have to
    fail at once) still cannot leave this process with any data.
    """

    def __init__(self, hub: WebHub) -> None:
        super().__init__()
        self._hub = hub
        # Always negative, decrementing, and seeded from the wall clock
        # (ms since epoch) rather than a bare -1: app/tg/send.py's
        # edit_keyboard guard and app/db/models.py's TelegramUpdate both
        # key off the *sign* of an id to tell a web-issued one apart
        # from a real Telegram id, but the browser also keys its own
        # dedupe/edit map (`rendered`, app.js) by this exact id, and
        # that map can outlive one process -- an EventSource reconnects
        # on its own after a redeploy, with no page reload and no
        # `rendered` clear. Restarting the counter at -1 every boot
        # (the previous behaviour) would then collide with ids a tab
        # already rendered from the *previous* boot: the first reply
        # after a restart gets silently dropped as "already seen", and
        # a later edit for the same reused id rewrites the wrong bubble
        # (high-severity finding: "Sink message ids restart at -1 on
        # every process start"). Seeding from a millisecond timestamp
        # instead keeps ids negative and strictly decreasing *within*
        # this process (uniqueness there was never the problem) while
        # also being, with overwhelming probability, below every id any
        # earlier boot ever minted -- the same fix WebHub's own `_seq`
        # uses for the identical reason, and still far inside 2**53's
        # JS-safe-integer ceiling.
        self._next_message_id = itertools.count(-(time.time_ns() // 1_000_000), -1)

    async def close(self) -> None:
        pass

    async def make_request(
        self, bot: Bot, method: TelegramMethod, timeout: int | None = None
    ):
        now = datetime.datetime.now(datetime.timezone.utc)

        if isinstance(method, SendMessage):
            message_id = next(self._next_message_id)
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            keyboard = _keyboard_to_hub(markup)
            self._hub.register_keyboard(message_id, keyboard)
            self._hub.register_message_text(message_id, method.text)
            self._hub.publish_message(
                id=message_id,
                role="assistant",
                text=method.text,
                kind=_infer_kind(keyboard),
                keyboard=keyboard,
                ts=now,
            )
            return TgMessage.model_validate(
                {
                    "message_id": message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                },
                context={"bot": bot},
            )

        if isinstance(method, EditMessageText):
            if method.message_id is not None and method.message_id >= 0:
                # Not a sink-issued id: some other write path (app/tg/
                # checkin.py's retire, a proposal) is editing what is
                # really a *positive*, real Telegram message_id -- e.g.
                # a check-in prompt sent over Telegram whose note then
                # happens to arrive over the web, so `message.bot` for
                # that turn is this sink. Nothing this hub ever
                # published owns this id (the browser keys bubbles by
                # DB `message.id`, an unrelated positive number that
                # can coincide with this one by pure chance), so
                # touching the allowlist or publishing an edit for it
                # would silently rewrite an unrelated bubble and leave
                # the *real* Telegram keyboard still live (a correctness
                # finding). Treat it exactly like send.py's edit_keyboard
                # already documents for the reverse direction: an edit
                # of an id this session does not own is a harmless,
                # already-applied no-op, not a hub event.
                return TgMessage.model_validate(
                    {
                        "message_id": method.message_id,
                        "date": 0,
                        "chat": {"id": method.chat_id, "type": "private"},
                        "text": method.text,
                    },
                    context={"bot": bot},
                )
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            keyboard = _keyboard_to_hub(markup)
            self._hub.register_keyboard(method.message_id, keyboard)
            self._hub.register_message_text(method.message_id, method.text)
            self._hub.publish_edit(id=method.message_id, text=method.text, keyboard=keyboard)
            return TgMessage.model_validate(
                {
                    "message_id": method.message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                },
                context={"bot": bot},
            )

        if isinstance(method, EditMessageReplyMarkup):
            if method.message_id is not None and method.message_id >= 0:
                # Same reasoning as EditMessageText above.
                return True
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            keyboard = _keyboard_to_hub(markup)
            self._hub.register_keyboard(method.message_id, keyboard)
            self._hub.publish_edit(id=method.message_id, keyboard=keyboard)
            return True

        if isinstance(method, SendChatAction):
            self._hub.publish_typing()
            return True

        if isinstance(method, AnswerCallbackQuery):
            if method.text:
                self._hub.publish_toast(method.text)
            return True

        raise NotImplementedError(f"WebSinkSession cannot handle {method!r}")

    async def stream_content(
        self,
        url: str,
        headers: dict | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError
        yield b""  # pragma: no cover


def make_web_bot(token: str, hub: WebHub) -> Bot:
    """A Bot wired to a fresh WebSinkSession over `hub`, flagged so every
    `is_web_sink` guard in this codebase fires for it.

    `token` is the real bot token (app/main.py/track 2 passes
    settings.TELEGRAM_BOT_TOKEN) but WebSinkSession never makes an HTTP
    call with it -- aiogram's Bot constructor only needs a validly
    shaped token string, the same reasoning tests/conftest.py's
    make_bot() documents for FakeSession.
    """
    bot = Bot(token=token, session=WebSinkSession(hub))
    bot.is_web_sink = True
    return bot
