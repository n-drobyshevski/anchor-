"""One gatherer for mood, voice and nickname (phase-5 plan section 5; implementation plan section 2).

`gather()` is the single place that turns `user_state` and the clock
into the three per-turn things a persona prompt carries beyond what
Phase 1-4 already built: a mood, this scene's voice anchors, and
whether (and how) to address the user by name. app/core/turn.py's chat
branch and app/core/outbound_send.py's `build_outbound_messages` both
call it, so eval -- which goes through those same builders -- sees the
real prompt rather than a lookalike (the same reasoning that keeps
`build_outbound_messages` itself factored out of the send job).

**Only ever called on a persona path.** Neutral mode and welfare turns
never call this -- there is nothing here for either of them: neutral
mode has no persona to color, and a welfare turn already discards its
generation and speaks out of character. Callers enforce this by simply
not calling `gather()` on those branches, the same way app/core/turn.py
already skips memory retrieval outside the persona branch.

**The boundary retry reuses the same context.** app/core/turn.py calls
`gather()` once per *turn*, not once per model call within it, and
passes the same `PersonaContext` into both `build_messages()` calls
when a boundary violation forces a retry -- re-rolling the nickname or
the voice lines on a retry would let the same turn address the user two
different ways, and a fresh mood computation could theoretically read a
different DB state mid-turn for no reason a user could see.

`amendments`, `orders` and `notebook` are typed and default empty here
already, even though nothing populated them before 5b -- so 5b only
extends this module's own `gather()` (below) to fill `notebook`, and
5c/5d will do the same for `orders`/`amendments`, without changing this
dataclass's shape.
"""

from __future__ import annotations

import dataclasses
import datetime
import random

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import callbacks as callbacks_module
from app.core import mood as mood_module
from app.core import notebook as notebook_module
from app.core import orders as orders_module
from app.core import voice as voice_module
from app.core.clock import Clock
from app.core import clock as clock_module
from app.core.prompt import REPO_ROOT
from app.db.models import PersonaAmendment, UserState
from sqlalchemy import select


@dataclasses.dataclass(frozen=True)
class PersonaContext:
    """Everything `gather()` computes for one persona turn.

    `amendments` is still a 5d placeholder: an empty tuple here until
    that milestone's own gatherer populates it. `notebook` is 5b's, and
    `orders`/`orders_yesterday` are 5c's: `gather()` below fills all
    three from `app.core.notebook.active_entries()` and
    `app.core.orders.active_orders()`/`yesterday_tally()`. All four
    exist on this dataclass already so `build_messages`
    (app/core/prompt.py) always has a stable shape to be fed from.
    """

    mood: str | None
    voice_lines: list[str]
    nickname: str | None
    nickname_directive: str | None
    amendments: tuple[str, ...] = ()
    orders: tuple[str, ...] = ()
    orders_yesterday: str | None = None
    notebook: dict[str, list[str]] | None = None
    # 5e: the callback text for "## Можно вспомнить" and the memory id
    # it came from -- None/None when this turn did not check (not a
    # chat persona turn) or checked and found nothing to offer. The id
    # is threaded back out so app/core/turn.py can dedupe it out of
    # "## Может быть важно" and, after delivery, pass it to
    # app/core/callbacks.py's mark_delivered().
    callback: str | None = None
    callback_memory_id: int | None = None


