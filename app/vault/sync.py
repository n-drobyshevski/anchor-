"""The sync pass (phase-8 plan section 7), job `vault_sync`, in `mirror`.

8b implements the database -> vault direction. Each pass:

0. prunes `vault_sync` jobs done more than an hour ago (the per-minute
   dedup key would otherwise grow the job table by ~1,440 rows a day),
   and does nothing at all while a `vault_purge` is pending (plan 10);
1. asks vaultd for its status, and records it -- an unreachable vault
   is recorded and the job completes normally: no retry storm, the
   next minute tries again;
2. reads the manifest;
3. deletes **epoch orphans**: Anchor-scope files with no row whose
   `anchor_epoch` differs from the current one. They are leftovers from
   before a /delete, re-uploaded by an offline device, and are never
   imported;
4. **records** edits made in the vault (mirror's ingest): a tracked
   fact file whose hash moved gets its new hash stored, and nothing
   else happens. The next database-side change to that fact overwrites
   the edit, which /vault says in as many words;
5. renders facts, then 6. the journal.

**Every write is compare-and-swap.** A create is create-only; an update
names the hash Anchor last saw; a delete names it too. A 412 means the
user touched the file after this pass's manifest, and the file is simply
skipped -- the next pass records their hash first. This is what "Anchor
never overwrites or deletes a file whose current content it has not
ingested" means in practice.

**Each file is its own transaction**, and a new fact's row is committed
*before* its create-only PUT: a crash between the two leaves a row to
reconcile (the next pass finds the file and adopts its hash), never an
untracked file.

**The write cap** (`VAULT_MAX_WRITES_PER_PASS`) counts every PUT and
DELETE. Bootstrap and backfill are therefore paced, not bursted: 400
facts become files over eight minutes, not in one burst Sync would
upload all at once.

**What 8b does not do** (8c): apply an edit, create a fact from a file,
resolve renames, forget a fact whose file vanished, or hold anything.
A file the user deleted is not recreated -- its CAS update fails and is
skipped. `sync` mode behaves exactly like `mirror` until then.

Logs carry counts, ids and codes. Never a path, a name or a text.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.checkin import DUE_LABELS_TEXT
from app.core.clock import Clock
from app.db.jobs import enqueue_job
from app.db.models import (
    Checkin,
    Job,
    Journal,
    Memory,
    Message,
    StudyCard,
    StudyClip,
    UserState,
    VaultFile,
)
from app.vault import errors, frontmatter, render
from app.vault.client import ManifestEntry, VaultClient
from app.vault.errors import VaultError
from app.vault.kinds import SYNC_MODES, VAULT_SYNC, sync_dedup_key
from app.vault.status import ClientFactory, purge_pending, record_status

logger = logging.getLogger(__name__)

PRUNE_AFTER = datetime.timedelta(hours=1)

# Codes a quarantined row can carry in 8b (ck_vault_file_reason_code).
NAME_TAKEN = "name_taken"
BAD_YAML = "bad_yaml"


@dataclass
class PassResult:
    ran: bool = False
    unavailable: bool = False
    created: int = 0
    updated: int = 0
    deleted: int = 0
    orphans: int = 0
    recorded: int = 0
    skipped: int = 0
    quarantined: int = 0
    counts: dict = field(default_factory=dict)

    @property
    def writes(self) -> int:
        return self.created + self.updated + self.deleted + self.orphans


class _Budget:
    def __init__(self, limit: int) -> None:
        self.left = max(0, limit)

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


async def maybe_enqueue_vault_sync(session: AsyncSession, settings: Settings, clock: Clock) -> bool:
    """Queue this minute's pass, in mirror and sync only (plan section 7)."""
    if settings.VAULT_MODE not in SYNC_MODES:
        return False
    return await enqueue_job(session, VAULT_SYNC, {}, dedup_key=sync_dedup_key(clock.now_utc()))


async def prune_done_passes(session: AsyncSession, clock: Clock) -> int:
    cutoff = clock.now_utc() - PRUNE_AFTER
    result = await session.execute(
        delete(Job)
        .where(Job.kind == VAULT_SYNC, Job.status == "done", Job.created_at < cutoff)
        .returning(Job.id)
    )
    pruned = len(result.all())
    await session.commit()
    return pruned


