"""The `BOARD_SYNC` job: one pass of keeping the user's task boards and
the planner in step (app/planner/board_sync.py decides, this module
reads, writes and stores).

**Its own job kind, in the background.** The user chose a background
sync over an on-command one, knowing it relaxes "Echo writes to the
vault only on the user's tap" (docs/decisions.md, "Board sync"). So it is
a job of its own, never an idle job: app/worker.py enqueues it after a
successful `PLANNER_SYNC` (`maybe_enqueue`), at most once per 15 minutes,
only while `BOARD_SYNC_ENABLED` is on, the planner link is active and
`VAULT_MODE` is not `off`. Its one vault write goes through
app/core/echo_write.py's `write_board`; tests/test_echo_isolation.py
names this module as the one caller, the documented exception to the
tap rule, and tests/test_idle_isolation.py keeps idle away from it.

**One pass, in an order where a crash can only cause a harmless repeat:**

1. Read every mapped board (`GET /v1/boards`) and, per board, the
   planner's own top-level tasks with its tag (`list_tasks`), and the
   stored pairs; once per pass, the user's own open top-level tasks
   with no tag, which a card may adopt by title (board_sync's module
   docstring). Compute every board's plan; a task adopted by one board
   is not offered to the next.
2. Per board: the planner creates (idempotent by `clientRequestId`, so a
   pass that dies after one and before the board write gets the same
   task back next time, or pairs it by title).
3. `POST /v1/boards` with the board's batch, under the pass's one fresh
   changeset (`boards_...`). A 409 (the user typed, Sync landed) changes
   nothing more for that board this pass; the next pass reads it again.
4. Pairs whose unit needs no planner call are committed now.
5. The planner updates (title, due, done, untag, tag), each unit's row
   committed only once its calls succeeded: a row never records a value
   the planner has not taken, so a lost call is retried, never reverted.

A planner or vault failure aborts the pass quietly: logged by event name
only, recorded as an outcome in `board_sync_run`, never retried by the
queue (the next pass is 15 minutes away), and the agenda snapshot is
never touched. `/boards now` and `/boards preview` queue the same job
with a trigger; the preview reads and computes and writes nothing
anywhere -- not the boards, not the planner, not a pair, not a run row --
and works with the flag off, so the first pairing can be checked before
the flag goes on.

Nothing here logs a title, a card's text or a board's name: events and
counts only.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import echo_write
from app.core.clock import Clock
from app.db.jobs import enqueue_job
from app.db.models import BoardPair, BoardSyncRun
from app.planner import auth as planner_auth
from app.planner import board_sync
from app.planner.auth import PlannerAuthError
from app.planner.board_sync import Counts, Plan
from app.planner.client import PlannerToolError, PlannerUnavailable
from app.vault.client import TaskBoard
from app.vault.errors import VaultError

logger = logging.getLogger(__name__)

BOARD_SYNC = "board_sync"

TRIGGER_AUTO = "auto"
TRIGGER_NOW = "now"
TRIGGER_PREVIEW = "preview"
TRIGGERS = (TRIGGER_AUTO, TRIGGER_NOW, TRIGGER_PREVIEW)

# Pass-level outcomes (BoardSyncRun.outcome with `board` null), and the
# per-board ones besides `ok`.
OK = "ok"
CHANGED = "changed"
UNAVAILABLE = "unavailable"
REFUSED = "refused"
PLANNER_UNAVAILABLE = "planner_unavailable"
NO_BOARDS = "no_boards"
MISSING = "missing"
NOT_BOARD = "not_board"
# Not stored: what a preview or a `/boards now` says when the gates are shut.
DISABLED = "disabled"
VAULT_OFF = "vault_off"
NOT_LINKED = "not_linked"

RUNS_KEEP_DAYS = 14
BUCKET_MIN = 15

_PLANNER_ERRORS = (PlannerUnavailable, PlannerToolError, PlannerAuthError)


class _Abort(Exception):
    """The pass stops here; `outcome` says why."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


@dataclass
class BoardResult:
    name: str
    outcome: str
    counts: Counts = field(default_factory=Counts)