async def gather(
    session: AsyncSession,
    settings: Settings,
    state: UserState,
    clock: Clock,
    *,
    scene_id: int | None,
    exclude_update_id: int | None,
    rng: random.Random,
    user_text: str = "",
    enable_callback: bool = False,
) -> PersonaContext:
    """Compute one turn's mood, voice anchors, nickname -- and, from 5e, callback.

    `exclude_update_id` is threaded straight into `mood.load_mood_facts`
    -- see that module's docstring for why the current turn's own user
    message must be excluded from "when did the user last write".

    `scene_id` seeds the voice-anchor draw when there is one; an
    outbound send that has no scene yet at gather-time (there is always
    one by the time `run_send_outbound` calls this, in practice) falls
    back to the local calendar date's ordinal, so the anchors are still
    stable across the day rather than reshuffling on every call.

    `rng` is injected rather than read from the module-level `random`,
    so a caller (and its tests) controls the nickname coin flip
    directly instead of monkeypatching a global.

    5e: `enable_callback` defaults to False, so every existing caller
    (both of app/core/outbound_send.py's, whose `scene_id` is `None` in
    practice anyway) keeps getting no callback at all without having to
    say so. app/core/turn.py's chat branch is the only caller that ever
    passes `enable_callback=True`, and only for an ordinary chat turn --
    never a check-in's synthetic line, never neutral mode (which never
    calls `gather()` at all), never a welfare turn (same). When it is
    False, `user_text` is never even read.
    """
    facts = await mood_module.load_mood_facts(
        session, state, clock, exclude_update_id=exclude_update_id
    )
    computed_mood = mood_module.mood(state, facts, clock.now_utc())

    voice_lines = voice_module.load_lines(REPO_ROOT / settings.VOICE_FILE)
    seed = (
        scene_id
        if scene_id is not None
        else clock_module.local_date(clock, state.timezone).toordinal()
    )
    anchors = voice_module.voice_anchors(voice_lines, seed, settings.VOICE_PER_SCENE)

    nicknames = voice_module.load_lines(REPO_ROOT / settings.NICKNAMES_FILE)
    nickname = voice_module.choose_nickname(
        nicknames, state.nickname_last, settings.NICKNAME_RATE, rng
    )
    # No directive at all when the file is empty (plan section 5's
    # "empty file = never use nicknames") -- not merely "без обращения"
    # every time, which would still be a line persona.md has to read and
    # obey for a feature that was never configured.
    nickname_directive = voice_module.directive(nickname) if nicknames else None

    # 5b: the notebook (plan section 6). Texts only, no ids -- "the IDs
    # are not shown in the persona prompt" is the implementation plan's
    # own design decision, and app/core/prompt.py's `_notebook_lines`
    # already expects exactly this shape: a dict of the three kinds to
    # plain text lists.
    view = await notebook_module.active_entries(session)
    notebook = {
        "intentions": [text for _, text, _ in view.intentions],
        "observations": [text for _, text, _ in view.observations],
        "threads": [text for _, text, _ in view.threads],
    }

    # 5c: active standing orders (plan section 7's "## Договорённости")
    # and yesterday's tally for the "now" block. Texts and cadence
    # labels only, same "no ids in the persona prompt" rule the
    # notebook already follows -- app/core/orders.py's own ids exist for
    # the `so:*` callbacks, never for the chat model.
    order_rows = await orders_module.active_orders(session)
    order_lines = tuple(
        f"«{row.text}» ({orders_module.cadence_label(row.cadence, row.weekday)})"
        for row in order_rows
    )
    yesterday = clock_module.local_date(clock, state.timezone) - datetime.timedelta(days=1)
    orders_yesterday = await orders_module.yesterday_tally(session, yesterday)

    # 5d: active persona amendments (phase-5 plan section 9's "## Поправки
    # (одобрены тобой)"). Texts only, oldest-adopted first -- same "no
    # ids in the persona prompt" rule notebook/orders already follow.
    amendment_rows = await session.execute(
        select(PersonaAmendment.text)
        .where(PersonaAmendment.status == "active")
        .order_by(PersonaAmendment.id)
    )
    amendments = tuple(row[0] for row in amendment_rows.all())

    # 5e: the callback (plan section 11a). select_callback() itself
    # already returns None whenever scene_id is None or this scene
    # already had its one callback -- enable_callback only decides
    # whether this turn asks at all.
    callback: str | None = None
    callback_memory_id: int | None = None
    if enable_callback:
        picked = await callbacks_module.select_callback(
            session,
            settings,
            clock,
            user_text=user_text,
            scene_id=scene_id,
            callback_scene=state.callback_scene,
        )
        if picked is not None:
            callback_memory_id, callback = picked

    return PersonaContext(
        mood=computed_mood,
        voice_lines=anchors,
        nickname=nickname,
        nickname_directive=nickname_directive,
        notebook=notebook,
        orders=order_lines,
        orders_yesterday=orders_yesterday,
        amendments=amendments,
        callback=callback,
        callback_memory_id=callback_memory_id,
    )
