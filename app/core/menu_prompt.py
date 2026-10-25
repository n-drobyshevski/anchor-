"""The /menu text prompt: one pending "reply with text" step (menu redesign).

A /menu button that needs typed input («✏️ Запомнить», «✏️ Часовой
пояс», «✏️ Новая договорённость › Каждый день», ...) cannot carry that
input in its callback data, so the bot asks for it and waits for the
next message. The wait lives in the same three `user_state` columns the
check-in note step and 5c's counter step already use, because they are
already the codebase's one "the next plain text is not chat" switch and
every rule that ends such a step already applies to them:

- `awaiting = 'menu:<key>'`, where `<key>` is an app/tg/menu.ACTIONS key
  of kind "prompt". The key, never any text, so the audit row and the
  debug view (`debug.user_state.awaiting`) stay content-free.
- `awaiting_ref` = the prompt message's own message id, so whatever
  ends the prompt can edit it to «Ответ принят.» / «Отменено.» instead of
  leaving a dead "Напиши..." on screen.
- `awaiting_at` = when the prompt was opened (the only reader of that
  column): a prompt older than MENU_PROMPT_TTL_MIN is not live, and the
  message that finds it so is ordinary chat.

What ends a prompt is everything that already ends the other steps --
app/core/checkin.py's `clear_awaiting` clears all three columns, and it
runs on any slash command (app/tg/router.py's outer middleware), on the
☰ Меню button, on every menu tap, on a pause word (app/core/turn.py's
step 0c) and on each web panel write -- plus the prompt's own
[✕ Отмена] and its expiry.

Pure reads and two writes; no aiogram, no Telegram. The router owns the
dispatch, exactly as app/tg/menu.py's own docstring draws that line.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.checkin import clear_awaiting
from app.core.clock import Clock
from app.core.state import STATE_ID, update_state
from app.db.models import Job, UserState

PREFIX = "menu:"


@dataclass(frozen=True)
class Prompt:
    """A pending menu prompt, as read from `user_state`."""

    key: str
    message_id: int | None
    opened_at: datetime.datetime | None

    def live(self, now: datetime.datetime, ttl_min: int) -> bool:
        """Opened less than `ttl_min` minutes before `now`. A prompt with
        no timestamp (a row written before `awaiting_at` existed, or a
        half-written one) is never live: the safe reading of "we do not
        know how old this is" is "too old"."""
        if self.opened_at is None:
            return False
        return now - self.opened_at < datetime.timedelta(minutes=ttl_min)


def awaiting_value(key: str) -> str:
    return f"{PREFIX}{key}"


def parse(awaiting: str | None) -> str | None:
    """The menu key in an `awaiting` value, or None for anything else
    (no step pending, the check-in note, the counter step)."""
    if not awaiting or not awaiting.startswith(PREFIX):
        return None
    key = awaiting[len(PREFIX):]
    return key or None


async def current(session: AsyncSession) -> Prompt | None:
    """The pending menu prompt, or None. Tolerates a missing user_state
    row, like `clear_awaiting`: the plain-text handler asks this before
    every chat turn, and "no row yet" means "nothing pending"."""
    result = await session.execute(select(UserState).where(UserState.id == STATE_ID))
    state = result.scalar_one_or_none()
    if state is None:
        return None
    key = parse(state.awaiting)
    if key is None:
        return None
    return Prompt(key=key, message_id=state.awaiting_ref, opened_at=state.awaiting_at)


async def open_prompt(session: AsyncSession, clock: Clock, key: str, message_id: int) -> None:
    """Start waiting for `key`'s text. Replaces whatever step was
    pending -- the caller has already retired an older menu prompt's
    message (a menu tap ends it anyway), and a check-in note step or a
    counter step loses to a menu tap exactly as it does today. Through
    `update_state`, like `set_awaiting_note`, so each write has its
    audit row (a key, an id and a timestamp -- never text)."""
    await update_state(session, "awaiting", awaiting_value(key), "button")
    await update_state(session, "awaiting_ref", message_id, "button")
    await update_state(session, "awaiting_at", clock.now_utc(), "button")


async def close_prompt(session: AsyncSession, key: str | None = None) -> bool:
    """End the pending menu prompt -- only if it is `key`'s, when `key`
    is given, so a reply that ran a command which opened a step of its
    own is never undone by the bookkeeping after it. Returns True iff
    something was cleared. Never touches a non-menu step."""
    prompt = await current(session)
    if prompt is None or (key is not None and prompt.key != key):
        return False
    await clear_awaiting(session)
    return True


# --- one-shot cards -----------------------------------------------------

# A confirmation card («Точно?» [Да][Нет]) must act once however many
# times its button is pressed. Two quick taps on «Да» arrive as two
# updates with two different update_ids, so the replay gate every
# command already has (`_once`, keyed on the update) cannot see the
# second as a repeat: the first press edits the card to «Готово.», but
# the second was already on its way, and would run /export twice or send
# a second, wrong, code to `/claude connect`. The claim below is keyed on
# the *card* instead.
CARD_CLAIM_KIND = "menu_card"


def card_claim_key(message_id: int) -> str:
    """Keyed on the card's message id alone: the bot has one chat, and a
    web-issued id is negative, so the sign already tells a web card from
    a Telegram one. No chat id, because `dedup_key` is visible in the
    content-free `debug.job` view."""
    return f"menu_card:{message_id}"


async def claim_card(session: AsyncSession, message_id: int, update_id: int) -> bool:
    """Atomically claim a confirmation card for the press `update_id`.

    The `job` table already is the codebase's "insert once, keyed on a
    unique string" ledger (`dedup_key`, ON CONFLICT DO NOTHING -- the
    same mechanism `enqueue_job` and `scene:<id>` rely on), so the claim
    is one such row, written already `done` so the worker never claims
    it. It holds two integers and a constant: no text, no token, no
    chat id.

    Returns True for the press that wins -- and for a *replay of that
    same press* (the queue re-runs an update after a crash, and the
    command behind «Да» is itself `_once`-gated on the update, so
    running it again is the correct recovery) -- and False for any other
    press on the same card, which the caller answers «Устарело.».

    The rows age out with the rest of the finished jobs (app/core/
    retention.py). A card is edited to «Готово.» or «Отменено.» as soon
    as it is claimed, so nothing is left to press by then.
    """
    key = card_claim_key(message_id)
    stmt = (
        pg_insert(Job)
        .values(
            kind=CARD_CLAIM_KIND,
            payload={"update_id": update_id},
            dedup_key=key,
            status="done",
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(Job.id)
    )
    inserted = (await session.execute(stmt)).first() is not None
    if inserted:
        await session.commit()
        return True
    result = await session.execute(select(Job.payload).where(Job.dedup_key == key))
    payload = result.scalar_one_or_none()
    await session.commit()
    return isinstance(payload, dict) and payload.get("update_id") == update_id