@dataclass
class PassResult:
    """What one pass did (or, for a preview, would do). Counts and codes
    only: what the user is shown and what `board_sync_run` holds."""

    trigger: str
    outcome: str
    boards: list[BoardResult] = field(default_factory=list)

    @property
    def counts(self) -> Counts:
        total = Counts()
        for board in self.boards:
            total.add(board.counts)
        return total


# -- enqueue ---------------------------------------------------------------------------


def gates_open(settings: Settings) -> bool:
    """The deploy-level gates of an automatic pass (the planner link is
    checked against the database by the callers)."""
    return settings.BOARD_SYNC_ENABLED and settings.PLANNER_ENABLED and settings.VAULT_MODE != "off"


def dedup_key(clock: Clock) -> str:
    now = clock.now_utc()
    return f"{BOARD_SYNC}:{now.date().isoformat()}:{now.hour}:{now.minute // BUCKET_MIN}"


async def maybe_enqueue(session: AsyncSession, settings: Settings, clock: Clock) -> bool:
    """After a successful PLANNER_SYNC: queue one automatic pass, at most
    one per 15-minute bucket, only while every gate is open."""
    if not gates_open(settings) or not await planner_auth.is_enabled(session):
        return False
    enqueued = await enqueue_job(session, BOARD_SYNC, {"trigger": TRIGGER_AUTO}, dedup_key=dedup_key(clock))
    if enqueued:
        logger.info("board sync queued", extra={"event": BOARD_SYNC})
    return enqueued


async def enqueue_command(session: AsyncSession, clock: Clock, trigger: str) -> bool:
    """`/boards now` or `/boards preview`: queue one pass of that trigger
    right away. A double tap within the same minute queues one."""
    if trigger not in (TRIGGER_NOW, TRIGGER_PREVIEW):
        raise ValueError(f"not a command trigger: {trigger}")
    minute = clock.now_utc().strftime("%Y-%m-%dT%H:%M")
    return await enqueue_job(
        session, BOARD_SYNC, {"trigger": trigger}, dedup_key=f"{BOARD_SYNC}:{trigger}:{minute}"
    )


# -- reading -----------------------------------------------------------------------------


def _read_rows(raw: object) -> list[tuple[board_sync.Task, str | None]]:
    """`list_tasks`' answer, strictly: a row of the wrong shape is a
    planner failure (the pass aborts), never a task the merge could act
    on wrongly. Each task with its parent id."""
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        raise PlannerToolError("list_tasks: malformed answer")
    out = []
    seen: set[str] = set()
    for item in raw["tasks"]:
        if not isinstance(item, dict):
            raise PlannerToolError("list_tasks: malformed row")
        task_id, title, done = item.get("id"), item.get("title"), item.get("done")
        tags, due, parent = item.get("tags", []), item.get("dueDate"), item.get("parentId")
        if not (isinstance(task_id, str) and task_id and isinstance(title, str) and isinstance(done, bool)):
            raise PlannerToolError("list_tasks: malformed row")
        if not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
            raise PlannerToolError("list_tasks: malformed tags")
        if due is not None and not isinstance(due, str):
            raise PlannerToolError("list_tasks: malformed due date")
        if task_id in seen:
            raise PlannerToolError("list_tasks: a task twice")
        seen.add(task_id)
        out.append((board_sync.Task(id=task_id, title=title, done=done, due=due, tags=tuple(tags)), parent))
    return out


def parse_tasks(raw: object, tag: str) -> list[board_sync.Task]:
    """`list_tasks`' answer for one tag as merge input. A subtask or a
    row without the tag (neither should come back) is left out."""
    return [task for task, parent in _read_rows(raw) if parent is None and tag in task.tags]


def parse_untagged(raw: object) -> list[board_sync.Task]:
    """`list_tasks`' answer with no tag filter, cut to the tasks a card
    may adopt: top-level, open, with no tag at all."""
    return [task for task, parent in _read_rows(raw) if parent is None and not task.done and not task.tags]


