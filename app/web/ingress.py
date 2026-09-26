"""Turning a browser request into a synthetic Telegram Update (web-chat
plan track 1, design section 2).

`POST /api/send` and `POST /api/press` (track 2, app/web/routes.py) call
`send_text` / `press` below. Both build an aiogram-shaped Update payload
-- `chat.id` and `from.id` hardcoded to `settings.ALLOWED_CHAT_ID`, never
taken from the request -- and hand it to app/db/queue.py's `enqueue_web`,
which mints the row's negative `update_id` from `web_update_seq` and
stores it. From there app/worker.py's single claim loop treats it exactly
like a Telegram row (see that module's docstring for the one line that
changed: picking `web_bot` by the sign of `row.update_id`).

**Two independent blocks**, per the design's adversarial review (finding
1 and its fix): a text message that would route to `/delete` or
`/export` in the real router (app/tg/router.py's `Command("delete")` /
`Command("export")`) never reaches the queue at all, and neither does a
`d:`-prefixed callback press (the /delete confirm keyboard's own
prefix, app/tg/data.py). This is layer one. Layer two lives in
app/tg/router.py itself: `is_web_sink` guards at the top of
`export_command`, `delete_command` and the `d:` callback handler, so a
future drift in *this* module's tokenization is not the only thing
standing between a stolen web session and a wipe.

`is_blocked_command` replicates `aiogram.filters.Command.extract_command`'s
exact tokenization (verified against aiogram 3.31.0's source: split the
whole text on the first run of whitespace, take the first character as
the prefix, partition the rest of that token on "@" for the command name
and mention) rather than a bespoke regex, precisely so it cannot drift
from what the router will actually execute. `/export foo`, `/export@X
bar`, and `/EXPORT` (checked case-insensitively here, on purpose --
see that function's docstring) are all caught.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import queue
from app.tg.checkin import WEB_SUBMIT_CALLBACK
from app.web.hub import WebHub

# The commands the design blocks on the web (design section 2 and
# section 13's decision #3): a stolen web session must not be able to
# wipe the data or produce a bulk export. Matched case-insensitively
# against the command name only, after the "/" prefix and any "@mention"
# have been stripped -- see is_blocked_command.
#
# `planner_link` joins this set for the same reason: it sends back a
# live OAuth authorize URL that completes a credential link on
# whichever browser opens it, so a stolen web session must not be able
# to trigger or read that reply either -- see planner_link_command's
# own is_web_sink guard in app/tg/router.py for the second, redundant
# layer, matching /delete's and /export's own belt-and-braces shape.
#
# `grok` joins it for the same reason as `planner_link`: its reply
# becomes a capability URL granting read access to the data
# (app/tg/grok.py), which must only ever be shown in Telegram. `claude`
# joins it because `/claude connect <code>` *approves* an OAuth
# connection (app/tg/claude.py): Telegram is the only approval channel.
BLOCKED_COMMANDS = frozenset({"delete", "export", "planner_link", "grok", "claude"})

# The /delete confirm keyboard's callback_data prefix (app/tg/data.py's
# confirm_keyboard: "d:yes:<epoch>" / "d:no"). Rejected outright rather
# than checked against the allowlist, so it can never even be issued a
# synthetic Update -- the /delete confirmation keyboard itself is never
# sent by WebSinkSession in the first place (delete_command's is_web_sink
# guard fires before send_keyboard is ever called), so this can only
# ever fire on a forged press. `g:` is the /grok picker's prefix
# (app/tg/grok.py) and `cl:` the /claude window picker's
# (app/tg/claude.py), both blocked for the reasons BLOCKED_COMMANDS
# gives. `v:` is a vault hold's [Да]/[Нет, вернуть] (app/tg/vault.py):
# accepting a rule or a mass forget is Telegram's alone (phase-8 plan
# section 8), and hold messages are never sent to the web chat. A
# tuple, because str.startswith takes one.
BLOCKED_CALLBACK_PREFIX = ("d:", "g:", "cl:", "v:")


class BlockedCommand(Exception):
    """A /delete- or /export-shaped request, refused before being queued.

    Track 2's POST /api/send and POST /api/press handlers catch this and
    answer 422 {"error": "blocked"}.
    """


class PressRejected(Exception):
    """A button press whose `data` is not currently on the hub's
    allowlist for `message_id` -- stale, replayed, or forged.

    Track 2's POST /api/press handler catches this and answers 409
    {"error": "stale"}.
    """


def _tokenize_command(text: str) -> tuple[str, str, str | None] | None:
    """Replicate `Command.extract_command`'s tokenization exactly.

    Returns `(prefix, command, mention)` or `None` for anything
    `extract_command` itself would raise `CommandException` on (an
    empty or whitespace-only string) -- which the real `Command` filter
    treats as "not a command", so this module treats it the same way:
    not blocked, falls through to an ordinary chat turn.
    """
    tokens = text.split(maxsplit=1)
    if not tokens:
        return None
    full_command = tokens[0]
    prefix = full_command[0]
    command, _, mention = full_command[1:].partition("@")
    return prefix, command, mention or None


def is_blocked_command(text: str) -> bool:
    """True iff `text` would route to /delete or /export in the real
    router, or is close enough that ingress should refuse to take the
    risk (design's adversarial review, finding 1).

    Case-insensitive on purpose, even though app/tg/router.py's
    `Command("delete")` / `Command("export")` are not (their default
    `ignore_case=False`): the router's exact case sensitivity is an
    implementation detail this module must not depend on staying fixed
    forever, and the only direction that matters for safety is
    over-blocking, never under-blocking. Refusing a string the router
    would not itself have executed -- "/Export" typed by hand -- costs
    nothing but a slightly-too-cautious 422; the reverse would be the
    bug the whole design exists to prevent.

    The `@mention` part is stripped and ignored, not validated against
    the bot's own username: this chat is always the single allowed
    private chat, so there is no second bot a mention could correctly
    disambiguate against, and refusing to guess is the safe default.
    """
    parsed = _tokenize_command(text)
    if parsed is None:
        return False
    prefix, command, _mention = parsed
    if prefix != "/":
        return False
    return command.casefold() in BLOCKED_COMMANDS


def build_message_update(update_id: int, text: str, settings: Settings) -> dict:
    """The synthetic Update payload for a web-origin text message.

    Shaped exactly like a Telegram webhook body for a private-chat text
    message (cross-checked against tests/test_worker.py's own
    `_update_payload` helper and `aiogram.types.Update`'s schema), with
    no `entities` field -- the design's grounded claim that aiogram's
    `Command` filter reads only `message.text`/`message.caption`, never
    `entities`, is what makes that safe for every command this bot has.

    `message_id` is set to `update_id` itself (always negative, from
    `web_update_seq`): the two never need to differ, since nothing reads
    a web-origin message's `message_id` for anything but identity, and
    reusing `update_id` avoids minting a second id space for no reason.
    """
    chat_id = settings.ALLOWED_CHAT_ID
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Web"},
            "text": text,
        },
    }


def build_callback_update(
    update_id: int, message_id: int, data: str, settings: Settings, *, text: str = ""
) -> dict:
    """The synthetic Update payload for a web button press.

    `message_id` is the id WebSinkSession minted for the message that
    carried the button (design section 2, "Button presses") -- the
    browser sends it back in POST /api/press, and it is what
    app/tg/router.py's callback handlers read as `callback.message.
    message_id`. `chat_instance` and the callback query's own id are
    fixed, opaque constants: nothing in the router or app/core/ reads
    either for meaning, only for presence (app/tg/send.py's
    answer_callback needs *an* id to answer).

    `text` is the message's real current text -- `press()` below reads
    it from the hub (`WebHub.text_for`, the same thing WebSinkSession
    recorded when it sent or last edited this message) rather than
    leaving it hardcoded empty. A callback handler that appends an
    acknowledgement to `callback.message.text` (app/tg/welfare.py's
    handle_callback is the concrete case: a welfare reply's `w:resume`/
    `w:stay` buttons) needs that base text to still be there, or the
    edit it issues replaces the whole reply -- including any
    crisis-support text -- with just the ack (a correctness finding).
    """
    chat_id = settings.ALLOWED_CHAT_ID
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"web-{-update_id}",
            "from": {"id": chat_id, "is_bot": False, "first_name": "Web"},
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id, "is_bot": True, "first_name": "Anchor"},
                "text": text,
            },
            "chat_instance": "web",
            "data": data,
        },
    }


async def send_text(
    session: AsyncSession, *, settings: Settings, text: str, client_key: str
) -> int:
    """Enqueue a web-origin text message. Returns its update_id.

    Idempotent on `client_key` end to end (app/db/queue.py's
    `enqueue_web`): POST /api/send's "Не отправлено — повторить" retry
    sends the same client_key and gets back the same update_id, never a
    second queued row.

    Raises BlockedCommand for /delete, /export (any case, any @mention,
    with or without arguments) before a row is ever written.
    """
    if is_blocked_command(text):
        raise BlockedCommand(text)

    def build(update_id: int) -> dict:
        return build_message_update(update_id, text, settings)

    return await queue.enqueue_web(session, build, client_key)


async def press(
    session: AsyncSession, hub: WebHub, *, settings: Settings, message_id: int, data: str
) -> int:
    """Enqueue a web-origin button press as a synthetic callback_query.

    No `client_key`: the HTTP contract gives POST /api/press none, so
    every call inserts a fresh row (app/db/queue.py's enqueue_web with
    client_key=None never conflicts -- NULL is never equal to NULL under
    the partial unique index the migration creates, so this is a plain
    insert every time, not an accidental dedup).

    Two rejections, both before anything is written: `d:` (the /delete
    confirm keyboard's prefix) is refused outright, and anything not
    currently on the hub's allowlist for this exact `message_id` --
    never issued, or replaced by a later edit -- raises PressRejected.
    """
    if data.startswith(BLOCKED_CALLBACK_PREFIX):
        raise BlockedCommand(data)
    if not hub.allow_press(message_id, data):
        raise PressRejected(data)

    text = hub.text_for(message_id)

    def build(update_id: int) -> dict:
        return build_callback_update(update_id, message_id, data, settings, text=text)

    return await queue.enqueue_web(session, build, None)


async def checkin_complete(session: AsyncSession, *, settings: Settings, message_id: int) -> int:
    """Enqueue the synthetic completion press for a check-in filled on
    the web (W4). Returns its update_id.

    `message_id` is the negative id app/web/panels/checkin.py minted
    with `queue.reserve_web_id` and stored as the check-in row's
    `tg_message_id` in the same request (core `checkin.submit`, which
    also stored the note, if any). The data is WEB_SUBMIT_CALLBACK, so
    app/tg/checkin.py's `_current()` staleness check accepts this press
    and `finish_and_react` finishes exactly that check-in, by its id
    (core `finish_submitted`) -- never whatever the global note step
    happens to point at, and in queue order, so nothing queued before
    this row can be taken for the check-in's note.

    **No hub allowlist check, unlike `press`**, on purpose: that check
    exists to refuse a *browser-supplied* (message_id, data) pair that
    the sink never issued. Here neither value comes from the browser --
    the data is a fixed constant and the id was minted server-side a
    moment ago -- and no message carrying this button was ever sent
    through the sink, so the allowlist could never contain it (and so
    a browser can never press it through `press`). `client_key=None`:
    every call is a fresh row; the panel's in-progress check (409,
    under its lock) is what stops a double submit, and
    `finish_submitted` is what makes a replay finish nothing.
    """
    def build(update_id: int) -> dict:
        return build_callback_update(update_id, message_id, WEB_SUBMIT_CALLBACK, settings)

    return await queue.enqueue_web(session, build, None)
