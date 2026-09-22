"""Voice anchors and nickname rotation (phase-5 plan section 5).

Two independent, code-only rotations, both deterministic given their
seed:

- **Voice anchors**: `VOICE_PER_SCENE` lines sampled from `voice.md`,
  seeded by the scene id so they stay stable for the whole scene (a
  cache-friendliness concern noted in app/core/prompt.py's own
  docstring, even though the current model has no implicit cache to
  benefit from it).
- **Nicknames**: a coin flip at `NICKNAME_RATE`, never repeating the
  previous nickname, resolved into the literal instruction line the
  persona prompt carries -- persona.md tells the model to use only the
  address given there, never to invent its own.

`remember_nickname()` is this module's only write, and it is narrow on
purpose: a single `UPDATE user_state SET nickname_last`, not routed
through `update_state()`. A nickname rotation is not a decision worth
an audit row -- it is the same kind of bookkeeping app/core/state.py's
`set_counters()` already carves out an exception for, just written
directly here instead of through that function, because `set_counters`
itself is off limits to this module (tests/test_autonomy_isolation.py:
mood/voice/persona_context must not be able to reach a state writer,
`set_counters` included, even though its allow-list would happen to
admit this column).
"""

from __future__ import annotations

import random
from pathlib import Path

from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserState

# A comment or a blank line is never a voice line or a nickname -- lets
# both files carry a `<!-- ... -->` note or a `#` remark for whoever
# edits them by hand, exactly as persona.md itself does.
_COMMENT_PREFIXES = ("<!--", "#")

# UserState.id is pinned to 1 by a check constraint (ck_user_state_id_
# singleton); spelled out here rather than imported from app.core.state
# (STATE_ID), which this module must not import at all -- see the
# module docstring.
_STATE_ID = 1


def load_lines(path: Path | str) -> list[str]:
    """One trimmed line per non-blank, non-comment line of `path`.

    Shared by voice.md and nicknames.txt: both are "one per line, up to
    a handful, hand-edited" files with the same trivial format, so one
    loader serves both rather than two near-identical ones.
    """
    lines: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(_COMMENT_PREFIXES):
            continue
        lines.append(line)
    return lines


def voice_anchors(lines: list[str], seed: int, k: int) -> list[str]:
    """`k` lines from `lines`, chosen deterministically by `seed`, in file order.

    `random.Random(seed).sample` rather than `.choices`: sampling
    without replacement is what keeps a scene from seeing the same line
    twice, and a plain `random.Random(seed)` (not the module-level
    `random`) is what makes the same scene always draw the same lines
    -- the whole point of seeding by `scene_id`.

    Returned in file order, not sample order, so two scenes that happen
    to draw an overlapping set read them in the same relative sequence
    -- `sample()`'s own output order is an artifact of the algorithm,
    not something worth exposing to the prompt.

    `k` is capped at `len(lines)` here rather than left to the caller,
    so `VOICE_PER_SCENE` set above the file's length is a no-op instead
    of a `sample()` ValueError.
    """
    if not lines:
        return []
    count = min(k, len(lines))
    if count <= 0:
        return []
    rng = random.Random(seed)
    indices = sorted(rng.sample(range(len(lines)), count))
    return [lines[i] for i in indices]


def choose_nickname(
    nicks: list[str], last: str | None, rate: float, rng: random.Random
) -> str | None:
    """A nickname at random, at probability `rate`, never equal to `last`.

    `None` both when the coin comes up "no address this time" *and*
    when there is genuinely no candidate to give -- an empty file, or a
    single-nickname file whose one entry is `last`. Callers do not need
    to tell the two apart: either way, this turn addresses no one by
    name.
    """
    candidates = [nick for nick in nicks if nick != last]
    if not candidates:
        return None
    if rng.random() >= rate:
        return None
    return rng.choice(candidates)


def directive(nick: str | None) -> str:
    """The literal line persona.md tells the model to obey.

    Always one or the other -- never both, never neither -- so the
    model has exactly one instruction to follow about address, not a
    default to guess at when this line is silent.
    """
    if nick is None:
        return "Без обращения в этом ответе."
    return f"Обращение в этом ответе: {nick}"


async def remember_nickname(session: AsyncSession, nick: str) -> None:
    """The one write this module makes: `user_state.nickname_last = nick`.

    A targeted UPDATE, not update_state() -- see the module docstring.
    Callers commit this in the delivery transaction rather than here,
    matching the rest of the codebase's convention of one commit per
    logical step of a turn; the sole caller (app/core/turn.py) commits
    right after this returns.
    """
    await session.execute(
        sql_update(UserState).where(UserState.id == _STATE_ID).values(nickname_last=nick)
    )
