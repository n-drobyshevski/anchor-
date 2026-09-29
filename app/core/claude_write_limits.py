"""Bot-side caps on Claude's writes, mirroring vaultd's own copy
(anchor-claude-write-plan.md section 6.4, section 14 (rev. 3);
vaultd/vaultd/config.py, limits.py and undo.py hold vaultd's copy).

**Two kinds of cap.** The rate/volume caps in `SPECS` (files and
changesets, creates and bytes per day, undos, folders, moves) are
settings: the user tunes them by hand from Telegram (`/claude limits`)
or the web app's state screen, within each spec's bounds
(docs/decisions.md, "Claude write caps become settings"). The module
constants below are their defaults. An override is one
`claude_write_limit` row; `effective` overlays them. Never from the
environment -- a deploy still cannot widen them by pasting a variable.
`BYTES_PER_FILE`, `MAX_FOLDER_DEPTH` and `CHANGESET_IDLE` stay
constants: the first two touch vaultd's own body and path limits.

**vaultd's copy.** vaultd enforces seven of the nine caps itself
(`VAULT_KEYS`; creates and bytes per day are the bot's alone).
`set_limit` marks `vault_status.limits_push_pending`, and `push`
sends the full effective set to `PUT /v1/limits`. `reconcile` compares
vaultd's copy (`GET /v1/limits`) with the bot's on every vault sync
pass and pushes when they differ, so any drift heals itself. Until
then the two copies can disagree -- whichever is stricter wins, which
is the safe direction.

All caps except `CREATES_PER_DAY`/`BYTES_PER_CONNECTION_PER_DAY`/
`CHANGESETS_PER_HOUR`/`UNDOS_PER_HOUR`/`FOLDERS_PER_DAY`/`MOVES_PER_DAY`
are checked directly against an argument or a `claude_changeset` row
already in hand; those are enforced by summing `claude_changeset`
(app/web/claude_write.py) -- ledger-backed, not an in-memory tracker,
so a worker restart cannot loosen them.

**FILES_PER_CHANGESET covers content writes only** (`update_note`,
`create_note`) -- rev. 3 splits a rename's own files off into
`MOVE_FILES_PER_CHANGESET`/`MOVES_PER_DAY`, entirely separate. The bot
cannot know in advance how many folders a nested `create_note`/
`rename_note` will need (vaultd resolves the tree), so
`FOLDERS_PER_CHANGESET`/`FOLDERS_PER_DAY` and the move caps are
enforced authoritatively by vaultd itself (app/vault/client.py's
`folders_created`/`files_moved`); the bot's own check against its
ledger, before ever calling vaultd, is a fast local rejection once a
budget is already exhausted -- the same belt-and-suspenders shape as
every other mirrored cap here.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import asdict, dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.db.models import ClaudeWriteLimit, VaultStatus
from app.vault.client import VaultClient
from app.vault.errors import VaultError

logger = logging.getLogger(__name__)

FILES_PER_CHANGESET = 20
CHANGESETS_PER_HOUR = 4
CREATES_PER_DAY = 40
BYTES_PER_FILE = 64 * 1024
BYTES_PER_CONNECTION_PER_DAY = 512 * 1024
UNDOS_PER_HOUR = 4

# Rev. 3 (anchor-claude-write-plan.md section 14): folder auto-creation
# and the move budget, mirroring vaultd's own copy exactly.
FOLDERS_PER_CHANGESET = 3
FOLDERS_PER_DAY = 10
MAX_FOLDER_DEPTH = 4
MOVE_FILES_PER_CHANGESET = 20
MOVES_PER_DAY = 60

# A changeset is all writes by one connection within this idle window
# (plan section 6.3): reuse the open one if the last write was under
# this long ago, else mint a new one.
CHANGESET_IDLE = datetime.timedelta(minutes=10)


@dataclass(frozen=True)
class Spec:
    default: int
    min: int
    max: int
    label: str


# Order is display order (Telegram and the web app). Keys match
# vaultd's limits.py; bounds must match too for the seven it shares.
SPECS: dict[str, Spec] = {
    "files_per_changeset": Spec(FILES_PER_CHANGESET, 1, 200, "Файлов в одном пакете"),
    "changesets_per_hour": Spec(CHANGESETS_PER_HOUR, 0, 60, "Пакетов в час"),
    "creates_per_day": Spec(CREATES_PER_DAY, 0, 500, "Новых заметок в день"),
    "bytes_per_day": Spec(BYTES_PER_CONNECTION_PER_DAY, 0, 8 * 1024 * 1024, "Объём текста в день"),
    "undos_per_hour": Spec(UNDOS_PER_HOUR, 0, 60, "Откатов в час"),
    "folders_per_changeset": Spec(FOLDERS_PER_CHANGESET, 0, 20, "Новых папок в пакете"),
    "folders_per_day": Spec(FOLDERS_PER_DAY, 0, 100, "Новых папок в день"),
    "move_files_per_changeset": Spec(MOVE_FILES_PER_CHANGESET, 1, 200, "Файлов при переносе в пакете"),
    "moves_per_day": Spec(MOVES_PER_DAY, 0, 600, "Файлов при переносах в день"),
}

# The caps vaultd enforces too (and so receives on a push).
VAULT_KEYS = (
    "files_per_changeset",
    "changesets_per_hour",
    "undos_per_hour",
    "folders_per_changeset",
    "folders_per_day",
    "move_files_per_changeset",
    "moves_per_day",
)


@dataclass(frozen=True)
class Limits:
    files_per_changeset: int = FILES_PER_CHANGESET
    changesets_per_hour: int = CHANGESETS_PER_HOUR
    creates_per_day: int = CREATES_PER_DAY
    bytes_per_day: int = BYTES_PER_CONNECTION_PER_DAY
    undos_per_hour: int = UNDOS_PER_HOUR
    folders_per_changeset: int = FOLDERS_PER_CHANGESET
    folders_per_day: int = FOLDERS_PER_DAY
    move_files_per_changeset: int = MOVE_FILES_PER_CHANGESET
    moves_per_day: int = MOVES_PER_DAY

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


DEFAULTS = Limits()


class LimitError(ValueError):
    """An unknown cap name, or a value outside its spec's bounds."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code  # "unknown_key" | "out_of_range"


