"""Echo's writes into the user's vault: adopting lens research, and `/lens undo` (lens L4).

anchor-lens-plan.md sections 9, 13 and 14.5, the L4 spec section 5. The
only module that writes or undoes anything in the vault on Echo's own
behalf (a test pins it): «в Inbox» on a research's result message
becomes one knowledge note in vaultd's `echo_inbox`, and `/lens undo`
takes the newest one back. It never writes a memory: a lens card is
material to read, not something Echo knows about the user.

**`adopt_research`**, one tap:

1. `lens.research_target` checks the tap -- the vault epoch, a gap still
   `researched`, and the result message that carries it -- and gives the
   gap's kind, proposed title and its lens notes' current titles.
2. An `echo_changeset` row not yet confirmed for this gap (an earlier
   tap whose answer from vaultd was lost) is replayed with the same
   `vault_ref`, before anything else: vaultd answers `replayed` if it
   had written the note, and writes it now if not. The note is rendered
   from the row's own `card_ids`, whatever their status now (the sweep
   may have expired them meanwhile). Never two notes for one tap.
3. Otherwise app/core/echo_note.py renders the note from the gap's
   visible pending cards, `note_checks.check_content` screens it (an
   instruction or a secret refuses it), and the row -- ids and times
   only -- is inserted and **committed before the write**, so a crash
   after it can only lead to a replay.
4. `put_echo_note`. On success one commit confirms the row, adopts the
   cards (they point at the row, never at a memory) and moves the gap
   to `done`, which the next garden run rechecks. A refusal (403 and
   the like) deletes the row: nothing was written. A transport failure
   keeps it, and the next tap replays it.

**`settle_open` / `settle_orphans`**: an unconfirmed row that no tap
will replay -- «не нужно» on its result message, its cards all expired,
its gap resolved by a recheck or gone -- is settled by asking vaultd,
never by writing: `GET /v1/changes` lists the row's `vault_ref` if the
note was written (the row is confirmed, so `/lens undo` can take it
back), and does not if it was not (the row is deleted). A vault that
does not answer leaves the row for the next try. So a row is never
dropped while its note may exist.

**`undo_last`**: the newest confirmed, not yet undone write under
`UNDO_WINDOW_DAYS` old, undone through vaultd's writer-scoped undo
(`writer="echo"`: it can never take back a Claude write). The outcome is
one of four the reply names -- undone, nothing to undo, expired (vaultd
no longer has it) or changed (the note was edited since, and
compare-and-swap refused) -- or a refusal/unavailable vault. An undo
whose answer was lost is not "changed" on the retry: vaultd's index
marks the changeset undone, and the row is marked too.

All commit. Logs carry the row's id and an outcome code, never the
gap's id (with it, the logs would tell a researched gap from a resolved
one, the L4 spec section 6), the note's name, a card's text, a quote, a
URL or a path.
"""

from __future__ import annotations

import datetime
import logging
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import echo_note, note_checks
from app.core.clock import Clock
from app.db.models import EchoChangeset
from app.research import jobs
from app.vault import errors, lens
from app.vault.client import VaultClient
from app.vault.errors import VaultError

logger = logging.getLogger(__name__)

# adopt_research's outcomes.
OK = "ok"
STALE = "stale"
EMPTY = "empty"
REFUSED = "refused"
UNAVAILABLE = "unavailable"
# undo_last's outcomes (and REFUSED, UNAVAILABLE).
UNDONE = "undone"
NOTHING = "nothing"
EXPIRED = "expired"
CHANGED = "changed"

# vaultd keeps pre-images this long (vaultd/vaultd/config.py's
# UNDO_TTL_DAYS); an older write cannot be undone.
UNDO_WINDOW_DAYS = 14
# vaultd's changeset ids are `[A-Za-z0-9_-]{1,64}`; Echo's say whose.
VAULT_REF_PREFIX = "echo_"

