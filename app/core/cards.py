"""Pending research cards: list, view, adopt, reject (phase-4 plan sections 4, 9, 12).

**This is the deliberate outlet of the research valve.** Plan section 0
draws the whole pipeline as a one-way pressure gate: web text goes in,
and the only thing that ever comes back out is the text of a card the
user explicitly pressed [Принять] on. Every other module upstream --
fetch, distill, risk -- exists to narrow what can reach this file.
This file is where that narrowed thing is finally allowed to become
something the persona can see.

**What this module is allowed to write, and nothing else:** a single
`memory(kind='technique', source='adopt')` row per adoption, and a
`state_change` audit row via `app.core.state.record_change`. It must
never import `app.core.state.update_state` (the user_state writer) or
any outbound-planning module -- plan section 12: "Adopting writes only
`memory(kind='technique')`. Nothing in research writes `persona.md`,
`user_state`, rules, commitments, or outbound." tests/test_cards.py
pins this with the same AST-walk tests/test_research_isolation.py uses
for the package this module sits just downstream of.

**A hidden card is unreachable through every function here.**
`get_card` filters it out by status; `adopt`/`reject` refuse it as
`FORBIDDEN` even though, under `ck_study_card_high_is_hidden`, a
hidden status and a `risk_final='high'` card are actually the same
card by construction. Both checks are kept anyway: `study_card` has an
`id`, and code that only checked `status` would go on to write to a
row it looked up by an untrusted caller-supplied id without ever
looking at the column plan section 12 says is the one that must never
be adoptable. Belt and braces, cheaply, on the one table where the
belt matters.

**Why `adopt`/`reject` do not call `get_card` to fetch the row.**
`get_card` treats "hidden" and "does not exist" identically, which is
exactly right for a user paging through cards -- plan section 9's /card
row says a hidden card "behaves as if it does not exist". But the
refusal *codes* this module returns have to tell FORBIDDEN (it exists,
and the answer is permanently no) apart from GONE (there is nothing
here, or it was already decided the other way), so app/tg/research.py
can log the difference even while showing the user the same "no such
card" reply for both -- see that module's docstring. So both functions
read the row directly with `session.get` and apply the status/risk
checks themselves.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.memory import near_duplicate, write_memory
from app.core.state import record_change
from app.db.models import StudyCard

PAGE_SIZE = 5

ADOPTED = "adopted"
REJECTED = "rejected"
ALREADY = "already"
GONE = "gone"
FORBIDDEN = "forbidden"


async def pending_page(session: AsyncSession, *, page: int) -> tuple[list[StudyCard], int]:
    """One page of `status='pending'` cards, newest first, plus the total.

    Filtering on `status == 'pending'` is what keeps a hidden card off
    every page by construction -- there is no offset or filter flag
    that could ever surface one, unlike a "show hidden" toggle would
    invite. Newest first (plan section 9's /notes row), unlike
    app/tg/memory.py's oldest-first /memories: a freshly finished job's
    cards are what the user came to look at, not the oldest backlog.
    """
    total = await session.scalar(
        select(func.count()).select_from(StudyCard).where(StudyCard.status == "pending")
    )
    rows = await session.execute(
        select(StudyCard)
        .where(StudyCard.status == "pending")
        .order_by(StudyCard.created_at.desc(), StudyCard.id.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    return list(rows.scalars().all()), total


async def get_card(session: AsyncSession, card_id: int) -> StudyCard | None:
    """A card by id, any status except `'hidden'`, which is invisible here too."""
    card = await session.get(StudyCard, card_id)
    if card is None or card.status == "hidden":
        return None
    return card


async def _refuse(session: AsyncSession, card_id: int, *, already: str) -> tuple[StudyCard | None, str | None]:
    """Shared precondition for adopt/reject.

    Returns `(card, None)` when the card is `'pending'` and the caller
    should proceed, or `(None, code)` when the call is already decided.
    `already` is the status this particular action is idempotent on
    (`'adopted'` for adopt, `'rejected'` for reject) -- landing on it a
    second time is `ALREADY`, not an error, and writes nothing; landing
    on the *other* terminal status (or `'expired'`) is `GONE`, because
    that decision cannot be reversed by this module.
    """
    card = await session.get(StudyCard, card_id)
    if card is None:
        return None, GONE
    if card.status == "hidden" or card.risk_final == "high":
        return None, FORBIDDEN
    if card.status == already:
        return None, ALREADY
    if card.status != "pending":
        return None, GONE
    return card, None


async def adopt(session: AsyncSession, card_id: int, *, clock: Clock) -> str:
    """Adopt a pending card: one `technique` memory, and nothing else.

    Idempotent per `_refuse`'s rule: adopting an already-adopted card
    returns `ALREADY` and writes nothing.

    **`write_memory` can return `None`** (a near-duplicate active
    memory already says this). That is not refused here -- a technique
    a page repeats, or a second `/read` of a similar page, is exactly
    the ordinary case dedupe exists for, not a reason to block the
    user's decision -- so the card is linked to the existing memory via
    `app.core.memory.near_duplicate` instead of being left without one, which
    `ck_study_card_adopted_has_memory` would reject outright. This also
    makes a *replayed* adopt (the worker re-running a stuck or crashed
    job) land correctly the second time: the first run's memory is
    still active, `write_memory` calls it a duplicate of itself, and
    the card is linked to that same row rather than a fresh copy.
    """
    card, refusal = await _refuse(session, card_id, already="adopted")
    if card is None:
        return refusal

    written = await write_memory(session, kind="technique", text=card.text, source="adopt")
    if written is None:
        # write_memory reports only *that* an active memory already says
        # this, never which row. app.core.memory.near_duplicate is the
        # same function write_memory itself asked, so the two cannot
        # disagree about what counts as the same fact.
        written = await near_duplicate(session, card.text)
    if written is None:
        # Unreachable with one worker: write_memory just saw the row.
        # Raised rather than asserted, because `python -O` strips an
        # assert and this must not become a constraint violation three
        # statements later.
        raise RuntimeError("write_memory reported a duplicate that is now missing")

    now = clock.now_utc()
    card.memory_id = written.id
    card.status = "adopted"
    card.decided_at = now
    # Both columns are set on the same ORM object before any commit, so
    # they reach the database in the same flush -- ck_study_card_adopted_
    # has_memory can never observe one without the other, even under a
    # crash between them (there is no "between" to crash in).
    await record_change(
        session, field="study_card", old_value=str(card_id), new_value=ADOPTED, source="command"
    )
    return ADOPTED


async def reject(session: AsyncSession, card_id: int, *, clock: Clock) -> str:
    """Reject a pending card. Idempotent: rejecting twice returns `ALREADY` the second time."""
    card, refusal = await _refuse(session, card_id, already="rejected")
    if card is None:
        return refusal

    card.status = "rejected"
    card.decided_at = clock.now_utc()
    await record_change(
        session, field="study_card", old_value=str(card_id), new_value=REJECTED, source="command"
    )
    return REJECTED
