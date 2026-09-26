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
5. renders facts, then 6. the journal;
7. **indexes knowledge notes** (phase-8 plan section 7 step 8, amended
   by the 8e plan's section 9 and this PR's own decision -- see
   `_index_notes`'s docstring for why personal notes are never
   touched here).

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
import re
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
from app.vault import deletions, errors, frontmatter, holds, ingest, notes_knowledge, notes_text, render
from app.vault.client import ManifestEntry, VaultClient
from app.vault.errors import VaultError
from app.vault.kinds import SYNC_MODES, VAULT_SYNC, sync_dedup_key
from app.vault.status import ClientFactory, purge_pending, record_status

FACT_PATH_RE = re.compile(r"^Anchor/Memory/[^/]+\.md$")

logger = logging.getLogger(__name__)

PRUNE_AFTER = datetime.timedelta(hours=1)

NOTES_MAX_PER_PASS = 50
"""Pacing for step 8 (notes index), independent of
`VAULT_MAX_WRITES_PER_PASS`: a note costs a vaultd GET plus a database
write, not a vaultd PUT, so it earns its own budget rather than
competing with facts and the journal for the same one. A constant, not
a Settings field -- the same call as `notes_text.NOTE_CHUNK_CHARS`: a
deploy must not be able to widen how much of a big vault is read in one
pass by pasting a bigger number into the environment."""

# Quarantine codes are app/vault/errors.py's alone (8c: QUARANTINE_CODES
# unifies what used to be a second copy here). Kept as local names for
# every call site below, which read better as bare NAME_TAKEN/BAD_YAML
# than errors.NAME_TAKEN/errors.BAD_YAML.
NAME_TAKEN = errors.NAME_TAKEN
BAD_YAML = errors.BAD_YAML


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
    # 8c: sync mode's ingest and deletions (mirror leaves all of these 0).
    created_facts: int = 0
    changed_facts: int = 0
    forgotten_facts: int = 0
    held: int = 0
    new_hold_ids: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    # Step 8 (notes index): knowledge notes only (personal is out of
    # scope for this PR). Not sent anywhere yet.
    indexed: int = 0
    removed: int = 0

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
    sync_mode = settings.VAULT_MODE == "sync"
    try:
        manifest = {entry.path: entry for entry in (await client.manifest()).entries}
        state = (await session.execute(select(UserState))).scalar_one()
        budget = _Budget(settings.VAULT_MAX_WRITES_PER_PASS)

        # Step 2: snapshot `absent` *before* ingest, so a rename that
        # arrives in a single manifest is recognised (plan 7.1 case b).
        tracked = (
            (await session.execute(select(VaultFile).where(VaultFile.role.in_(("fact", "journal")))))
            .scalars()
            .all()
        )
        absent = [row for row in tracked if row.path not in manifest]

        await _delete_orphans(session, client, manifest, state.vault_epoch, budget, result)
        if sync_mode:
            claimed = await _ingest_facts(
                session, client, manifest, state, settings, clock, budget, result
            )
            absent = [row for row in absent if row.id not in claimed]
            await deletions.process_deletions(
                session,
                absent=absent,
                clock=clock,
                ob_running_since=service.running_since,
                result=result,
            )
        else:
            await _record_edits(session, manifest, clock, result)
        await _render_facts(session, client, manifest, state, clock, budget, result, sync_mode=sync_mode)
        await _render_journal(session, client, manifest, state, clock, budget, result)
        await _index_notes(session, client, manifest, state, settings, result)
        if sync_mode:
            expired = await holds.expire_holds(session, clock)
            result.new_hold_ids = [hid for hid in result.new_hold_ids if hid not in expired]
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
                f"quarantined={result.quarantined} created_facts={result.created_facts} "
                f"changed_facts={result.changed_facts} forgotten_facts={result.forgotten_facts} "
                f"held={result.held} indexed={result.indexed} removed={result.removed}"
            ),
        },
    )
    return result


# --- step 4 (sync only): ingest -----------------------------------------