async def run_vault_purge(
    settings: Settings, client_factory: ClientFactory = VaultClient.from_settings
) -> bool:
    """POST /v1/purge. True on success; False means "defer and try again".

    **Any** failure defers (plan section 10): unreachable, 401, 5xx.
    "/delete must really delete", so the job never gives up and never
    burns an attempt -- the worker turns False into a Deferred, which
    keeps attempts at 0.
    """
    try:
        deleted = await client_factory(settings).purge()
    except VaultError as exc:
        logger.warning("vault purge deferred", extra={"error_code": exc.code})
        return False
    logger.info("vault purged", extra={"count": deleted})
    return True


async def run_vault_sync(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    client_factory: ClientFactory = VaultClient.from_settings,
) -> PassResult:
    result = PassResult()
    await prune_done_passes(session, clock)
    if settings.VAULT_MODE not in SYNC_MODES:
        return result
    if await purge_pending(session):
        logger.info("vault sync skipped", extra={"event": "purge_pending"})
        return result

    client = client_factory(settings)
    now = clock.now_utc()
    try:
        service = await client.status()
    except VaultError as exc:
        await record_status(session, last_unavailable_at=now)
        logger.warning("vault unavailable", extra={"error_code": exc.code})
        result.unavailable = True
        return result
    await record_status(session, last_ok_at=now, ob_running_since=service.running_since)

    result.ran = True
    try:
        manifest = {entry.path: entry for entry in await client.manifest()}
        state = (await session.execute(select(UserState))).scalar_one()
        budget = _Budget(settings.VAULT_MAX_WRITES_PER_PASS)
        await _delete_orphans(session, client, manifest, state.vault_epoch, budget, result)
        await _record_edits(session, manifest, clock, result)
        await _render_facts(session, client, manifest, state, clock, budget, result)
        await _render_journal(session, client, manifest, state, clock, budget, result)
    except VaultError as exc:
        await session.rollback()
        if exc.code == errors.UNAVAILABLE:
            await record_status(session, last_unavailable_at=clock.now_utc())
            result.unavailable = True
        logger.warning("vault sync stopped", extra={"error_code": exc.code})
    logger.info(
        "vault sync pass",
        extra={
            "count": result.writes,
            "event": (
                f"created={result.created} updated={result.updated} deleted={result.deleted} "
                f"orphans={result.orphans} recorded={result.recorded} skipped={result.skipped} "
                f"quarantined={result.quarantined}"
            ),
        },
    )
    return result


# --- step 3: epoch orphans -------------------------------------------------


async def _delete_orphans(
    session: AsyncSession,
    client: VaultClient,
    manifest: dict[str, ManifestEntry],
    epoch: str,
    budget: _Budget,
    result: PassResult,
) -> None:
    tracked = set((await session.execute(select(VaultFile.path))).scalars())
    for path, entry in sorted(manifest.items()):
        if entry.scope != "anchor" or path in tracked:
            continue
        try:
            current = await client.get_file(path)
        except VaultError as exc:
            if exc.code in (errors.NOT_FOUND, errors.REFUSED):
                continue
            raise
        meta = frontmatter.load(current.content)
        file_epoch = meta.get("anchor_epoch") if meta else None
        if not isinstance(file_epoch, str) or file_epoch == epoch:
            continue
        if not budget.take():
            return
        try:
            await client.delete_file(path, current.sha256)
        except VaultError as exc:
            if exc.code in (errors.CONFLICT, errors.NOT_FOUND):
                result.skipped += 1
                continue
            raise
        result.orphans += 1
        manifest.pop(path, None)


# --- step 4: mirror's ingest -------------------------------------------------


async def _record_edits(
    session: AsyncSession, manifest: dict[str, ManifestEntry], clock: Clock, result: PassResult
) -> None:
    rows = (await session.execute(select(VaultFile).where(VaultFile.role == "fact"))).scalars().all()
    for row in rows:
        entry = manifest.get(row.path)
        if entry is None or entry.sha256 == row.disk_sha256 or row.state == "held":
            continue
        row.disk_sha256 = entry.sha256
        row.updated_at = clock.now_utc()
        if row.state == "quarantined" and row.reason == BAD_YAML:
            # The user changed the file; try reading its properties again.
            row.state, row.reason, row.render_digest = "ok", None, None
        await session.commit()
        result.recorded += 1