async def overrides(session: AsyncSession) -> dict[str, int]:
    """The user's overrides, name -> value; unknown names are ignored."""
    rows = (await session.execute(select(ClaudeWriteLimit))).scalars().all()
    return {row.name: row.value for row in rows if row.name in SPECS}


async def effective(session: AsyncSession) -> Limits:
    """The caps in force right now: the defaults with the overrides on
    top. An override outside its current bounds (bounds narrowed in a
    later release) is clamped, never trusted as-is."""
    values = DEFAULTS.as_dict()
    for name, value in (await overrides(session)).items():
        spec = SPECS[name]
        values[name] = min(max(value, spec.min), spec.max)
    return Limits(**values)


def check_value(key: str, value: int) -> None:
    spec = SPECS.get(key)
    if spec is None:
        raise LimitError("unknown_key")
    if isinstance(value, bool) or not isinstance(value, int) or not spec.min <= value <= spec.max:
        raise LimitError("out_of_range")
    # Shown and edited in whole KB everywhere (Telegram and the web), so
    # a byte count between two KB could neither be displayed nor typed.
    if key == "bytes_per_day" and value % 1024:
        raise LimitError("out_of_range")


async def _mark_push_pending(session: AsyncSession, pending: bool) -> None:
    stmt = (
        pg_insert(VaultStatus)
        .values(id=1, limits_push_pending=pending)
        .on_conflict_do_update(index_elements=[VaultStatus.id], set_={"limits_push_pending": pending})
    )
    await session.execute(stmt)


async def set_limit(session: AsyncSession, clock: Clock, key: str, value: int | None) -> Limits:
    """Set one cap (`value`), or put it back to its default (`None`).
    Commits, marks vaultd's copy as needing a push, and returns the new
    effective caps. Raises LimitError without touching the DB."""
    if value is None:
        if key not in SPECS:
            raise LimitError("unknown_key")
        await session.execute(delete(ClaudeWriteLimit).where(ClaudeWriteLimit.name == key))
    else:
        check_value(key, value)
        stmt = pg_insert(ClaudeWriteLimit).values(name=key, value=value, updated_at=clock.now_utc())
        stmt = stmt.on_conflict_do_update(
            index_elements=[ClaudeWriteLimit.name],
            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
        )
        await session.execute(stmt)
    if key in VAULT_KEYS:
        await _mark_push_pending(session, True)
    await session.commit()
    logger.info("claude write limit set", extra={"event": "claude_limit_set", "fields": key})
    return await effective(session)