async def _ingest_facts(
    session: AsyncSession,
    client: VaultClient,
    manifest: dict[str, ManifestEntry],
    state: UserState,
    settings: Settings,
    clock: Clock,
    budget: _Budget,
    result: PassResult,
) -> set[int]:
    """Ingests every changed Anchor/Memory/ file. Returns the ids of rows
    a rename claimed this pass, so the caller excludes them from deletions."""
    tracked_by_path = {
        row.path: row
        for row in (
            await session.execute(select(VaultFile).where(VaultFile.role == "fact"))
        ).scalars()
    }
    absent_paths = {
        row.path
        for row in (
            await session.execute(
                select(VaultFile).where(VaultFile.role.in_(("fact", "journal")))
            )
        ).scalars()
        if row.path not in manifest
    }
    claimed: set[int] = set()
    for path, entry in sorted(manifest.items()):
        if entry.scope != "anchor" or not FACT_PATH_RE.match(path):
            continue
        row = tracked_by_path.get(path)
        if row is not None and row.state == "held":
            continue
        if row is not None and row.disk_sha256 == entry.sha256:
            continue
        try:
            current = await client.get_file(path)
        except VaultError as exc:
            if exc.code in (errors.NOT_FOUND, errors.REFUSED):
                continue
            raise
        if current.sha256 != entry.sha256:
            # Changed again since the manifest was read; next pass.
            continue
        outcome = await ingest.ingest_file(
            session,
            client,
            path=path,
            content=current.content,
            disk_sha256=current.sha256,
            epoch=state.vault_epoch,
            absent_paths=absent_paths,
            clock=clock,
            budget=budget,
            max_pinned=settings.MEMORY_PINNED_MAX,
            timezone=state.timezone,
        )
        if outcome.renamed_row_id is not None:
            claimed.add(outcome.renamed_row_id)
        if outcome.kind == "created":
            result.created_facts += 1
        elif outcome.kind == "changed":
            result.changed_facts += 1
        elif outcome.kind == "quarantined":
            result.quarantined += 1
        elif outcome.kind == "held":
            result.held += 1
            if outcome.hold_id is not None:
                result.new_hold_ids.append(outcome.hold_id)
        elif outcome.kind == "skipped":
            result.skipped += 1
    return claimed


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
    *,
    sync_mode: bool = False,
) -> None:
    epoch = state.vault_epoch
    callout = render.FACT_CALLOUT_SYNC if sync_mode else render.FACT_CALLOUT_MIRROR
    rows = (
        (await session.execute(select(VaultFile).where(VaultFile.role == "fact").order_by(VaultFile.id)))
        .scalars()
        .all()
    )
    await _follow_heads(session, rows)

    # Forgotten facts: the memory is gone (on delete set null), so is the
    # file. A quarantined row also has memory_id=None -- e.g. a
    # duplicate_file/duplicate_fact/technique that never got a memory of
    # its own -- and must not be swept up here: 8c's quarantined rows
    # are re-evaluated when their sha changes, not deleted.
    for row in rows:
        if row.memory_id is not None or row.state in ("held", "quarantined"):
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
        elif row.state == "restore":
            await _restore_fact(session, client, row, view, epoch, clock, budget, result, callout)
            if budget.left <= 0:
                return
            continue
        elif row.state == "quarantined" and row.reason == errors.PIN_CAP and row.render_digest is None:
            # 8c (plan 7.3): the one exception -- a pin_cap refusal
            # clears render_digest so the property is put back, even
            # though the row stays quarantined (for /vault to list).
            pass
        elif row.state != "ok":
            continue
        await _write_fact(session, client, manifest, row, view, epoch, clock, budget, result, callout)
        if budget.left <= 0:
            return


async def _restore_fact(
    session: AsyncSession,
    client: VaultClient,
    row: VaultFile,
    view: render.FactView,
    epoch: str,
    clock: Clock,
    budget: _Budget,
    result: PassResult,
    callout: str,
) -> None:
    """State `restore` (plan 7.3): a reverted mass-delete hold. The file
    is absent, so this is a create-only PUT, then the row goes back to
    `ok` -- exactly like a brand-new fact's first render."""
    if not budget.take():
        return
    rendered = render.render_fact(view, epoch, callout=callout)
    try:
        new_sha = await client.put_file(row.path, rendered.content, None)
    except VaultError as exc:
        if exc.code != errors.CONFLICT:
            raise
        result.skipped += 1
        return
    row.state, row.reason = "ok", None
    _written(row, new_sha, rendered.digest, clock)
    await session.commit()
    result.created += 1



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
    callout: str = render.FACT_CALLOUT_MIRROR,
) -> None:
    plain = render.render_fact(view, epoch, callout=callout)
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
    rendered = render.render_fact(view, epoch, extras, callout)
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


# --- step 8: notes index ----------------------------------------------------