# --- step 5: facts ---------------------------------------------------------


@dataclass(frozen=True)
class _MemoryRow:
    id: int
    kind: str
    text: str
    pinned: bool
    source: str
    created_at: datetime.datetime
    superseded_by: int | None


async def _fact_views(session: AsyncSession, timezone: str) -> dict[int, render.FactView]:
    """A FactView per active memory, built from two queries for the whole table."""
    rows = [
        _MemoryRow(*r)
        for r in await session.execute(
            select(
                Memory.id,
                Memory.kind,
                Memory.text,
                Memory.pinned,
                Memory.source,
                Memory.created_at,
                Memory.superseded_by,
            )
        )
    ]
    predecessor_of: dict[int, _MemoryRow] = {}
    for row in sorted(rows, key=lambda r: r.id):
        if row.superseded_by is not None:
            predecessor_of.setdefault(row.superseded_by, row)

    # memory id -> (card id, domain, quote) of the lowest-id card on it.
    cards: dict[int, tuple[int, str, str]] = {}
    for card_id, memory_id, quote, domain in await session.execute(
        select(StudyCard.id, StudyCard.memory_id, StudyCard.quote, StudyClip.domain)
        .join(StudyClip, StudyClip.id == StudyCard.clip_id)
        .where(StudyCard.memory_id.is_not(None))
        .order_by(StudyCard.id)
    ):
        cards.setdefault(memory_id, (card_id, domain, quote))

    def local(moment: datetime.datetime) -> datetime.date:
        return clock_module.local_date_of(moment, timezone)

    views: dict[int, render.FactView] = {}
    for head in rows:
        if head.superseded_by is not None:
            continue
        history: list[tuple[datetime.date, str]] = []
        lineage_ids = [head.id]
        seen = {head.id}
        cursor = predecessor_of.get(head.id)
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            lineage_ids.append(cursor.id)
            history.append((local(cursor.created_at), cursor.text))
            cursor = predecessor_of.get(cursor.id)
        source = None
        if head.kind == "technique":
            # The card still points at the row originally adopted, which
            # may be any id in the lineage (plan 4.1). Lowest card id wins,
            # so the choice is deterministic.
            candidates = sorted(cards[i] for i in lineage_ids if i in cards)
            if candidates:
                _card_id, domain, quote = candidates[0]
                source = (domain, quote)
        views[head.id] = render.FactView(
            memory_id=head.id,
            kind=head.kind,
            text=head.text,
            pinned=head.pinned,
            source=head.source,
            created=local(head.created_at),
            history=tuple(history),
            technique_source=source,
        )
    return views


async def _render_facts(
    session: AsyncSession,
    client: VaultClient,
    manifest: dict[str, ManifestEntry],
    state: UserState,
    clock: Clock,
    budget: _Budget,
    result: PassResult,
) -> None:
    epoch = state.vault_epoch
    rows = (
        (await session.execute(select(VaultFile).where(VaultFile.role == "fact").order_by(VaultFile.id)))
        .scalars()
        .all()
    )
    await _follow_heads(session, rows)

    # Forgotten facts: the memory is gone (on delete set null), so is the file.
    for row in rows:
        if row.memory_id is not None or row.state == "held":
            continue
        entry = manifest.get(row.path)
        if entry is not None:
            if not budget.take():
                return
            try:
                await client.delete_file(row.path, row.disk_sha256 or entry.sha256)
            except VaultError as exc:
                if exc.code == errors.CONFLICT:
                    result.skipped += 1
                    continue
                if exc.code != errors.NOT_FOUND:
                    raise
            result.deleted += 1
            # This pass's snapshot must not still show a file it deleted:
            # a new fact rendered to the same path later in the pass
            # would read as "someone else's file" and wait a minute.
            manifest.pop(row.path, None)
        await session.delete(row)
        await session.commit()

    views = await _fact_views(session, state.timezone)
    by_memory = {row.memory_id: row for row in rows if row.memory_id is not None}
    for memory_id in sorted(views):
        view = views[memory_id]
        row = by_memory.get(memory_id)
        if row is None:
            if budget.left <= 0:
                return
            path = render.fact_path(memory_id, epoch)
            taken = (
                await session.execute(select(VaultFile.id).where(VaultFile.path == path))
            ).first()
            if taken is not None:
                continue
            row = VaultFile(path=path, role="fact", memory_id=memory_id)
            session.add(row)
            # Committed before the PUT (plan 7.3): a crash between the two
            # leaves a row to reconcile, never an untracked file.
            await session.commit()
        if row.state != "ok":
            continue
        await _write_fact(session, client, manifest, row, view, epoch, clock, budget, result)
        if budget.left <= 0:
            return