def as_board(board: TaskBoard) -> board_sync.Board:
    cards = tuple(
        board_sync.Card(n=c.n, id=c.id, text=c.text, checked=c.checked, date=c.date, lane=c.lane)
        for c in board.cards
    )
    return board_sync.Board(name=board.name, dates=board.dates, cards=cards, sha256=board.sha256 or "")


async def load_rows(session: AsyncSession, board: str) -> list[board_sync.Row]:
    rows = (await session.execute(select(BoardPair).where(BoardPair.board == board))).scalars().all()
    return [
        board_sync.Row(
            block_id=r.block_id,
            task_id=r.task_id,
            title=r.title,
            done=r.done,
            due=r.due.isoformat() if r.due else None,
            status=r.status,
        )
        for r in rows
    ]


# -- storing ------------------------------------------------------------------------------


async def _apply_row(
    session: AsyncSession, board: str, change: board_sync.RowChange, task_id: str | None, now: datetime.datetime
) -> None:
    """One row change, flushed, not committed."""
    existing = (
        await session.execute(
            select(BoardPair).where(BoardPair.board == board, BoardPair.block_id == change.block_id)
        )
    ).scalar_one_or_none()
    if change.op == "delete":
        if existing is not None:
            await session.delete(existing)
        await session.flush()
        return
    due = datetime.date.fromisoformat(change.due) if change.due else None
    if existing is None:
        existing = BoardPair(board=board, block_id=change.block_id, created_at=now)
        session.add(existing)
    existing.task_id = task_id or change.task_id
    existing.title = change.title
    existing.done = change.done
    existing.due = due
    existing.status = change.status
    existing.updated_at = now
    await session.flush()


def _run_row(now: datetime.datetime, board: str | None, outcome: str, counts: Counts | None = None) -> BoardSyncRun:
    values = {}
    if counts is not None:
        values = {name: getattr(counts, name) for name in Counts.__dataclass_fields__}
    return BoardSyncRun(at=now, board=board, outcome=outcome, **values)


async def forget_boards(session: AsyncSession, names: list[str]) -> int:
    """Drop every pair of these boards (after `/boards undo` put them back
    as they were): the next pass pairs them again by title instead of
    reading the undone cards as deleted. Commits."""
    if not names:
        return 0
    result = await session.execute(delete(BoardPair).where(BoardPair.board.in_(names)))
    await session.commit()
    return result.rowcount or 0


# -- the pass ------------------------------------------------------------------------------


async def run_pass(
    session: AsyncSession,
    settings: Settings,
    planner,
    vault,
    clock: Clock,
    *,
    trigger: str = TRIGGER_AUTO,
) -> PassResult:
    """One pass (module docstring). `planner` is a PlannerClient (or a
    fake with the same methods), `vault` a VaultClient. Never raises for
    a planner or vault failure: the outcome says it."""
    dry_run = trigger == TRIGGER_PREVIEW
    result = PassResult(trigger=trigger, outcome=OK)
    if settings.VAULT_MODE == "off":
        result.outcome = VAULT_OFF
        return result
    if not dry_run and not settings.BOARD_SYNC_ENABLED:
        result.outcome = DISABLED
        return result
    if not settings.PLANNER_ENABLED or not await planner_auth.is_enabled(session):
        result.outcome = NOT_LINKED
        return result

    now = clock.now_utc()
    try:
        # A pass the user asked for by hand is the one way past the mass-loss
        # guard (board_sync.merge); a preview keeps it, to show what would be held.
        plans = await _read_and_plan(
            session, settings, planner, vault, clock, result, guard=trigger != TRIGGER_NOW
        )
        if not dry_run:
            changeset = echo_write.new_boards_ref()
            for tag, board, plan in plans:
                outcome = await _carry_out(session, settings, planner, vault, clock, tag, board, plan, changeset)
                # A board that changed under the pass did none of it.
                counts = plan.counts if outcome == OK else Counts()
                result.boards.append(BoardResult(board.name, outcome, counts))
                session.add(_run_row(now, board.name, outcome, counts))
                await session.commit()
        else:
            result.boards.extend(BoardResult(board.name, OK, plan.counts) for _tag, board, plan in plans)
    except _Abort as abort:
        result.outcome = abort.outcome
    except _PLANNER_ERRORS:
        result.outcome = PLANNER_UNAVAILABLE

    if not dry_run:
        await session.rollback()
        for board in result.boards:
            if board.outcome in (MISSING, NOT_BOARD):
                session.add(_run_row(now, board.name, board.outcome))
        if result.outcome != OK:
            session.add(_run_row(now, None, result.outcome))
        await session.execute(
            delete(BoardSyncRun).where(BoardSyncRun.at < now - datetime.timedelta(days=RUNS_KEEP_DAYS))
        )
        await session.commit()
    logger.info(
        "board sync pass",
        extra={"event": f"board_sync_{result.outcome}", "count": len(result.boards)},
    )
    return result