def _note_title(path: str) -> str:
    """The file name without its directory or `.md` suffix (the plan's
    own words: "title = file name without .md")."""
    name = path.rsplit("/", 1)[-1]
    return name[: -len(".md")] if name.endswith(".md") else name


async def _remove_knowledge_note(session: AsyncSession, row: VaultFile) -> None:
    """Chunks first, row second -- the 8e plan's FK order (section 9):
    the composite foreign key refuses reclassifying a note while its
    old-class chunks exist, and refuses nothing about deleting the row
    outright, but doing it in this order keeps one rule for both
    "the note left" and "the note changed class"."""
    await notes_knowledge.delete_for_file(session, row.id)
    await session.delete(row)
    await session.commit()


async def _index_notes(
    session: AsyncSession,
    client: VaultClient,
    manifest: dict[str, ManifestEntry],
    state: UserState,
    settings: Settings,
    result: PassResult,
) -> None:
    """Step 8 (phase-8 plan section 7; indexing amended by the 8e plan's
    section 9). **Knowledge notes only.**

    This PR indexes `note_class == "knowledge"` and nothing else.
    Personal notes are read by nothing here -- `app/vault/notes_personal`
    is not even imported by this module (tests/test_vault_sync.py pins
    it, and tests/test_vault_notes_isolation.py's allowlist would let it
    happen if it were ever added). A `personal` manifest entry is looked
    at only to notice that it is *not* knowledge, so a note that changes
    class from knowledge to personal is removed, never re-filed under a
    row this PR does not create (docs/decisions.md: "index knowledge
    notes only").

    Runs in both `mirror` and `sync` -- the caller already returned
    before this point for `off` and `status` -- because nothing here
    writes to the vault. The mirror/sync split that matters for facts
    (record vs. apply an edit) has no equivalent for a read-only index.

    **Consent and the flag, checked here, not only by the caller
    (docs/decisions.md, "8e -- consent is checked inside the access
    modules", extended to this step and to the flag):**
    - consent off: `/vault notes off` already deleted every note row
      and both chunk tables' rows for it, synchronously
      (app/vault/consent.py). Nothing to do here.
    - consent on, `VAULT_KNOWLEDGE_ENABLED` off: a flag turned off must
      not leave a stale index, so every existing knowledge row (and its
      chunks) is removed, every pass, until the flag comes back on.
    - both on: removals, then indexing, each capped and each note its
      own transaction, so one bad note (invisible by the time it is
      fetched, or text that cannot survive a round trip) is skipped
      without failing the pass or any other note in it.
    """
    if not state.notes_consent:
        return
    tracked = (
        (
            await session.execute(
                select(VaultFile).where(VaultFile.role == "note", VaultFile.note_class == "knowledge")
            )
        )
        .scalars()
        .all()
    )
    if not settings.VAULT_KNOWLEDGE_ENABLED:
        for row in tracked:
            await _remove_knowledge_note(session, row)
            result.removed += 1
        return

    tracked_by_path = {row.path: row for row in tracked}
    for row in tracked:
        entry = manifest.get(row.path)
        if entry is None or entry.scope != "note" or entry.note_class != "knowledge":
            # Gone from the manifest, or reclassified away from
            # knowledge (including to `personal` -- this PR keeps no
            # row for that class at all).
            await _remove_knowledge_note(session, row)
            result.removed += 1
            del tracked_by_path[row.path]

    budget = _Budget(NOTES_MAX_PER_PASS)
    for path, entry in sorted(manifest.items()):
        if entry.scope != "note" or entry.note_class != "knowledge":
            continue
        row = tracked_by_path.get(path)
        if row is not None and row.disk_sha256 == entry.sha256:
            continue
        if not budget.take():
            # A big vault bootstraps over several passes.
            break
        try:
            current = await client.get_file(path)
            chunks = notes_text.prepare(current.content, _note_title(path))
        except (VaultError, UnicodeError):
            # vaultd 404 (the note became invisible between this pass's
            # manifest and this fetch) or text that cannot survive a
            # round trip: skip this note, never the pass.
            result.skipped += 1
            continue
        try:
            if row is None:
                row = VaultFile(path=path, role="note", note_class="knowledge")
                session.add(row)
                await session.flush()
                tracked_by_path[path] = row
            row.disk_sha256 = current.sha256
            await notes_knowledge.replace_chunks(session, row.id, chunks)
            await session.commit()
        except Exception:
            await session.rollback()
            result.skipped += 1
            continue
        result.indexed += 1
