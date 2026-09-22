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
already, even though nothing populates them until milestones 5b-5d --
so those milestones extend this dataclass's *callers*, not its shape.
"""

from __future__ import annotations

import dataclasses
import random

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import mood as mood_module
from app.core import voice as voice_module
from app.core.clock import Clock
from app.core import clock as clock_module
from app.core.prompt import REPO_ROOT
from app.db.models import UserState


@dataclasses.dataclass(frozen=True)
class PersonaContext:
    """Everything `gather()` computes for one persona turn.

    `amendments`, `orders` and `notebook` are 5b-5d placeholders: empty
    tuples/None here, always, until those milestones' own gatherers
    populate them. They exist on this dataclass now so `build_messages`
    (app/core/prompt.py) already has a stable shape to be fed from.
    """

    mood: str | None
    voice_lines: list[str]
    nickname: str | None
    nickname_directive: str | None
    amendments: tuple[str, ...] = ()
    orders: tuple[str, ...] = ()
    notebook: dict[str, list[str]] | None = None


async def gather(
    session: AsyncSession,
    settings: Settings,
    state: UserState,
    clock: Clock,
    *,
    scene_id: int | None,
    exclude_update_id: int | None,
    rng: random.Random,
) -> PersonaContext:
    """Compute one turn's mood, voice anchors and nickname.

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

    return PersonaContext(
        mood=computed_mood,
        voice_lines=anchors,
        nickname=nickname,
        nickname_directive=nickname_directive,
    )