async def _read_and_plan(
    session: AsyncSession, settings: Settings, planner, vault, clock: Clock, result: PassResult, *, guard: bool = True
) -> list[tuple[str, board_sync.Board, Plan]]:
    """Step 1: every read, then every plan. Records `missing`/`not_board`
    boards on `result` and raises `_Abort` when there is nothing to do."""
    try:
        boards = await vault.get_boards()
    except VaultError:
        raise _Abort(UNAVAILABLE) from None
    mapped = [b for b in boards if b.name in settings.BOARD_SYNC_TAGS]
    if not mapped:
        raise _Abort(NO_BOARDS)
    plans = []
    untagged: list[board_sync.Task] | None = None
    for board in sorted(mapped, key=lambda b: b.name):
        if board.state != "ok":
            result.boards.append(BoardResult(board.name, MISSING if board.state == "missing" else NOT_BOARD))
            continue
        if untagged is None:
            raw = await planner.list_tasks(settings, session, clock, tag=None, include_completed=False)
            untagged = parse_untagged(raw)
        tag = settings.BOARD_SYNC_TAGS[board.name]
        raw = await planner.list_tasks(settings, session, clock, tag=tag)
        tasks = parse_tasks(raw, tag)
        rows = await load_rows(session, board.name)
        merged = as_board(board)
        plan = board_sync.merge(
            merged,
            tasks,
            rows,
            tag=tag,
            creates_max=settings.BOARD_SYNC_CREATES_PER_PASS,
            guard=guard,
            untagged=untagged,
        )
        # One task, one board: what this board adopts is not offered to the next.
        taken = {call.task_id for call in plan.calls if call.kind == "tag"}
        untagged = [task for task in untagged if task.id not in taken]
        plans.append((tag, merged, plan))
    return plans


def _created_id(answer: object) -> str:
    task_id = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise PlannerToolError("create_task: no id")
    return task_id


async def _carry_out(
    session: AsyncSession,
    settings: Settings,
    planner,
    vault,
    clock: Clock,
    tag: str,
    board: board_sync.Board,
    plan: Plan,
    changeset: str,
) -> str:
    """Steps 2-5 for one board. Returns its outcome; raises `_Abort` (or
    a planner error) to stop the pass."""
    now = clock.now_utc()
    known_tasks = {row.task_id for row in await load_rows(session, board.name)}

    # 2. Planner creates.
    created: dict[str, str] = {}
    for create in plan.creates:
        answer = await planner.create_task(
            settings, session, clock,
            title=create.title, due_date=create.due, client_request_id=create.request_id, tags=[create.tag],
        )
        task_id = _created_id(answer)
        if create.done:
            await planner.complete_task(settings, session, clock, task_id=task_id, done=True)
        created[create.block_id] = task_id

    # A create retried by its request id may answer with a task this pass
    # also meant to add as a card, or one already paired: the card being
    # tagged now is that task's card, so the add goes, and a second pair
    # for one task is never made.
    answered = set(created.values())
    units = []
    claimed: set[str] = set()
    for unit in plan.units:
        if unit.adds_task is not None and unit.adds_task in answered:
            continue
        if unit.create is not None:
            task_id = created[unit.create.block_id]
            if task_id in known_tasks or task_id in claimed:
                units.append(board_sync.Unit(ops=unit.ops))
                continue
            claimed.add(task_id)
        units.append(unit)
    kept = Plan(board=board.name, units=units, counts=plan.counts)

    # 3. The board write.
    ops = kept.ops
    if ops:
        written = await echo_write.write_board(vault, board.name, board.sha256, ops, changeset)
        if written.outcome == echo_write.BOARD_CHANGED:
            return CHANGED
        if written.outcome == echo_write.REFUSED:
            raise _Abort(REFUSED)
        if written.outcome != echo_write.OK:
            raise _Abort(UNAVAILABLE)

    # 4. Rows that wait on nothing else.
    for unit in kept.units:
        if unit.row is not None and not unit.calls:
            task_id = created.get(unit.create.block_id) if unit.create is not None else None
            await _apply_row(session, board.name, unit.row, task_id, now)
    await session.commit()

    # 5. Planner updates, each unit's row once its calls are made.
    for unit in kept.units:
        if not unit.calls:
            continue
        for call in unit.calls:
            await _call(planner, settings, session, clock, call)
        if unit.row is not None:
            await _apply_row(session, board.name, unit.row, None, now)
        await session.commit()
    return OK


