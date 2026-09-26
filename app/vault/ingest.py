"""Ingest: fact files -> database (phase-8 plan section 7.1).

Two layers. `parse_fact` is pure: it reads one file's content and
decides whether it is a fact file at all, and if so whether its
Anchor-owned properties are valid. `ingest_file` does the identity
resolution (plan section 7.1's table) and the three-way apply against
the database, calling `parse_fact` once it knows which row (if any)
this path already belongs to.

**Why identity resolution comes before full validation.** A duplicate
file (case c) or a rename (case b) is decided from the path and
`anchor_id` alone; whether the *content* is a valid fact does not
change which of those cases applies, and a corrupt duplicate is still
a duplicate. Type/kind/text validation only matters once a row -- new
or existing -- has been chosen to apply it to.

**Only `write_memory`, `set_pinned` and `forget_lineage` change
memory** (plan section 13); this module calls the first two, and
app/vault/deletions.py the third. Nothing here imports app.worker, an
LLM provider, or persona/outbound/proposal machinery
(tests/test_vault_isolation.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import memory
from app.core.clock import Clock
from app.core.redact import is_safe_to_store
from app.db.models import Memory as MemoryModel
from app.db.models import VaultFile
from app.research import injection
from app.vault import errors, frontmatter, holds, limits
from app.vault.client import VaultClient
from app.vault.errors import VaultError

logger = logging.getLogger(__name__)

FACT_KINDS = ("identity", "preference", "event", "rule", "technique")

# Deliberately not url/handle/code_fence (plan section 7.1): a GitHub
# handle or a fenced snippet in an ordinary fact is not an attack. Rules
# are exempt from role_reassign/speak_as_assistant only, since a rule
# legitimately says "veди себя как..." and every rule change already
# gets a Telegram confirmation (open_rule_hold).
_INSTRUCTION_IDS = frozenset(
    {"override_previous", "override_previous_en", "override_previous_fr", "system_prompt", "developer_mode", "role_tag", "exfiltrate", "role_reassign", "speak_as_assistant"}
)
_RULE_EXEMPT_IDS = frozenset({"role_reassign", "speak_as_assistant"})


def _collapse(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class ParsedFact:
    kind: str
    fact: str
    pinned: bool
    anchor_id: int | None
    anchor_epoch: str | None


@dataclass(frozen=True)
class Ignored:
    """Not a fact file (`anchor` is missing or is not `"fact"`)."""


@dataclass(frozen=True)
class Quarantined:
    code: str


def _typed_or_none(value, expected: type) -> tuple[bool, object]:
    """(ok, value). Rejects bool where an int is expected."""
    if expected is int:
        return (isinstance(value, int) and not isinstance(value, bool)), value
    return isinstance(value, expected), value


def parse_fact(content: str) -> ParsedFact | Ignored | Quarantined:
    """Parse one Anchor/Memory/ file per plan sections 4.4 and 7.1.

    Callers that already know a file's resolved identity (existing row
    vs. new) still call this for the type/kind/text checks; identity
    itself (rename, duplicate, forgotten) is resolved by `ingest_file`
    from the raw `anchor_id`, before this function's verdict on the
    content is known.
    """
    meta = frontmatter.load(content)
    if meta is None:
        return Quarantined(errors.BAD_YAML)
    if meta.get("anchor") != "fact":
        return Ignored()

    kind = meta.get("kind")
    if not isinstance(kind, str):
        return Quarantined(errors.BAD_TYPE)
    fact = meta.get("fact")
    if not isinstance(fact, str):
        return Quarantined(errors.BAD_TYPE)
    pinned = meta.get("pinned")
    if not isinstance(pinned, bool):
        return Quarantined(errors.BAD_TYPE)
    anchor_epoch = meta.get("anchor_epoch")
    if anchor_epoch is not None and not isinstance(anchor_epoch, str):
        return Quarantined(errors.BAD_TYPE)
    raw_anchor_id = meta.get("anchor_id")
    anchor_id: int | None
    if raw_anchor_id is None:
        anchor_id = None
    elif isinstance(raw_anchor_id, int) and not isinstance(raw_anchor_id, bool):
        anchor_id = raw_anchor_id
    else:
        return Quarantined(errors.BAD_TYPE)

    if kind not in FACT_KINDS:
        return Quarantined(errors.BAD_KIND)

    collapsed = _collapse(fact)
    if not collapsed:
        return Quarantined(errors.EMPTY)
    if len(collapsed) > limits.FACT_MAX_CHARS:
        return Quarantined(errors.TOO_LONG)
    if not is_safe_to_store(collapsed):
        return Quarantined(errors.UNSAFE)
    hits = set(injection.hits(collapsed))
    if kind == "rule":
        hits -= _RULE_EXEMPT_IDS
    if hits & _INSTRUCTION_IDS:
        return Quarantined(errors.INSTRUCTION)

    return ParsedFact(kind=kind, fact=collapsed, pinned=pinned, anchor_id=anchor_id, anchor_epoch=anchor_epoch)


@dataclass(frozen=True)
class IngestOutcome:
    kind: str  # ignored|quarantined|created|changed|cosmetic|held|skipped
    code: str | None = None
    renamed_row_id: int | None = None
    hold_id: int | None = None


async def _head_of(session: AsyncSession, memory_id: int) -> MemoryModel | None:
    seen = {memory_id}
    row = await session.get(MemoryModel, memory_id)
    while row is not None and row.superseded_by is not None and row.superseded_by not in seen:
        seen.add(row.superseded_by)
        nxt = await session.get(MemoryModel, row.superseded_by)
        if nxt is None:
            break
        row = nxt
    return row


async def _resolve_identity(
    session: AsyncSession, path: str, raw_anchor_id: int | None, absent_paths: set[str]
) -> tuple[str, VaultFile | None]:
    """One of 'a'/'b'/'c'/'d'/'e' and the row it names, if any (plan 7.1)."""
    tracked = (
        await session.execute(select(VaultFile).where(VaultFile.path == path, VaultFile.role == "fact"))
    ).scalar_one_or_none()
    if tracked is not None:
        return "a", tracked
    if raw_anchor_id is None:
        return "e", None
    head = await _head_of(session, raw_anchor_id)
    if head is None:
        return "d", None
    target = (
        await session.execute(
            select(VaultFile).where(VaultFile.memory_id == head.id, VaultFile.role == "fact")
        )
    ).scalar_one_or_none()
    if target is None:
        return "d", None
    return ("b", target) if target.path in absent_paths else ("c", target)


async def ingest_file(
    session: AsyncSession,
    client: VaultClient,
    *,
    path: str,
    content: str,
    disk_sha256: str,
    epoch: str,
    absent_paths: set[str],
    clock: Clock,
    budget,
    max_pinned: int,
    timezone: str = "UTC",
) -> IngestOutcome:
    """Ingests one Anchor/Memory/ file. Logs its outcome by code/kind
    only -- never the path or the fact text (plan section 13)."""
    outcome = await _ingest_file(
        session,
        client,
        path=path,
        content=content,
        disk_sha256=disk_sha256,
        epoch=epoch,
        absent_paths=absent_paths,
        clock=clock,
        budget=budget,
        max_pinned=max_pinned,
        timezone=timezone,
    )
    if outcome.kind == "quarantined":
        logger.info("vault fact quarantined", extra={"reason_code": outcome.code})
    elif outcome.kind == "held":
        logger.info("vault rule hold opened", extra={"hold_id": outcome.hold_id})
    elif outcome.kind in ("created", "changed"):
        logger.info("vault fact ingested", extra={"event": outcome.kind})
    return outcome


async def _ingest_file(
    session: AsyncSession,
    client: VaultClient,
    *,
    path: str,
    content: str,
    disk_sha256: str,
    epoch: str,
    absent_paths: set[str],
    clock: Clock,
    budget,
    max_pinned: int,
    timezone: str = "UTC",
) -> IngestOutcome:
    from app.core import clock as clock_module
    from app.vault import render  # local import: avoids a cycle at module load

    meta = frontmatter.load(content)
    raw_anchor_id = meta.get("anchor_id") if isinstance(meta, dict) else None
    if not (isinstance(raw_anchor_id, int) and not isinstance(raw_anchor_id, bool)):
        raw_anchor_id = None

    case, row = await _resolve_identity(session, path, raw_anchor_id, absent_paths)

    if row is not None and row.state == "held":
        return IngestOutcome("held")

    if case == "c":
        # A second file names an anchor_id a present, tracked row already
        # answers to. Nothing in the database changes.
        target = (
            await session.execute(select(VaultFile).where(VaultFile.path == path, VaultFile.role == "fact"))
        ).scalar_one_or_none()
        if target is None:
            target = VaultFile(path=path, role="fact")
            session.add(target)
        target.state, target.reason = "quarantined", errors.DUPLICATE_FILE
        target.disk_sha256 = disk_sha256
        target.render_digest = None
        await session.commit()
        return IngestOutcome("quarantined", errors.DUPLICATE_FILE)

    renamed_id: int | None = None
    if case == "b":
        row.path = path
        row.missing_since = None
        renamed_id = row.id

    reuse_row: VaultFile | None = None
    if row is not None and row.memory_id is None and row.state == "quarantined":
        # Bug fix: a row quarantined on first sight (bad_type, too_long,
        # technique, duplicate_fact, pin_cap...) never got a memory of
        # its own. Left as "an existing row with memory_id None", the
        # existing-row branch below would read it as *forgotten* and
        # skip it forever. Reuse it as the new fact's row instead, so
        # fixing the file on disk is enough to bring it in.
        reuse_row = row
        row = None

    parsed = parse_fact(content)
    if isinstance(parsed, Ignored):
        return IngestOutcome("ignored")

    if row is None:

        def _quarantine(code: str) -> VaultFile:
            target = reuse_row or VaultFile(path=path, role="fact")
            target.state, target.reason = "quarantined", code
            target.memory_id, target.render_digest = None, None
            target.disk_sha256 = disk_sha256
            session.add(target)
            return target

        # New fact (cases d, e), or a previously-quarantined row whose
        # file is being retried. Quarantine attaches to that row.
        if isinstance(parsed, Quarantined):
            _quarantine(parsed.code)
            await session.commit()
            return IngestOutcome("quarantined", parsed.code)
        if parsed.kind == "technique":
            _quarantine(errors.TECHNIQUE)
            await session.commit()
            return IngestOutcome("quarantined", errors.TECHNIQUE)
        if parsed.kind == "rule":
            fresh = reuse_row or VaultFile(path=path, role="fact")
            fresh.memory_id = None
            fresh.disk_sha256 = disk_sha256
            session.add(fresh)
            await session.flush()
            hold = await holds.open_rule_hold(
                session,
                file_id=fresh.id,
                kind=parsed.kind,
                text=parsed.fact,
                supersedes_id=None,
                clock=clock,
            )
            fresh.state, fresh.reason, fresh.hold_id = "held", None, hold.id
            await session.commit()
            return IngestOutcome("held", hold_id=hold.id)
        if parsed.pinned and await memory.count_pinned(session) >= max_pinned:
            # Bug fix: a brand-new pinned fact must respect the cap too,
            # rather than silently writing it unpinned.
            _quarantine(errors.PIN_CAP)
            await session.commit()
            return IngestOutcome("quarantined", errors.PIN_CAP)
        if budget.left <= 0 or not budget.take():
            return IngestOutcome("skipped")
        written = await memory.write_memory(
            session, kind=parsed.kind, text=parsed.fact, source="vault", pinned=parsed.pinned, commit=False
        )
        if written is None:
            _quarantine(errors.DUPLICATE_FACT)
            await session.commit()
            return IngestOutcome("quarantined", errors.DUPLICATE_FACT)
        fresh = reuse_row or VaultFile(path=path, role="fact")
        fresh.state, fresh.reason = "ok", None
        fresh.memory_id, fresh.disk_sha256, fresh.render_digest = written.id, disk_sha256, None
        session.add(fresh)
        await session.commit()
        view = render.FactView(
            memory_id=written.id,
            kind=written.kind,
            text=written.text,
            pinned=written.pinned,
            source=written.source,
            created=clock_module.local_date_of(written.created_at, timezone),
        )
        extras = frontmatter.extra_segments(content, render.FACT_KEYS) or ""
        rendered = render.render_fact(view, epoch, extras)
        try:
            new_sha = await client.put_file(path, rendered.content, disk_sha256)
        except VaultError as exc:
            if exc.code != errors.CONFLICT:
                raise
            return IngestOutcome("skipped")
        fresh.disk_sha256, fresh.render_digest = new_sha, rendered.digest
        await session.commit()
        return IngestOutcome("created")

    # Existing row: cases a and b. A row with memory_id None and
    # state="quarantined" was already diverted to reuse_row above, so
    # reaching here with memory_id None means the memory was actually
    # forgotten (/forget's ON DELETE SET NULL) between passes; the next
    # render cleans up the file.
    if row.memory_id is None:
        return IngestOutcome("skipped")
    head = await session.get(MemoryModel, row.memory_id)
    if head is None:
        return IngestOutcome("skipped")

    if isinstance(parsed, Quarantined):
        row.state, row.reason = "quarantined", parsed.code
        row.disk_sha256 = disk_sha256
        await session.commit()
        return IngestOutcome("quarantined", parsed.code, renamed_row_id=renamed_id)

    if parsed.kind != head.kind and ("technique" in (parsed.kind, head.kind)):
        row.state, row.reason = "quarantined", errors.TECHNIQUE
        row.disk_sha256 = disk_sha256
        await session.commit()
        return IngestOutcome("quarantined", errors.TECHNIQUE, renamed_row_id=renamed_id)

    base_id = parsed.anchor_id if parsed.anchor_id is not None else head.id
    base = await session.get(MemoryModel, base_id) if base_id is not None else None
    if base is not None:
        # Bug fix: a hand-edited anchor_id can name a row from a
        # *different* lineage. Only trust it as the three-way base when
        # it actually leads to this file's head; otherwise every field
        # reads as "changed" relative to a stranger's text, which would
        # supersede the head with nonsense. Fall back to the head, which
        # degenerates the merge to file-vs-head, same as when base_id's
        # row is gone outright.
        base_head = await _head_of(session, base_id)
        if base_head is None or base_head.id != head.id:
            base = None
    if base is None:
        base = head

    fact_changed = parsed.fact != base.text
    kind_changed = parsed.kind != base.kind
    pinned_changed = parsed.pinned != base.pinned

    rule_involved = (fact_changed or kind_changed) and (parsed.kind == "rule" or head.kind == "rule")
    if rule_involved:
        hold = await holds.open_rule_hold(
            session,
            file_id=row.id,
            kind=parsed.kind,
            text=parsed.fact,
            supersedes_id=head.id,
            clock=clock,
        )
        row.state, row.reason, row.hold_id = "held", None, hold.id
        row.disk_sha256 = disk_sha256
        await session.commit()
        return IngestOutcome("held", hold_id=hold.id, renamed_row_id=renamed_id)

    row.state, row.reason = "ok", None
    applied = False
    if fact_changed or kind_changed:
        new_kind = parsed.kind if kind_changed else head.kind
        new_text = parsed.fact if fact_changed else head.text
        written = await memory.write_memory(
            session, kind=new_kind, text=new_text, source="vault", supersedes_id=head.id, commit=False
        )
        if written is None:
            row.state, row.reason = "quarantined", errors.DUPLICATE_FACT
            row.disk_sha256 = disk_sha256
            await session.commit()
            return IngestOutcome("quarantined", errors.DUPLICATE_FACT, renamed_row_id=renamed_id)
        row.memory_id = written.id
        head = written
        applied = True

    if pinned_changed:
        if parsed.pinned and not head.pinned:
            if await memory.count_pinned(session) >= max_pinned:
                row.state, row.reason, row.render_digest = "quarantined", errors.PIN_CAP, None
                row.disk_sha256 = disk_sha256
                await session.commit()
                return IngestOutcome("quarantined", errors.PIN_CAP, renamed_row_id=renamed_id)
        await memory.set_pinned(session, row.memory_id, parsed.pinned, commit=False)
        applied = True

    row.disk_sha256 = disk_sha256
    row.updated_at = clock.now_utc()
    await session.commit()
    return IngestOutcome("changed" if applied else "cosmetic", renamed_row_id=renamed_id)