# A definite "no" from vaultd: nothing was written, the row can go.
_DEFINITE_REFUSALS = (errors.REFUSED, errors.NOT_FOUND, errors.CONFLICT)


def _new_vault_ref() -> str:
    return VAULT_REF_PREFIX + secrets.token_hex(12)


def _log(event: str, *, changeset_id: int | None = None) -> None:
    logger.info("echo inbox", extra={"event": event, "echo_changeset_id": changeset_id})


async def open_row(session: AsyncSession, gap_id: int) -> EchoChangeset | None:
    """The gap's unconfirmed write, if any (at most one per gap)."""
    return (
        await session.execute(
            select(EchoChangeset).where(
                EchoChangeset.lens_gap_id == gap_id, EchoChangeset.confirmed_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def adopt_research(
    session: AsyncSession,
    client: VaultClient,
    clock: Clock,
    *,
    gap_id: int,
    epoch: str,
    message_id: int,
) -> str:
    """«в Inbox» (module docstring). Returns `OK`, `STALE` (the tap no
    longer applies), `EMPTY` (no visible pending card is left to write),
    `REFUSED` (vaultd, or the content check, said no: nothing was
    written) or `UNAVAILABLE` (the vault did not answer; the next tap
    replays). Commits."""
    target = await lens.research_target(session, gap_id, epoch, message_id=message_id)
    if target is None:
        return STALE
    name = echo_note.note_name(
        gap_id=gap_id, kind=target.kind, title=target.title, titles=target.titles
    )
    row = await open_row(session, gap_id)
    if row is not None:
        # The replay comes first, from the row's own cards, whatever
        # their status: vaultd may already hold the note.
        cards = await jobs.lens_cards_by_id(session, row.card_ids or ())
        content = echo_note.render(gap_id=gap_id, titles=target.titles, cards=cards)
        return await _put(session, client, clock, row, name, content)

    now = clock.now_utc()
    views = (await jobs.lens_card_views(session, [gap_id])).get(gap_id)
    cards = list(views.cards) if views is not None else []
    if not cards:
        _log(EMPTY)
        return EMPTY
    content = echo_note.render(gap_id=gap_id, titles=target.titles, cards=cards)
    try:
        note_checks.check_content(content)
    except note_checks.Refused:
        _log("content_refused")
        return REFUSED
    row = EchoChangeset(
        vault_ref=_new_vault_ref(),
        lens_gap_id=gap_id,
        study_job_id=views.job_id if views is not None else None,
        card_ids=[card.id for card in cards],
        created_at=now,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # Another tap on the same gap got its row in first.
        await session.rollback()
        return STALE
    return await _put(session, client, clock, row, name, content)


async def _put(
    session: AsyncSession,
    client: VaultClient,
    clock: Clock,
    row: EchoChangeset,
    name: str,
    content: str,
) -> str:
    """`put_echo_note` under the row's `vault_ref` (a first write or its
    replay), then confirm the row -- or delete it on a definite refusal,
    or keep it for a replay when the vault did not answer."""
    row_id = row.id
    try:
        await client.put_echo_note(name, content, row.vault_ref)
    except VaultError as exc:
        if exc.code in _DEFINITE_REFUSALS:
            await session.delete(row)
            await session.commit()
            _log(REFUSED, changeset_id=row_id)
            return REFUSED
        _log(UNAVAILABLE, changeset_id=row_id)
        return UNAVAILABLE
    await _confirm(session, row, clock.now_utc())
    await session.commit()
    _log(OK, changeset_id=row_id)
    return OK


async def _confirm(session: AsyncSession, row: EchoChangeset, now: datetime.datetime) -> None:
    """vaultd holds the row's note: the row is confirmed, the cards it
    holds that are still pending are adopted, and the gap, if still
    `researched`, moves to `done`. Flushes, never commits."""
    row.confirmed_at = now
    await jobs.adopt_lens_cards(session, list(row.card_ids or ()), row.id, now)
    if row.lens_gap_id is not None:
        await lens.mark_research_adopted(session, row.lens_gap_id, now)
    await session.flush()


async def settle_open(
    session: AsyncSession, client: VaultClient, clock: Clock, row: EchoChangeset
) -> str:
    """Settle an unconfirmed row no tap will replay (module docstring)
    by asking vaultd whether it holds the row's changeset, never by
    writing: `OK` when it does (the row is confirmed, its pending cards
    adopted, a still-researched gap moved to `done`), `NOTHING` when it
    does not (the row is deleted: nothing was written), `UNAVAILABLE`
    when the vault did not answer (the row stays). Commits."""
    row_id = row.id
    try:
        changes = await client.list_changes()
    except VaultError:
        _log("settle_" + UNAVAILABLE, changeset_id=row_id)
        return UNAVAILABLE
    if any(c.id == row.vault_ref and c.kind == "write" and c.writer == "echo" for c in changes):
        await _confirm(session, row, clock.now_utc())
        await session.commit()
        _log("settle_" + OK, changeset_id=row_id)
        return OK
    await session.delete(row)
    await session.commit()
    _log("settle_" + NOTHING, changeset_id=row_id)
    return NOTHING


async def orphan_rows(session: AsyncSession) -> list[EchoChangeset]:
    """Unconfirmed rows whose gap is gone or no longer `researched`
    (resolved by a recheck, or reopened): no tap reaches them any more."""
    rows = list(
        (
            await session.execute(
                select(EchoChangeset)
                .where(EchoChangeset.confirmed_at.is_(None))
                .order_by(EchoChangeset.id)
            )
        ).scalars().all()
    )
    gaps = await lens.research_gaps(
        session, [row.lens_gap_id for row in rows if row.lens_gap_id is not None]
    )
    return [
        row
        for row in rows
        if row.lens_gap_id not in gaps or gaps[row.lens_gap_id].status != "researched"
    ]


async def undo_last(session: AsyncSession, client: VaultClient, clock: Clock) -> str:
    """`/lens undo` (module docstring). Returns `UNDONE`, `NOTHING`,
    `EXPIRED`, `CHANGED`, `REFUSED` (vaultd refused, e.g. Echo's hourly
    undo cap) or `UNAVAILABLE`. Commits."""
    now = clock.now_utc()
    cutoff = now - datetime.timedelta(days=UNDO_WINDOW_DAYS)
    row = (
        await session.execute(
            select(EchoChangeset)
            .where(
                EchoChangeset.confirmed_at.is_not(None),
                EchoChangeset.undone_at.is_(None),
                EchoChangeset.confirmed_at >= cutoff,
            )
            .order_by(EchoChangeset.confirmed_at.desc(), EchoChangeset.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return NOTHING
    try:
        result = await client.undo_changeset(row.vault_ref, writer="echo")
    except VaultError as exc:
        if exc.code == errors.NOT_FOUND:
            outcome = EXPIRED
        elif exc.code == errors.REFUSED:
            outcome = REFUSED
        else:
            outcome = UNAVAILABLE
        _log(f"undo_{outcome}", changeset_id=row.id)
        return outcome
    if result.restored == 0 and not await _undone_in_vault(client, row.vault_ref):
        _log(f"undo_{CHANGED}", changeset_id=row.id)
        return CHANGED
    row.undone_at = now
    await session.commit()
    _log(f"undo_{UNDONE}", changeset_id=row.id)
    return UNDONE


async def _undone_in_vault(client: VaultClient, vault_ref: str) -> bool:
    """Whether vaultd's index already marks this changeset undone: an
    earlier `/lens undo` whose answer was lost took the note back, and a
    retry finds nothing left to restore. Without this the retry would
    say «меняли» and stick on the same row for the whole window."""
    try:
        changes = await client.list_changes()
    except VaultError:
        return False
    return any(c.id == vault_ref and c.writer == "echo" and c.undone for c in changes)