async def _call(planner, settings: Settings, session: AsyncSession, clock: Clock, call: board_sync.Call) -> None:
    if call.kind == "title":
        await planner.update_task(settings, session, clock, task_id=call.task_id, title=call.value)
    elif call.kind == "due":
        await planner.update_task(settings, session, clock, task_id=call.task_id, due_date=call.value)
    elif call.kind == "done":
        await planner.complete_task(settings, session, clock, task_id=call.task_id, done=bool(call.value))
    elif call.kind in ("untag", "tag"):
        await planner.update_task(settings, session, clock, task_id=call.task_id, tags=list(call.value))
    else:
        raise ValueError(f"unknown board call: {call.kind}")


# -- what /boards and the digest read ----------------------------------------------------


@dataclass(frozen=True)
class BoardStatus:
    name: str
    tag: str
    paired: int
    held: int
    last: BoardSyncRun | None


async def status(session: AsyncSession, settings: Settings) -> tuple[list[BoardStatus], BoardSyncRun | None]:
    """Each mapped board's stored state, and the newest pass-level run
    (a pass that never reached a board). Stored state only: no call."""
    out = []
    for name, tag in sorted(settings.BOARD_SYNC_TAGS.items()):
        rows = (await session.execute(select(BoardPair.status).where(BoardPair.board == name))).scalars().all()
        last = (
            await session.execute(
                select(BoardSyncRun)
                .where(BoardSyncRun.board == name)
                .order_by(BoardSyncRun.at.desc(), BoardSyncRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        out.append(BoardStatus(name, tag, len(rows), sum(1 for s in rows if s == board_sync.ROW_HELD), last))
    whole = (
        await session.execute(
            select(BoardSyncRun)
            .where(BoardSyncRun.board.is_(None))
            .order_by(BoardSyncRun.at.desc(), BoardSyncRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return out, whole


async def issues_between(
    session: AsyncSession, start: datetime.datetime, end: datetime.datetime
) -> tuple[int, int]:
    """(conflicts, skipped titles) over the passes in [start, end).

    Conflicts are summed: each is settled by the pass that found it. A
    skipped title is skipped again by every pass until one side changes,
    so summing would count one title ninety times a day; the figure is
    the most any one pass skipped."""
    runs = (
        await session.execute(
            select(BoardSyncRun.at, BoardSyncRun.conflicts, BoardSyncRun.skipped).where(
                BoardSyncRun.at >= start, BoardSyncRun.at < end, BoardSyncRun.board.is_not(None)
            )
        )
    ).all()
    per_pass: dict[datetime.datetime, int] = {}
    for at, _conflicts, skipped in runs:
        per_pass[at] = per_pass.get(at, 0) + skipped
    return sum(r[1] for r in runs), max(per_pass.values(), default=0)