async def _follow_heads(session: AsyncSession, rows: list[VaultFile]) -> None:
    """Keep every fact file on the head of its lineage, whoever superseded it.

    `write_memory` moves `vault_file.memory_id` to the new head in the
    same transaction. Phase 6's idle consolidation does not go through
    it: it inserts the merged fact and sets `superseded_by` on the
    originals directly (app/core/idle/consolidate.py), and can merge two
    originals into one. So before rendering, a row pointing at a
    superseded memory follows the chain to its head. The oldest row to
    reach a head keeps it; any later row that lands on the same head is
    a duplicate, and is treated as forgotten -- its file is deleted by
    compare-and-swap on this pass, exactly as a /forget would be.
    """
    successor = dict(
        (await session.execute(select(Memory.id, Memory.superseded_by))).all()
    )
    claimed = {row.memory_id for row in rows if row.memory_id is not None and successor.get(row.memory_id) is None}
    changed = False
    for row in rows:
        if row.memory_id is None or successor.get(row.memory_id) is None:
            continue
        head, seen = row.memory_id, set()
        while successor.get(head) is not None and head not in seen:
            seen.add(head)
            head = successor[head]
        if head in claimed:
            row.memory_id = None
        else:
            row.memory_id = head
            claimed.add(head)
        changed = True
    if changed:
        await session.commit()

async def _write_fact(
    session: AsyncSession,
    client: VaultClient,
    manifest: dict[str, ManifestEntry],
    row: VaultFile,
    view: render.FactView,
    epoch: str,
    clock: Clock,
    budget: _Budget,
    result: PassResult,
) -> None:
    plain = render.render_fact(view, epoch)
    if row.render_digest == plain.digest:
        return
    entry = manifest.get(row.path)
    if row.disk_sha256 is None and entry is None:
        # A new file: create-only.
        if not budget.take():
            return
        try:
            new_sha = await client.put_file(row.path, plain.content, None)
        except VaultError as exc:
            if exc.code != errors.CONFLICT:
                raise
            row.state, row.reason = "quarantined", NAME_TAKEN
            await session.commit()
            result.quarantined += 1
            return
        _written(row, new_sha, plain.digest, clock)
        await session.commit()
        result.created += 1
        return
    if entry is None or entry.sha256 != row.disk_sha256:
        # Gone from the vault, or changed since we last looked: not ours
        # to touch in mirror. The next pass records the new hash first.
        result.skipped += 1
        return
    current = await client.get_file(row.path)
    if current.sha256 != row.disk_sha256:
        result.skipped += 1
        return
    extras = frontmatter.extra_segments(current.content, render.FACT_KEYS)
    if extras is None:
        # Properties we cannot read are properties we would drop.
        row.state, row.reason = "quarantined", BAD_YAML
        await session.commit()
        result.quarantined += 1
        return
    rendered = render.render_fact(view, epoch, extras)
    if rendered.sha256 == row.disk_sha256:
        # Already on disk (e.g. a crash after the PUT): adopt it.
        row.render_digest = rendered.digest
        await session.commit()
        return
    if not budget.take():
        return
    try:
        new_sha = await client.put_file(row.path, rendered.content, row.disk_sha256)
    except VaultError as exc:
        if exc.code != errors.CONFLICT:
            raise
        result.skipped += 1
        return
    _written(row, new_sha, rendered.digest, clock)
    await session.commit()
    result.updated += 1


def _written(row: VaultFile, sha: str, digest: str, clock: Clock) -> None:
    row.disk_sha256 = sha
    row.render_digest = digest
    row.updated_at = clock.now_utc()