async def reset_all(session: AsyncSession) -> Limits:
    """Every cap back to its default."""
    await session.execute(delete(ClaudeWriteLimit))
    await _mark_push_pending(session, True)
    await session.commit()
    logger.info("claude write limits reset", extra={"event": "claude_limit_reset"})
    return DEFAULTS


async def push_pending(session: AsyncSession) -> bool:
    row = await session.get(VaultStatus, 1)
    return bool(row is not None and row.limits_push_pending)


def _vault_subset(limits: Limits) -> dict[str, int]:
    values = limits.as_dict()
    return {key: values[key] for key in VAULT_KEYS}


async def _settle(session: AsyncSession, landed: dict) -> None:
    """Clear the pending mark only if what vaultd now holds is what the
    bot wants *now*, read after the request: a cap changed while the
    PUT was in flight (another tab, Telegram) leaves it pending, and the
    next reconcile sends the newer values."""
    await session.commit()  # a fresh transaction, so `effective` sees the latest
    if landed == _vault_subset(await effective(session)):
        await _mark_push_pending(session, False)
        await session.commit()


async def push(session: AsyncSession, client) -> bool:
    """Send vaultd the full effective set of its seven caps. True if the
    PUT landed; False leaves the change pending, and the next
    `reconcile` retries it."""
    want = _vault_subset(await effective(session))
    try:
        landed = await client.put_limits(want)
    except VaultError as exc:
        logger.warning("claude write limits push deferred", extra={"error_code": exc.code})
        return False
    await _settle(session, landed)
    return True


async def reconcile(session: AsyncSession, client) -> bool:
    """Make vaultd's copy match the bot's: read `GET /v1/limits` and
    push only when it differs. Self-healing, so it covers every way the
    two can drift -- a failed push, two pushes landing out of order, a
    lost or unreadable `limits.json`, a `/v1/purge` that reset vaultd's
    copy after a cap was set. Called by every vault sync pass, and by
    `/state`'s and `/vault`'s probe while a change is pending (the sync
    pass does not run in `status` mode). True when the copies match."""
    try:
        held = await client.get_limits()
    except VaultError as exc:
        logger.warning("claude write limits check deferred", extra={"error_code": exc.code})
        return False
    if held == _vault_subset(await effective(session)):
        if await push_pending(session):
            await _settle(session, held)
        return True
    return await push(session, client)


async def set_and_push(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    key: str,
    value: int | None,
    client_factory=VaultClient.from_settings,
) -> tuple[Limits, bool | None]:
    """`set_limit` (or `reset_all` for `key == "*"`), then push vaultd's
    copy straight away -- what Telegram and the web app both call.
    Returns the new caps and whether the push landed: True, False
    (saved; the vault sync pass will retry), or None (nothing to push:
    a bot-only cap, or the vault is off)."""
    if key == "*":
        new = await reset_all(session)
    else:
        new = await set_limit(session, clock, key, value)
    if (key != "*" and key not in VAULT_KEYS) or settings.VAULT_MODE == "off":
        return new, None
    return new, await push(session, client_factory(settings))


def format_value(key: str, value: int) -> str:
    if key == "bytes_per_day":
        return f"{value // 1024} КБ"
    return str(value)


def parse_value(key: str, raw: str) -> int | None:
    """A typed value: a plain integer. `bytes_per_day` is typed in KB,
    like it is shown (`512`), or with a unit (`512k`, `2m`). None if it
    is not a number at all -- ASCII digits only: `str.isdigit` also
    accepts `²`, which `int` then refuses."""
    raw = raw.strip().lower()
    factor = 1024 if key == "bytes_per_day" else 1
    if key == "bytes_per_day" and raw[-1:] in ("k", "m", "к", "м"):
        factor = 1024 if raw[-1] in ("k", "к") else 1024 * 1024
        raw = raw[:-1]
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 9:
        return None
    return int(raw) * factor