# --- step 6: journal -------------------------------------------------------


async def _welfare_dates(session: AsyncSession, timezone: str) -> set[datetime.date]:
    moments = (
        await session.execute(select(Message.created_at).where(Message.kind == "welfare"))
    ).scalars()
    return {clock_module.local_date_of(moment, timezone) for moment in moments}


async def _journal_view(
    session: AsyncSession, day: datetime.date, welfare_dates: set[datetime.date]
) -> render.JournalView:
    entries = tuple(
        (
            await session.execute(
                select(Journal.text).where(Journal.local_date == day).order_by(Journal.id)
            )
        ).scalars()
    )
    checkin = (
        await session.execute(select(Checkin).where(Checkin.local_date == day))
    ).scalar_one_or_none()
    if checkin is None:
        return render.JournalView(local_date=day, entries=entries)
    # A check-in note that tripped the welfare check keeps its text in
    # `checkin.note` (the message is retagged, the note is not), and
    # nothing from a welfare exchange may be rendered (plan section 10).
    # Fail closed: on a day with any welfare message -- or the day
    # after, for a check-in answered past midnight -- the note stays out.
    note_allowed = day not in welfare_dates and (day + datetime.timedelta(days=1)) not in welfare_dates
    return render.JournalView(
        local_date=day,
        day_rating=checkin.day_rating,
        due_label=DUE_LABELS_TEXT.get(checkin.due_result) if checkin.due_result else None,
        note=checkin.note if note_allowed else None,
        entries=entries,
    )


async def _render_journal(
    session: AsyncSession,
    client: VaultClient,
    manifest: dict[str, ManifestEntry],
    state: UserState,
    clock: Clock,
    budget: _Budget,
    result: PassResult,
) -> None:
    epoch, timezone = state.vault_epoch, state.timezone
    rows = {
        row.local_date: row
        for row in (
            await session.execute(select(VaultFile).where(VaultFile.role == "journal"))
        ).scalars()
    }
    today = clock_module.local_date(clock, timezone)
    with_content = set(
        (await session.execute(select(Journal.local_date).distinct())).scalars()
    ) | set((await session.execute(select(Checkin.local_date))).scalars())
    days = {today, today - datetime.timedelta(days=1)} | (with_content - set(rows))
    welfare_dates = await _welfare_dates(session, timezone)

    for day in sorted(days):
        row = rows.get(day)
        if row is not None and row.state != "ok":
            continue
        entry = manifest.get(row.path) if row is not None else None
        if row is not None and entry is not None:
            if row.disk_sha256 is None:
                # Created by a pass that crashed before recording it.
                row.disk_sha256 = entry.sha256
                row.render_digest = None
                await session.commit()
            elif entry.sha256 != row.disk_sha256:
                # Edited by hand: Anchor never touches it again (plan 7.4).
                row.state = "diverged"
                row.updated_at = clock.now_utc()
                await session.commit()
                continue
        view = await _journal_view(session, day, welfare_dates)
        if view.is_empty:
            continue
        rendered = render.render_journal(view, epoch)
        if row is not None and row.render_digest == rendered.digest:
            continue
        if budget.left <= 0:
            return
        if row is None:
            row = VaultFile(path=render.journal_path(day, epoch), role="journal", local_date=day)
            session.add(row)
            await session.commit()
        if row.disk_sha256 is None:
            budget.take()
            try:
                new_sha = await client.put_file(row.path, rendered.content, None)
            except VaultError as exc:
                if exc.code != errors.CONFLICT:
                    raise
                row.state, row.reason = "quarantined", NAME_TAKEN
                await session.commit()
                result.quarantined += 1
                continue
            _written(row, new_sha, rendered.digest, clock)
            await session.commit()
            result.created += 1
            continue
        if entry is None:
            # Deleted by the user. Dismissing it is 8c's; mirror leaves it.
            result.skipped += 1
            continue
        budget.take()
        try:
            new_sha = await client.put_file(row.path, rendered.content, row.disk_sha256)
        except VaultError as exc:
            if exc.code != errors.CONFLICT:
                raise
            result.skipped += 1
            continue
        _written(row, new_sha, rendered.digest, clock)
        await session.commit()
        result.updated += 1
