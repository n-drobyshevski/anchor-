"""The two-way merge between a task board in the vault and the planner's
tasks with that board's tag. Pure: no I/O, no clock, no database.

The user keeps tasks on Kanban Starlane boards at the root of the vault
(vaultd/vaultd/boards.py) and in the planner; a board is mapped to one
planner tag (`BOARD_SYNC_TAGS`). app/planner/board_job.py reads both
sides and the stored pair rows, calls `merge` once per board, and carries
out what it returns. This module only decides.

**The state** is one `Row` per tracked pair: the card's block id, the
task's id and the last synced snapshot of the three synced fields
(`title`, `done`, `due`). Every pass compares each side against that
snapshot, field by field:

- changed on one side only: carried to the other side;
- changed on both sides to different values: the planner wins, and it is
  counted as a conflict;
- changed on both sides to the same value: only the snapshot moves.

**New things.** A card with no row (no block id, or one no row knows)
becomes a planner task with the board's tag; a top-level, open task of
the user's own with the tag and no row becomes a card. Before either
rule, an unrowed card and an unrowed task are *paired* instead when the
card carries the id this module would mint for that task, or when their
titles are the same (stripped, case-insensitively); the planner's done
and due win on a fresh pair. Pairing runs on every pass, not only when
a board has no rows yet: a pass interrupted between the planner's
create and the board write leaves exactly such an unrowed couple, and
pairing is what keeps the next pass from duplicating it.

**Adopting untagged tasks.** A card still free after that is paired with
an open, top-level task of the user's own that carries *no tag at all*
and has the same title (`untagged`): the task gets the board's tag (a
`tag` call) and the pair is made as above, instead of a duplicate task.
So tasks the user kept in the planner before the board was mapped join
it by title. A task with any tag is never adopted (its tags are a
choice the user made), and an untagged task with no such card is never
touched.

**Gone things.** A card that left the board (deleted, archived) while
its task still has the tag: the tag is removed from the task, never the
task. A task that left the tagged list (untagged, deleted, no longer the
user's own) while its card is still there: the card is removed. Both
gone: the row is dropped. Nothing with no row is ever touched except by
the "new" rules.

**Subtasks** (`merge`'s `subtasks` and `sub_rows`). A card's nested
checklist (vaultd/vaultd/board_subs.py, depth 1..3) is kept in step with
its planner task's subtasks, for a pair that already had a row when the
pass began (a pair made in this pass waits a pass for its subtasks, so a
create never has to wait on another). One `SubRow` per tracked subtask
pair, with the same three-way field rules, screening and date rules as a
card. A subtask with no row pairs first with a planner subtask with no
row under the same (paired) parent, by minted id, then by title; a
subtask left on the card becomes a planner subtask of the mapped parent
(`create_task` with `parentId`, and a `sub_tag` op when it has no id), a
planner subtask left becomes a `sub_add` (as the parent's last child,
open or done: a task's subtasks are few, unlike the planner's whole done
history). A subtask whose parent is not paired yet waits. Gone from the
planner: the subtask leaves the card (`sub_remove`, with what is under
it). Gone from the card: the planner subtask is *not* deleted (the
client has no delete tool, on purpose); the pair is kept as `detached`
so no pass adds it back, and dropped once the planner subtask is gone
too. A detached subtask whose block id comes back on the same card (an
undo, a paste), its planner subtask still under that card's task, is a
pair again, compared against its old snapshot; one whose id turns up on
another card gives the id up (that card's subtask then pairs or is
created like any free one) and stays detached under an id no card
carries. Both gone: the row goes. A card that leaves takes its subtask rows
with it and touches no planner subtask. Order inside a card, and a
subtask moved between parents or cards, are not synced (moved to another
card reads as gone from this one). A board read without subtasks (an
older vaultd, `Board.subs_known` false) has its subtasks left alone.

**Dependencies** (`merge_deps`, across boards). A card's `after:` line
names the cards it waits for; the planner's is a task's `blockedBy`.
Only top-level cards carry the line, so only cards take part: a
subtask's dependencies stay planner-only (a nested checklist has no
`after:` line of its own, and inventing one would put structure in the
user's text that the plugin does not know). Both sides are read as
planner task ids through every board's rows at the start of the pass; a
link no row resolves is left as it was (written back unchanged), and a
blocker no board shows is never touched. The snapshot (`Row.deps`) is the
set both sides last agreed on: none yet, both get the union; otherwise a
change on one side is carried over, and when both changed the planner
wins. Only ids this pass can compare (blockers paired on a board read
this pass) are replaced in the snapshot; the others are carried over
unchanged, so a board missing for one pass does not later read as its
blockers removed. The card's line is rewritten whole by `after_set`, each
link labelled with the blocker's planner title; the task's whole
`blockedBy` is sent by `update_task`.

A segment of the line may also be a plain card title (`after: Buy
soil, [[#^k3f9a2|Spec]]`; vaultd reports it with `?titles=1`). It names
the paired top-level card of that title (`title_key`, the card itself
left out) on the card's own board when there is exactly one, else the
one across the boards read this pass. With every title found, the line
is rewritten as links on this pass even when its set is unchanged, so
the next pass reads links only; a title found nowhere, or on several
cards, makes the line one that cannot be written (below), its found
part still read, and the card counts as skipped each pass until the
user fixes it.

A line that cannot be written (text other than links on it, a label or
a link vaultd would refuse, more links than vaultd takes) is skipped and
retried, and its side is tracked apart: `deps` still moves to what was
agreed (the planner took it), and `Row.deps_card` records what the line
resolved to as read. While that is set, the line's own changes are read
against `deps_card` and the planner's against `deps` (the planner wins
when both moved), so the merge settles instead of resending the union,
and a blocker the planner only ever had is never read as removed on the
card; the first pass that writes the line clears it. A set larger than
the planner's 20 blockers is not sent: neither side moves, the snapshot
stays, skipped. A set the planner refuses (a cycle, the card's line
already written) counts as a conflict and a skip, and the snapshot is
stored as planned all the same: the next pass then reads the planner as
the side that moved and writes its set back to the card, taking the
loop's link off it (board_job.py).

**Lanes by role** (`merge`'s `states`). A card's lane is read as its
task's state: the board's done lane (`**Complete**`) is `done`, its
first other lane `planned`, any other lane `wip`; a checked card is
`done` wherever it sits, and on a board with no done lane `done` is the
box, as before. The planner's rows carry `state` (`planned`, `wip`,
`done`; `done` is the row's `done`), and for a top-level pair this
three-valued state replaces the two-valued done field, merged the same
three ways against `Row.state` (the planner wins a conflict, and a new
pair). A row from before states has no state snapshot and reads its base
from `Row.done`: done, else planned, the one state an open task had then.
Not "unknown, the planner wins": the planner's first states are its
defaults, and the board's in-progress lanes were the only record of
that state, so on the first pass a card in such a lane makes its task
wip rather than being moved back to the first lane. The card side is written as a
`check` for done (vaultd moves it into the done lane) and a `move` to the
first lane (planned) or the first lane that is neither the first nor the
done one (wip); a card already in a lane of the right role (a second
in-progress lane, "Review") is left where it is. The planner side is
`complete_task` for done and undone, as before, and `update_task`'s
`state` between planned and wip (after `complete_task(done=False)` when
a done task goes back to wip). A state the planner refuses (a task in no
collection, a collection with no in-progress column) is a conflict and a
skip, and the snapshot is stored as planned, as with a refused blocker
(below): the next pass reads the planner as the side that moved and puts
the card back in a lane of the planner's state. A board with no such middle lane shows
wip as planned: the comparison reads a planner wip as planned there, the
card is not moved, the snapshot keeps wip, and the pair counts as
skipped once, on the pass that first finds it so; the card's own moves
(ticked, unticked) still travel. A new card makes a task of its state
(created, then `update_task` state for wip); a new open task is added
in its state's lane (`add`, then `move` in the same batch for wip). The
rule is off (done/open, as before) for a pass whose planner rows carry
no `state` (an older planner), and for a board read from a vaultd that
does not name its done lane (an older one, which has no `move`), or whose
lane titles are not unique or all done. Subtasks keep done/open.

**What is not synced.** Continuation lines other than subtasks and
`after:`, descriptions, the archive, a card's place inside its lane. A board
whose dates use their own syntax (`dates: false`) syncs no due date in either direction. A done
planner task with no row is not added as a card: the planner keeps
every completed task forever, and adding them all to a board would bury
it (and past vaultd's 500 cards a board stops being one).

**Block ids the user wrote.** A card whose block id is not of the form
vaultd's write ops take (`^[a-z0-9]{4,16}$`; a hand-typed `^Plan-1` is
read but cannot be named back) is never paired, created for or written:
one op on it would make vaultd refuse the whole batch. It is counted as
skipped once per pass, keeps a pair it already had as it was, and keeps
an unrowed task of its title from becoming a second card.

**Titles.** vaultd refuses a whole batch for one bad card text, so
`card_text_ok` mirrors its check (vaultd/vaultd/boards.py's
`_check_text`) and a title that fails it is never sent: that card is not
added or retitled, the pass counts it as skipped, and everything else
goes on. A card text the planner cannot take as a title (empty, over 500
characters) is skipped the same way. Hashtags and `@@{time}` inside a
card's text are part of the title and travel verbatim.

**Caps and order.** At most `OPS_MAX` board operations per board per
pass (vaultd's limit) and `creates_max` planner creates (subtasks
included); what does not fit waits for the next pass. Units are taken in
a fixed order (existing pairs by block id, then new pairs, new cards, new
tasks, then subtasks card by card; dependencies last, in what room the
batch has left: `add_units`), so the same
input always gives the same output. A merge run again on the state its
own output produced yields no operation, no call and no row change.

**Ids.** Block ids the bot mints are 12 hex characters (vaultd wants
`^[a-z0-9]{4,16}$`), derived from the board's name and, for a new card,
its text and occurrence, for a new task, the task's id -- so the same
card or task gets the same id on every pass, and the planner create's
`clientRequestId` (built from the board and that id) makes a retried
create return the task it already made.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, replace

# vaultd's limits (vaultd/vaultd/config.py).
OPS_MAX = 100
CARD_MAX_CHARS = 500
BOARD_MAX_CARDS = 500
# The planner's own (lib/mcp/tools.ts: `title: z.string().min(1).max(500)`).
TITLE_MAX_CHARS = 500
CREATES_MAX_DEFAULT = 20
# A pass that would drop more pairs than this, and more than half of the
# board's pairs, drops none (`merge`'s mass-loss guard).
MASS_LOSS_MIN = 3
# vaultd's subtask and `after:` limits (vaultd/vaultd/config.py).
SUB_MAX_DEPTH = 3
SUBS_PER_CARD_MAX = 50
AFTER_LINKS_MAX = 20
# The planner's `update_task` takes at most this many blockers
# (lib/mcp/tools.ts: `MAX_BLOCKED_BY`).
BLOCKED_BY_MAX = 20

ID_RE = re.compile(r"^[a-z0-9]{4,16}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# vaultd/vaultd/boards.py's `_BLOCK_ID_RE` and `_DATE_RE`, copied exactly:
# a text that matches either would read back as structure.
_BLOCK_ID_RE = re.compile(r" \^([A-Za-z0-9-]+)$")
_DATE_RE = re.compile(r" ?@\{(\d{4}-\d{2}-\d{2})\}")
_BAD_CATEGORIES = ("Cc", "Cs", "Zl", "Zp")

ROW_OK = "ok"
# The pair has a field this pass could not carry (a title that cannot be
# a card or a task, a due date that is not a date): it is retried, and
# counted as skipped, on every pass until one side changes.
ROW_HELD = "held"
# A subtask pair whose subtask left the card while the planner kept it:
# never compared, never added back, dropped once the planner's is gone.
ROW_DETACHED = "detached"

# A task's state (module docstring, "Lanes by role").
STATE_PLANNED = "planned"
STATE_WIP = "wip"
STATE_DONE = "done"
STATES = (STATE_PLANNED, STATE_WIP, STATE_DONE)


# -- input -------------------------------------------------------------------------


@dataclass(frozen=True)
class Sub:
    """One subtask inside a card, as `GET /v1/boards?subs=1` reports it:
    `n` its index within the card (pre-order), `depth` 1..3."""

    n: int
    id: str | None
    depth: int
    text: str
    checked: bool
    date: str | None = None


@dataclass(frozen=True)
class Card:
    """One active card as `GET /v1/boards` reports it. `after` is the
    (board file name, block id) of each card its `after:` line names,
    `after_titles` the plain card titles on that line (in order);
    `after_raw_extra` whether that line holds anything else."""

    n: int
    id: str | None
    text: str
    checked: bool
    date: str | None
    lane: str = ""
    subs: tuple[Sub, ...] = ()
    after: tuple[tuple[str, str], ...] = ()
    after_raw_extra: bool = False
    after_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Board:
    """`subs_known` is false when the board was read without subtasks
    (an older vaultd): its subtasks and `after:` lines are then unknown,
    not empty. `lanes` are its lanes' titles in order, `done_lane` the
    done lane's title (None: it has none); `lanes_known` is false when
    vaultd did not say which lane is done (an older one, with no `move`)."""

    name: str
    dates: bool
    cards: tuple[Card, ...]
    sha256: str = ""
    subs_known: bool = False
    # When the board's file last changed (epoch seconds); None = unknown.
    modified: int | None = None
    lanes: tuple[str, ...] = ()
    done_lane: str | None = None
    lanes_known: bool = False


@dataclass(frozen=True)
class Task:
    """One of the planner's `list_tasks` rows: the user's own; top-level
    with the board's tag, or (`parent_id` set) a subtask under one.
    `state` is None for a row from a planner without states."""

    id: str
    title: str
    done: bool
    due: str | None = None
    tags: tuple[str, ...] = ()
    parent_id: str | None = None
    position: float = 0
    blocked_by: tuple[str, ...] = ()
    state: str | None = None


@dataclass(frozen=True)
class Row:
    """A tracked pair and its last synced snapshot. `deps` is the sorted
    task ids of the card's dependencies both sides last agreed on, None
    until they were first compared. `deps_card` is None while the card's
    `after:` line shows `deps`; when the line could not be written, it is
    what the line resolved to when last read (module docstring,
    "Dependencies"). `state` is the task's state as last synced, None
    until a pass compared states (module docstring, "Lanes by role")."""

    block_id: str
    task_id: str
    title: str
    done: bool
    due: str | None
    status: str = ROW_OK
    deps: tuple[str, ...] | None = None
    deps_card: tuple[str, ...] | None = None
    state: str | None = None


@dataclass(frozen=True)
class SubRow:
    """A tracked subtask pair: the subtask's block id, the card it is in,
    the subtask it is under (None: right under the card), the planner
    subtask, and the snapshot."""

    block_id: str
    card_block_id: str
    task_id: str
    parent_block_id: str | None
    title: str
    done: bool
    due: str | None
    status: str = ROW_OK


# -- output ------------------------------------------------------------------------


@dataclass(frozen=True)
class Create:
    """A planner `create_task` (then `complete_task` when `done`, or
    `update_task` state when `state` is set: a card in an in-progress
    lane). The task id is the planner's answer; `request_id` makes a
    retry return the task already made. With `parent_task_id`, a subtask
    of that task (`tag` is then None: a subtask carries none)."""

    block_id: str
    title: str
    due: str | None
    done: bool
    tag: str | None
    request_id: str
    parent_task_id: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class Call:
    """A planner write on an existing task, made after the board write.

    `kind` is `title` (`update_task` title), `due` (`update_task`
    dueDate, None clears it), `done` (`complete_task`), `untag`
    (`update_task` tags, `value` being the tags that stay), `tag`
    (`update_task` tags, `value` being the tags with the board's added:
    an adopted untagged task), `blocked_by` (`update_task` blockedBy,
    `value` being the whole set) or `state` (`update_task` state,
    `planned` or `wip`)."""

    kind: str
    task_id: str
    value: object


@dataclass(frozen=True)
class RowChange:
    """`insert` (task_id is None when the unit's Create supplies it),
    `update` or `delete` of one row of this board, or `deps`: only the
    row's `deps` and `deps_card` snapshots change."""

    op: str
    block_id: str
    task_id: str | None = None
    title: str = ""
    done: bool = False
    due: str | None = None
    status: str = ROW_OK
    deps: tuple[str, ...] | None = None
    deps_card: tuple[str, ...] | None = None
    state: str | None = None


@dataclass(frozen=True)
class SubRowChange:
    """`insert` (task_id None when the unit's Create supplies it),
    `update` or `delete` of one subtask row of this board."""

    op: str
    block_id: str
    card_block_id: str = ""
    task_id: str | None = None
    parent_block_id: str | None = None
    title: str = ""
    done: bool = False
    due: str | None = None
    status: str = ROW_OK


@dataclass(frozen=True)
class Unit:
    """Everything one pair (or one new card, or one new task) needs, kept
    together so a cap defers all of it or none. The executor makes
    `create` before the board write, sends `ops` in the board's batch,
    makes `calls` after the board write succeeded or was not needed, and
    applies `row` once all of that succeeded.

    `adds_task` is the planner task a new card (or subtask) is added
    for: if a create in the same pass turns out to answer with that very
    task (a create retried by its request id), the executor drops this
    unit. `sub_rows` are applied with `row`."""

    ops: tuple[dict, ...] = ()
    create: Create | None = None
    calls: tuple[Call, ...] = ()
    row: RowChange | None = None
    adds_task: str | None = None
    sub_rows: tuple[SubRowChange, ...] = ()


@dataclass
class Counts:
    """What a pass would do, or did. Numbers only: never a title."""

    cards_added: int = 0
    cards_changed: int = 0
    cards_tagged: int = 0
    cards_removed: int = 0
    tasks_created: int = 0
    tasks_changed: int = 0
    tasks_untagged: int = 0
    paired: int = 0
    conflicts: int = 0
    skipped: int = 0
    waiting: int = 0
    # Removals the mass-loss guard held back (see `merge`).
    held: int = 0
    subs_added: int = 0
    subs_changed: int = 0
    subs_removed: int = 0
    subs_created: int = 0
    subs_detached: int = 0
    deps_changed: int = 0
    # Subtask pairs made (by id or title, or a detached one back on its card).
    subs_paired: int = 0
    # Pairs whose state was carried (a card moved, a task's state changed),
    # and new cards and tasks made in progress (module docstring, "Lanes
    # by role").
    states_changed: int = 0

    def add(self, other: "Counts") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class Plan:
    """`merge`'s result for one board."""

    board: str
    units: list[Unit] = field(default_factory=list)
    counts: Counts = field(default_factory=Counts)

    @property
    def ops(self) -> list[dict]:
        """The board's batch, in the order vaultd must apply it: `tag`
        and `sub_tag` first (they name cards and subtasks by their index
        as read, which only they leave alone), then changes, removals and
        additions (the order units were taken in, within each kind: a
        `sub_add` goes in as its parent's last child), and `move` last, so
        it can name a card added in the same batch."""
        ops = [op for unit in self.units for op in unit.ops]
        return sorted(ops, key=lambda op: _OP_ORDER[op["op"]])

    @property
    def creates(self) -> list[Create]:
        return [unit.create for unit in self.units if unit.create is not None]

    @property
    def calls(self) -> list[Call]:
        return [call for unit in self.units for call in unit.calls]

    @property
    def row_changes(self) -> list[RowChange]:
        return [unit.row for unit in self.units if unit.row is not None]

    @property
    def sub_row_changes(self) -> list[SubRowChange]:
        return [change for unit in self.units for change in unit.sub_rows]


_OP_ORDER = {
    "tag": 0,
    "sub_tag": 1,
    "text": 2,
    "date": 2,
    "check": 2,
    "sub_text": 2,
    "sub_date": 2,
    "sub_check": 2,
    "after_set": 2,
    "sub_remove": 3,
    "remove": 4,
    "sub_add": 5,
    "add": 6,
    "move": 7,
}


# -- screening ---------------------------------------------------------------------


def norm(text: str) -> str:
    """The form both sides are compared in: NFC, stripped. vaultd
    normalizes a card's text the same way when it writes one."""
    return unicodedata.normalize("NFC", text).strip()


def title_key(text: str) -> str:
    """The pairing key: the same title, stripped, case-insensitively."""
    return norm(text).casefold()


def card_text_ok(text: str) -> bool:
    """Whether vaultd would accept `text` as a card's text
    (vaultd/vaultd/boards.py's `_check_text`): one line of at most 500
    characters that reads back as the same text."""
    text = norm(text)
    if not text or len(text) > CARD_MAX_CHARS:
        return False
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in text):
        return False
    return not ("%%" in text or _BLOCK_ID_RE.search(text) or _DATE_RE.search(text))


def task_title_ok(text: str) -> bool:
    """Whether the planner takes `text` as a title."""
    text = norm(text)
    return 0 < len(text) <= TITLE_MAX_CHARS


def _date(value: str | None) -> str | None:
    """`value` when it is a real day as `yyyy-MM-dd`, else None. A planner
    value of another form is not a date the board can show, and a card's
    `@{2026-02-30}` is not one the planner can take."""
    if value is None or not ISO_DATE_RE.match(value):
        return None
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return value


# -- ids ---------------------------------------------------------------------------


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def mint_for_task(board: str, task_id: str) -> str:
    """The block id of the card added for `task_id` on `board`."""
    return _hex(f"task\0{board}\0{task_id}")


def mint_for_card(board: str, text: str, occurrence: int) -> str:
    """The block id given to a card that has none: its `occurrence`-th
    (from 0) among the board's untracked cards with the same text."""
    return _hex(f"card\0{board}\0{norm(text)}\0{occurrence}")


def mint_for_sub(board: str, card_id: str, text: str, occurrence: int) -> str:
    """The block id given to a subtask that has none: its `occurrence`-th
    (from 0) among the card's id-less subtasks with the same text."""
    return _hex(f"sub\0{board}\0{card_id}\0{norm(text)}\0{occurrence}")


def request_id(board: str, block_id: str) -> str:
    """The planner create's `clientRequestId` (at most 100 characters)."""
    return f"echo-board:{_hex('board' + chr(0) + board)}:{block_id}"


class _Ids:
    """The ids on a board, plus the ones this pass mints, so no two cards
    ever share one (vaultd refuses the batch otherwise)."""

    def __init__(self, cards: tuple[Card, ...]) -> None:
        # Subtasks share the board's ids (vaultd checks them together).
        self.taken = {card.id for card in cards if card.id}
        self.taken |= {sub.id for card in cards for sub in card.subs if sub.id}

    def claim(self, wanted: str) -> str:
        candidate, salt = wanted, 0
        while candidate in self.taken:
            salt += 1
            candidate = _hex(f"{wanted}\0{salt}")
        self.taken.add(candidate)
        return candidate


# -- the merge ---------------------------------------------------------------------


_NOTHING = object()


def _three_way(base: object, card: object, task: object) -> tuple[object, object, bool]:
    """(to the card, to the task, conflict), `_NOTHING` meaning no write.
    `base` is `_NOTHING` for a pair with no snapshot yet: the planner wins."""
    if card == task:
        return _NOTHING, _NOTHING, False
    if base is _NOTHING or card == base:
        return task, _NOTHING, False
    if task == base:
        return _NOTHING, card, False
    return task, _NOTHING, True


def planner_state(task: Task) -> str:
    """The task's state. The planner reads it from the task's column (its
    role) and `done` from the completion, which agree but for a task moved
    by hand into or out of the done column: done when either says so (a
    completed task's card belongs in the done lane, as before states)."""
    if task.done or task.state == STATE_DONE:
        return STATE_DONE
    return STATE_WIP if task.state == STATE_WIP else STATE_PLANNED


@dataclass(frozen=True)
class _Lanes:
    """A board's lanes by role (module docstring, "Lanes by role"):
    the done lane's title (None: none), the first other lane (planned)
    and the one after it (wip; None: the board has no middle lane)."""

    done: str | None
    first: str
    wip: str | None

    def role(self, card: Card) -> str:
        if card.checked or (self.done is not None and card.lane == self.done):
            return STATE_DONE
        return STATE_PLANNED if card.lane == self.first else STATE_WIP

    def fit(self, state: str) -> str:
        """What the board can show of `state`: wip with no middle lane is
        shown (and so compared) as planned."""
        return STATE_PLANNED if state == STATE_WIP and self.wip is None else state

    def ops(self, card: Card, block_id: str, target: str) -> list[dict]:
        """The ops that give a card of another role the role `target`
        (fitted)."""
        if target == STATE_DONE:
            return [{"op": "check", "id": block_id, "checked": True}]
        lane = self.first if target == STATE_PLANNED else self.wip
        if self.done is not None:
            # On a board with a done lane the box follows the lane (vaultd's
            # `move`), so one op does it, in place when the lane is right.
            return [{"op": "move", "id": block_id, "lane": lane}]
        out: list[dict] = []
        if card.checked:
            out.append({"op": "check", "id": block_id, "checked": False})
        if (card.lane == self.first) != (target == STATE_PLANNED):
            out.append({"op": "move", "id": block_id, "lane": lane})
        return out


def _lanes_of(board: Board) -> _Lanes | None:
    """The board's lanes by role, or None when they cannot be read: a
    vaultd that did not name the done lane, lane titles that are not
    unique (a card names its lane by title), no lane but the done one."""
    titles = board.lanes
    if not board.lanes_known or len(set(titles)) != len(titles) or "" in titles:
        return None
    if board.done_lane is not None and board.done_lane not in titles:
        return None
    others = [title for title in titles if title != board.done_lane]
    if not others:
        return None
    return _Lanes(board.done_lane, others[0], others[1] if len(others) > 1 else None)


def _state_calls(task_id: str, current: str, target: str) -> list[Call]:
    """The planner calls that take a task from `current` to `target`:
    `complete_task` for done and undone (as before states), `update_task`
    state between planned and wip."""
    if target == STATE_DONE:
        return [Call("done", task_id, True)]
    if current == STATE_DONE:
        undone = [Call("done", task_id, False)]
        return undone + ([Call("state", task_id, STATE_WIP)] if target == STATE_WIP else [])
    return [Call("state", task_id, target)]


@dataclass
class _Fields:
    """The outcome of comparing one pair's three fields."""

    ops: list[dict] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)
    conflicts: int = 0
    skipped: int = 0
    # Skips that hold nothing: a planner wip on a board with no middle
    # lane, counted on the pass that first finds it so.
    noted: int = 0
    # Whether the state was carried, either way.
    moved: bool = False


def _op(kind: str, block_id: str, card_id: str | None, **fields: object) -> dict:
    """A field op on a card, or (`card_id` set) on a subtask of that card."""
    if card_id is None:
        return {"op": kind, "id": block_id, **fields}
    return {"op": f"sub_{kind}", "card": card_id, "id": block_id, **fields}


def _compare(
    board: Board,
    card: Card | Sub,
    task: Task,
    row: Row | SubRow | None,
    block_id: str,
    card_id: str | None = None,
    lanes: _Lanes | None = None,
) -> _Fields:
    """One pair's three fields: a card and its task, or (`card_id` set,
    the card the subtask is in) a subtask and its planner subtask. With
    `lanes` (a card's pair, states on), its state instead of done."""
    out = _Fields()
    base = {
        "title": norm(row.title) if row else _NOTHING,
        "done": row.done if row else _NOTHING,
        "due": row.due if row else _NOTHING,
    }

    # Title.
    c_title, t_title = norm(card.text), norm(task.title)
    to_card, to_task, conflict = _three_way(base["title"], c_title, t_title)
    out.conflicts += conflict
    out.snapshot["title"] = c_title if to_card is _NOTHING and to_task is _NOTHING else None
    if to_card is not _NOTHING:
        if card_text_ok(to_card):
            out.ops.append(_op("text", block_id, card_id, text=to_card))
            out.snapshot["title"] = to_card
        else:
            out.skipped += 1
    if to_task is not _NOTHING:
        if task_title_ok(to_task):
            out.calls.append(Call("title", task.id, to_task))
            out.snapshot["title"] = to_task
        else:
            out.skipped += 1
    if out.snapshot["title"] is None:
        # Not carried: the snapshot keeps its old value, so the next pass
        # sees the same difference and tries again.
        out.snapshot["title"] = row.title if row else c_title

    if lanes is not None and isinstance(card, Card):
        _compare_state(out, lanes, card, task, row, block_id)
    else:
        # Done.
        to_card, to_task, conflict = _three_way(base["done"], card.checked, task.done)
        out.conflicts += conflict
        if to_card is not _NOTHING:
            out.ops.append(_op("check", block_id, card_id, checked=to_card))
        if to_task is not _NOTHING:
            out.calls.append(Call("done", task.id, to_task))
        out.snapshot["done"] = task.done if to_task is _NOTHING else card.checked
        # A state snapshot that no longer agrees with done is dropped (read
        # from done again when states come back).
        old = row.state if isinstance(row, Row) else None
        out.snapshot["state"] = old if old is not None and (old == STATE_DONE) == out.snapshot["done"] else None

    # Due: only on a board whose dates the bot may read and write.
    if not board.dates:
        out.snapshot["due"] = row.due if row else None
        return out
    if card.date is not None and _date(card.date) is None:
        # The card's date is not a real day: neither side moves.
        out.skipped += 1
        out.snapshot["due"] = row.due if row else None
        return out
    t_due = task.due
    to_card, to_task, conflict = _three_way(base["due"], card.date, t_due)
    out.conflicts += conflict
    out.snapshot["due"] = card.date if to_card is _NOTHING and to_task is _NOTHING else None
    settled = to_card is _NOTHING and to_task is _NOTHING
    if to_card is not _NOTHING:
        if to_card is None or _date(to_card) is not None:
            out.ops.append(_op("date", block_id, card_id, date=to_card))
            out.snapshot["due"] = to_card
            settled = True
        else:
            out.skipped += 1
    if to_task is not _NOTHING:
        out.calls.append(Call("due", task.id, to_task))
        out.snapshot["due"] = to_task
        settled = True
    if not settled:
        out.snapshot["due"] = row.due if row else None
    return out


def _compare_state(out: _Fields, lanes: _Lanes, card: Card, task: Task, row: Row | SubRow | None, block_id: str) -> None:
    """A card's pair's state, three ways (module docstring, "Lanes by
    role"), compared as the board can show it; the snapshot keeps the
    planner's own."""
    on_card, in_planner = lanes.role(card), planner_state(task)
    base: object = _NOTHING
    if isinstance(row, Row):
        if row.state in STATES:
            base = row.state
        else:
            # A row from before states: done, or open, which was planned
            # then (module docstring, "Lanes by role").
            base = STATE_DONE if row.done else STATE_PLANNED
    fitted_base = base if base is _NOTHING else lanes.fit(base)
    to_card, to_task, conflict = _three_way(fitted_base, on_card, lanes.fit(in_planner))
    out.conflicts += conflict
    state = in_planner
    if to_card is not _NOTHING:
        out.ops.extend(lanes.ops(card, block_id, to_card))
        out.moved = True
    if to_task is not _NOTHING:
        out.calls.extend(_state_calls(task.id, in_planner, to_task))
        state = to_task
        out.moved = True
    elif lanes.fit(in_planner) != in_planner and base != in_planner:
        # Wip on a board with no middle lane: not shown, counted once.
        out.noted += 1
    out.snapshot["state"] = state
    out.snapshot["done"] = state == STATE_DONE


def _row_for(block_id: str, task_id: str | None, f: _Fields, op: str) -> RowChange:
    return RowChange(
        op=op,
        block_id=block_id,
        task_id=task_id,
        title=f.snapshot["title"],
        done=f.snapshot["done"],
        due=f.snapshot["due"],
        status=ROW_HELD if f.skipped else ROW_OK,
        state=f.snapshot.get("state"),
    )


def _row_unchanged(row: Row, change: RowChange) -> bool:
    return (row.title, row.done, row.due, row.status, row.state) == (
        change.title, change.done, change.due, change.status, change.state,
    )



def _sub_row_for(
    block_id: str, card_id: str, task_id: str | None, parent: str | None, f: _Fields, op: str
) -> SubRowChange:
    return SubRowChange(
        op=op,
        block_id=block_id,
        card_block_id=card_id,
        task_id=task_id,
        parent_block_id=parent,
        title=f.snapshot["title"],
        done=f.snapshot["done"],
        due=f.snapshot["due"],
        status=ROW_HELD if f.skipped else ROW_OK,
    )


def _sub_row_unchanged(row: SubRow, change: SubRowChange) -> bool:
    return (row.title, row.done, row.due, row.status) == (change.title, change.done, change.due, change.status)


def _parents(subs: tuple[Sub, ...]) -> list[int | None]:
    """Each subtask's parent, as an index into `subs` (None: the card)."""
    out: list[int | None] = []
    stack: list[tuple[int, int]] = []
    for k, sub in enumerate(subs):
        while stack and stack[-1][0] >= sub.depth:
            stack.pop()
        out.append(stack[-1][1] if stack else None)
        stack.append((sub.depth, k))
    return out


def descendants(top_ids: set[str], subtasks: list[Task] | tuple[Task, ...]) -> dict[str, list[Task]]:
    """The subtasks under each of `top_ids`, at any depth: {top id: its
    descendants}. A subtask whose chain of parents does not reach one of
    them (or loops) belongs to none."""
    by_id = {task.id: task for task in subtasks}
    out: dict[str, list[Task]] = {top: [] for top in top_ids}
    for task in sorted(subtasks, key=lambda t: t.id):
        seen, parent = {task.id}, task.parent_id
        while parent is not None and parent not in top_ids:
            nxt = by_id.get(parent)
            if nxt is None or nxt.id in seen:
                parent = None
                break
            seen.add(nxt.id)
            parent = nxt.parent_id
        if parent is not None:
            out[parent].append(task)
    return out


@dataclass
class _Candidate:
    kind: str
    unit: Unit
    counts: Counts
    # The card pairs, and the subtask pairs, this unit lets go of, for the
    # guard (`merge`): each kind is weighed against its own pairs.
    loss: int = 0
    sub_loss: int = 0


def _merge_card_subs(
    board: Board,
    card: Card,
    top: Task,
    desc: list[Task],
    rows: list[SubRow],
    *,
    board_sub_ids: set[str],
    board_sub_tasks: set[str],
    top_ids: set[str],
    ids: _Ids,
    counts: Counts,
) -> list[_Candidate]:
    """One paired card's subtasks against its task's subtasks (module
    docstring, "Subtasks"). Waiting, and skips with nothing to do, go
    straight to `counts`."""
    cid = card.id
    assert cid is not None
    subs = card.subs
    parent = _parents(subs)
    index_by_id: dict[str, int] = {}
    for k, sub in enumerate(subs):
        if sub.id and sub.id not in index_by_id:
            index_by_id[sub.id] = k
    desc_by_id = {task.id: task for task in desc}
    out: list[_Candidate] = []

    # 1. Rowed subtasks.
    live: dict[int, tuple[SubRow, Task]] = {}
    removal: set[int] = set()
    rows_at: dict[int, SubRow] = {}
    detached_tasks: set[str] = set()
    for row in sorted(rows, key=lambda r: r.block_id):
        k = index_by_id.get(row.block_id)
        task = desc_by_id.get(row.task_id)
        if row.status == ROW_DETACHED:
            if task is None:
                out.append(_Candidate("sub", Unit(sub_rows=(SubRowChange("delete", row.block_id),)), Counts()))
                continue
            if k is None:
                detached_tasks.add(task.id)
                continue
            # Back on the card it left, its planner subtask still under this
            # card's task: a pair again, compared against its old snapshot
            # (below, as any rowed subtask).
        if k is None and task is None:
            out.append(_Candidate("sub", Unit(sub_rows=(SubRowChange("delete", row.block_id),)), Counts()))
        elif k is None:
            # Gone from the card, kept by the planner: let go of, never deleted.
            detached_tasks.add(task.id)
            change = replace(_snapshot_change(row), status=ROW_DETACHED)
            out.append(_Candidate("sub", Unit(sub_rows=(change,)), Counts(subs_detached=1), sub_loss=1))
        else:
            rows_at[k] = row
            if task is None:
                removal.add(k)
            else:
                live[k] = (row, task)

    # 2. Gone from the planner: the subtask goes, and what is under it.
    doomed: set[int] = set()
    for k in range(len(subs)):
        if k in removal or (parent[k] is not None and parent[k] in doomed):
            doomed.add(k)
    for k in sorted(removal):
        if parent[k] is not None and parent[k] in doomed:
            continue
        under = [k]
        j = k + 1
        while j < len(subs) and subs[j].depth > subs[k].depth:
            under.append(j)
            j += 1
        deletes = tuple(SubRowChange("delete", rows_at[j].block_id) for j in under if j in rows_at)
        unit = Unit(ops=({"op": "sub_remove", "card": cid, "id": subs[k].id},), sub_rows=deletes)
        out.append(_Candidate("sub", unit, Counts(subs_removed=1), sub_loss=len(deletes)))
    for k in doomed:
        live.pop(k, None)

    # 3. Both there: field by field.
    for k in sorted(live):
        row, task = live[k]
        f = _compare(board, subs[k], task, row, row.block_id, card_id=cid)
        change = _sub_row_for(row.block_id, cid, row.task_id, row.parent_block_id, f, "update")
        c = Counts(subs_changed=1 if f.ops or f.calls else 0, conflicts=f.conflicts, skipped=f.skipped)
        c.subs_paired = 1 if row.status == ROW_DETACHED else 0
        if f.ops or f.calls or not _sub_row_unchanged(row, change):
            out.append(_Candidate("sub", Unit(ops=tuple(f.ops), calls=tuple(f.calls), sub_rows=(change,)), c))
        else:
            counts.skipped += c.skipped
            counts.conflicts += c.conflicts

    # 4. What has no row, grouped under its parent's planner task.
    live_task_at = {k: task.id for k, (_row, task) in live.items()}
    block_of_task = {task.id: subs[k].id for k, (_row, task) in live.items()}
    depth_of_task = {top.id: 0, **{task.id: subs[k].depth for k, (_row, task) in live.items()}}
    free_card: dict[str, list[int]] = {}
    for k, sub in enumerate(subs):
        if k in doomed or k in live or k in rows_at:
            continue
        if sub.id is not None:
            if sub.id in board_sub_ids or sub.id in top_ids or index_by_id.get(sub.id) != k:
                continue
            if not ID_RE.match(sub.id):
                # An id vaultd's write ops cannot name: left alone.
                counts.skipped += 1
                continue
        p = parent[k]
        if p is None:
            free_card.setdefault(top.id, []).append(k)
        elif p in live_task_at:
            free_card.setdefault(live_task_at[p], []).append(k)
        elif p not in doomed:
            counts.waiting += 1
    free_task: dict[str, list[Task]] = {}
    for task in sorted(desc, key=lambda t: (t.position, t.id)):
        if task.id in board_sub_tasks:
            continue
        if task.parent_id in depth_of_task:
            free_task.setdefault(task.parent_id, []).append(task)
        elif task.parent_id not in detached_tasks:
            counts.waiting += 1

    # Minted ids for id-less subtasks, by occurrence of text in the card.
    seen: dict[str, int] = {}
    occurrence: dict[int, int] = {}
    for k, sub in enumerate(subs):
        if sub.id is None:
            key = norm(sub.text)
            occurrence[k] = seen.get(key, 0)
            seen[key] = occurrence[k] + 1

    def block_for(k: int) -> str:
        return subs[k].id or ids.claim(mint_for_sub(board.name, cid, subs[k].text, occurrence[k]))

    def tag_op(k: int, block_id: str) -> list[dict]:
        return [{"op": "sub_tag", "card": cid, "n": subs[k].n, "id": block_id}] if subs[k].id is None else []

    count_on_card = len(subs)
    for parent_task in [top.id, *(live_task_at[k] for k in sorted(live_task_at))]:
        cards_here = free_card.get(parent_task, [])
        tasks_here = list(free_task.get(parent_task, []))
        parent_block = block_of_task.get(parent_task)
        pairs: list[tuple[int, Task]] = []
        by_mint = {mint_for_task(board.name, t.id): t for t in tasks_here}
        for k in list(cards_here):
            task = by_mint.get(subs[k].id) if subs[k].id else None
            if task is not None and task in tasks_here:
                pairs.append((k, task))
                cards_here.remove(k)
                tasks_here.remove(task)
        for k in list(cards_here):
            task = next((t for t in tasks_here if title_key(t.title) == title_key(subs[k].text)), None)
            if task is not None:
                pairs.append((k, task))
                cards_here.remove(k)
                tasks_here.remove(task)

        for k, task in sorted(pairs, key=lambda p: p[0]):
            block_id = block_for(k)
            f = _compare(board, subs[k], task, None, block_id, card_id=cid)
            c = Counts(subs_paired=1, subs_changed=1 if f.ops or f.calls else 0, skipped=f.skipped)
            unit = Unit(
                ops=tuple(tag_op(k, block_id) + f.ops),
                calls=tuple(f.calls),
                sub_rows=(_sub_row_for(block_id, cid, task.id, parent_block, f, "insert"),),
            )
            out.append(_Candidate("sub", unit, c))

        # New on the card: a planner subtask each.
        for k in cards_here:
            sub = subs[k]
            if not task_title_ok(sub.text):
                counts.skipped += 1
                continue
            block_id = block_for(k)
            title = norm(sub.text)
            due = _date(sub.date) if board.dates else None
            held = board.dates and sub.date is not None and due is None
            create = Create(
                block_id=block_id,
                title=title,
                due=due,
                done=sub.checked,
                tag=None,
                request_id=request_id(board.name, block_id),
                parent_task_id=parent_task,
            )
            row = SubRowChange(
                "insert", block_id, cid, None, parent_block, title, sub.checked, due, ROW_HELD if held else ROW_OK
            )
            c = Counts(subs_created=1, skipped=1 if held else 0)
            out.append(_Candidate("sub_create", Unit(ops=tuple(tag_op(k, block_id)), create=create, sub_rows=(row,)), c))

        # New in the planner: a subtask each, as the parent's last child.
        for task in tasks_here:
            title = norm(task.title)
            if (
                depth_of_task[parent_task] + 1 > SUB_MAX_DEPTH
                or not card_text_ok(title)
                or count_on_card >= SUBS_PER_CARD_MAX
            ):
                counts.skipped += 1
                continue
            block_id = ids.claim(mint_for_task(board.name, task.id))
            due = _date(task.due) if board.dates else None
            op: dict = {"op": "sub_add", "card": cid, "id": block_id, "text": title, "checked": task.done}
            if parent_block is not None:
                op["parent"] = parent_block
            if due is not None:
                op["date"] = due
            held = board.dates and task.due is not None and due is None
            row = SubRowChange(
                "insert", block_id, cid, task.id, parent_block, title, task.done, due, ROW_HELD if held else ROW_OK
            )
            count_on_card += 1
            c = Counts(subs_added=1, skipped=1 if held else 0)
            out.append(_Candidate("sub_add", Unit(ops=(op,), sub_rows=(row,), adds_task=task.id), c))
    return out


def _snapshot_change(row: SubRow) -> SubRowChange:
    return SubRowChange(
        "update", row.block_id, row.card_block_id, row.task_id, row.parent_block_id,
        row.title, row.done, row.due, row.status,
    )


def merge(
    board: Board,
    tasks: list[Task],
    rows: list[Row],
    *,
    tag: str,
    creates_max: int = CREATES_MAX_DEFAULT,
    guard: bool = True,
    untagged: list[Task] | tuple[Task, ...] = (),
    subtasks: list[Task] | tuple[Task, ...] = (),
    sub_rows: list[SubRow] | tuple[SubRow, ...] = (),
    states: bool = False,
) -> Plan:
    """What one pass does for one board (module docstring). `states` is
    whether the planner's rows carry a state (module docstring, "Lanes by
    role"; off: done/open only, as before). `untagged`
    are the user's own open top-level tasks with no tag, the ones a free
    card may adopt by title; any of them with a tag, done, or already in
    a row is ignored. `subtasks` are the planner's subtasks under `tasks`
    (any depth; others are ignored), `sub_rows` the board's subtask rows.

    **The mass-loss guard.** A pair whose card or task is gone means a
    removal on the other side, and one side reading as empty by mistake
    (a planner answer cut short, a board the user is half way through
    reorganising, a tag renamed) would turn that into every card removed
    or every task untagged. So when one pass would drop more than
    `MASS_LOSS_MIN` pairs and more than half of the board's pairs, none
    of those removals is made: the pairs stay as they are and the count
    is reported as `held`, pass after pass, until the two sides agree
    again or the user runs `/boards now`, which passes `guard=False`
    because a person asked for exactly this board to be brought in step.
    Card pairs and subtask pairs are weighed apart, each against its own
    kind with the same thresholds: a card pair lost (untag, remove) is one
    card loss, its subtask rows going with it count as nothing more; a
    subtask removed from the card (with the rows under it) or let go of
    (`detached`) is a subtask loss, over the subtask pairs. Either
    triggering holds its own kind of losses (a held card keeps its
    subtask rows too), so subtask rows never dilute the cards' threshold
    nor the cards the subtasks'."""
    plan = Plan(board=board.name)
    counts = plan.counts
    ids = _Ids(board.cards)
    lanes = _lanes_of(board) if states else None

    rows = sorted(rows, key=lambda r: r.block_id)
    row_ids = {row.block_id for row in rows}
    row_tasks = {row.task_id for row in rows}
    # A block id on two cards (a copied line) names the first: the plugin
    # never does this, and vaultd's own lookup takes the first too.
    cards_by_id: dict[str, Card] = {}
    for card in board.cards:
        if card.id and card.id not in cards_by_id:
            cards_by_id[card.id] = card
    tasks_by_id = {task.id: task for task in tasks}
    subs_by_card: dict[str, list[SubRow]] = {}
    for sub_row in sub_rows:
        subs_by_card.setdefault(sub_row.card_block_id, []).append(sub_row)

    def drop_subs(block_id: str) -> tuple[SubRowChange, ...]:
        return tuple(SubRowChange("delete", r.block_id) for r in sorted(subs_by_card.get(block_id, []), key=lambda r: r.block_id))

    # Units in a fixed order, cut to the caps afterwards; the kind is for
    # reading only.
    candidates: list[_Candidate] = []
    # Pairs that keep both sides this pass: their subtasks are merged.
    kept_pairs: list[tuple[Card, Task, Row]] = []

    # 1. Existing pairs.
    for row in rows:
        card = cards_by_id.get(row.block_id)
        task = tasks_by_id.get(row.task_id)
        c = Counts()
        if card is None and task is None:
            unit = Unit(row=RowChange("delete", row.block_id), sub_rows=drop_subs(row.block_id))
            candidates.append(_Candidate("pair", unit, c))
            continue
        if card is None:
            # Gone from the board: the task keeps everything but the tag.
            stay = tuple(t for t in task.tags if t != tag)
            c.tasks_untagged = 1
            unit = Unit(
                calls=(Call("untag", task.id, stay),),
                row=RowChange("delete", row.block_id),
                sub_rows=drop_subs(row.block_id),
            )
            candidates.append(_Candidate("pair", unit, c, loss=1))
            continue
        if not ID_RE.match(card.id or ""):
            # A block id the user wrote in a form vaultd's write ops cannot
            # name (`^Plan-1`): any op on it would make vaultd refuse the
            # whole batch, so the pair is left as it is.
            counts.skipped += 1
            continue
        if task is None:
            # Gone from the tagged list: the card goes.
            c.cards_removed = 1
            unit = Unit(
                ops=({"op": "remove", "id": row.block_id},),
                row=RowChange("delete", row.block_id),
                sub_rows=drop_subs(row.block_id),
            )
            candidates.append(_Candidate("pair", unit, c, loss=1))
            continue
        kept_pairs.append((card, task, row))
        f = _compare(board, card, task, row, row.block_id, lanes=lanes)
        change = _row_for(row.block_id, row.task_id, f, "update")
        c.cards_changed = 1 if f.ops else 0
        c.tasks_changed = 1 if f.calls else 0
        c.states_changed = int(f.moved)
        c.conflicts, c.skipped = f.conflicts, f.skipped + f.noted
        if f.ops or f.calls or not _row_unchanged(row, change):
            candidates.append(_Candidate("pair", Unit(ops=tuple(f.ops), calls=tuple(f.calls), row=change), c))
        else:
            # Held as before: nothing to do, but still a skip (and, when
            # both sides moved, a conflict) this pass.
            counts.skipped += c.skipped
            counts.conflicts += c.conflicts
    # Subtask rows of a card no row knows (any more): nothing to follow.
    for card_id in sorted(set(subs_by_card) - row_ids):
        candidates.append(_Candidate("sub", Unit(sub_rows=drop_subs(card_id)), Counts()))

    # 2. Pairing what has no row.
    free_cards = [card for card in board.cards if card.id is None or (card.id not in row_ids and cards_by_id.get(card.id) is card)]
    # A card whose user-written block id vaultd's ops cannot name is never
    # paired, created for or written (see step 1); it only keeps a task of
    # its title from becoming a second card (below).
    odd_cards = [card for card in free_cards if card.id is not None and not ID_RE.match(card.id)]
    free_cards = [card for card in free_cards if card.id is None or ID_RE.match(card.id)]
    free_tasks = sorted((t for t in tasks if t.id not in row_tasks), key=lambda t: t.id)
    pairs: list[tuple[Card, Task]] = []
    by_mint = {mint_for_task(board.name, t.id): t for t in free_tasks}
    for card in list(free_cards):
        task = by_mint.get(card.id) if card.id else None
        if task is not None:
            pairs.append((card, task))
            free_cards.remove(card)
            free_tasks.remove(task)
    tasks_by_key: dict[str, list[Task]] = {}
    for task in free_tasks:
        tasks_by_key.setdefault(title_key(task.title), []).append(task)
    for card in list(free_cards):
        bucket = tasks_by_key.get(title_key(card.text))
        if bucket:
            task = bucket.pop(0)
            pairs.append((card, task))
            free_cards.remove(card)
            free_tasks.remove(task)

    # Adoption: a card still free takes an untagged task of the same title.
    adopted: set[str] = set()
    untagged_by_key: dict[str, list[Task]] = {}
    for task in sorted(untagged, key=lambda t: t.id):
        if task.tags or task.done or task.id in row_tasks or task.id in tasks_by_id:
            continue
        untagged_by_key.setdefault(title_key(task.title), []).append(task)
    for card in list(free_cards):
        bucket = untagged_by_key.get(title_key(card.text))
        if bucket:
            task = bucket.pop(0)
            pairs.append((card, task))
            adopted.add(task.id)
            free_cards.remove(card)
    for card in odd_cards:
        counts.skipped += 1
        bucket = tasks_by_key.get(title_key(card.text))
        if bucket:
            free_tasks.remove(bucket.pop(0))

    # Ids for the untracked cards that will need one, by occurrence of text.
    seen: dict[str, int] = {}
    minted: dict[int, str] = {}
    for card in board.cards:
        if card.id is None:
            key = norm(card.text)
            k = seen.get(key, 0)
            seen[key] = k + 1
            minted[card.n] = ids.claim(mint_for_card(board.name, card.text, k))

    for card, task in sorted(pairs, key=lambda p: p[0].n):
        block_id = card.id or minted[card.n]
        f = _compare(board, card, task, None, block_id, lanes=lanes)
        ops = ([{"op": "tag", "n": card.n, "id": block_id}] if card.id is None else []) + f.ops
        calls = f.calls
        if task.id in adopted:
            calls = [Call("tag", task.id, (*task.tags, tag)), *calls]
        c = Counts(paired=1, cards_tagged=1 if card.id is None else 0)
        c.cards_changed = 1 if f.ops else 0
        c.tasks_changed = 1 if calls else 0
        c.states_changed = int(f.moved)
        c.skipped = f.skipped + f.noted
        unit = Unit(ops=tuple(ops), calls=tuple(calls), row=_row_for(block_id, task.id, f, "insert"))
        candidates.append(_Candidate("pair", unit, c))

    # 3. New cards: a planner task each.
    for card in free_cards:
        if not task_title_ok(card.text):
            counts.skipped += 1
            continue
        block_id = card.id or minted[card.n]
        title = norm(card.text)
        due = _date(card.date) if board.dates else None
        held = board.dates and card.date is not None and due is None
        # The task is made in the card's state: done as before, wip with a
        # state call after the create.
        state = lanes.role(card) if lanes is not None else None
        done = state == STATE_DONE if state is not None else card.checked
        create = Create(
            block_id=block_id,
            title=title,
            due=due,
            done=done,
            tag=tag,
            request_id=request_id(board.name, block_id),
            state=STATE_WIP if state == STATE_WIP else None,
        )
        ops = ({"op": "tag", "n": card.n, "id": block_id},) if card.id is None else ()
        row = RowChange("insert", block_id, None, title, done, due, ROW_HELD if held else ROW_OK, state=state)
        c = Counts(tasks_created=1, cards_tagged=1 if card.id is None else 0, skipped=1 if held else 0)
        c.states_changed = int(state == STATE_WIP)
        candidates.append(_Candidate("create", Unit(ops=ops, create=create, row=row), c))

    # 4. New tasks: a card each (open ones only, module docstring).
    for task in free_tasks:
        if task.done:
            continue
        title = norm(task.title)
        if not card_text_ok(title):
            counts.skipped += 1
            continue
        block_id = ids.claim(mint_for_task(board.name, task.id))
        due = _date(task.due) if board.dates else None
        op: dict = {"op": "add", "id": block_id, "text": title}
        if due is not None:
            op["date"] = due
        ops = [op]
        c = Counts(cards_added=1)
        # A planner due date that is not a date the board can show stays
        # off the card; the pair is held on it, as `_compare` would hold
        # it on every later pass.
        held = board.dates and task.due is not None and due is None
        c.skipped = 1 if held else 0
        state = planner_state(task) if lanes is not None else None
        if state == STATE_WIP:
            # Added to the first lane, then moved to the middle one; with
            # none, it stays in the first lane, counted once (`_compare_state`).
            if lanes.wip is not None:
                ops.append({"op": "move", "id": block_id, "lane": lanes.wip})
                c.states_changed = 1
            else:
                c.skipped += 1
        row = RowChange("insert", block_id, task.id, title, False, due, ROW_HELD if held else ROW_OK, state=state)
        candidates.append(_Candidate("add", Unit(ops=tuple(ops), row=row, adds_task=task.id), c))

    # 5. Subtasks of the pairs that had a row when the pass began.
    if board.subs_known:
        tree = descendants({task.id for _card, task, _row in kept_pairs}, subtasks)
        # A detached subtask whose block id now sits on another card (cut
        # and pasted there): its row gives the id up, so that card's
        # subtask is paired or created as any free one, and the planner
        # subtask stays detached under an id no card carries, so it is not
        # added back to the card it left.
        home: dict[str, str] = {}
        for card in board.cards:
            for sub in card.subs:
                if card.id and sub.id:
                    home.setdefault(sub.id, card.id)
        kept_ids = {card.id for card, _task, _row in kept_pairs}
        planner_subs = {task.id for task in subtasks}
        moved: set[str] = set()
        for sub_row in sorted(sub_rows, key=lambda r: r.block_id):
            where = home.get(sub_row.block_id)
            if (
                sub_row.status != ROW_DETACHED
                or sub_row.card_block_id not in kept_ids
                or where is None
                or where == sub_row.card_block_id
            ):
                continue
            moved.add(sub_row.block_id)
            changes = [SubRowChange("delete", sub_row.block_id)]
            if sub_row.task_id in planner_subs:
                rekeyed = replace(sub_row, block_id=ids.claim(_hex(f"detached\0{sub_row.block_id}")))
                changes.append(replace(_snapshot_change(rekeyed), op="insert"))
                subs_by_card[sub_row.card_block_id].append(rekeyed)
            subs_by_card[sub_row.card_block_id] = [r for r in subs_by_card[sub_row.card_block_id] if r.block_id != sub_row.block_id]
            candidates.append(_Candidate("sub", Unit(sub_rows=tuple(changes)), Counts()))
        board_sub_ids = {r.block_id for r in sub_rows} - moved
        board_sub_tasks = {r.task_id for r in sub_rows}
        for card, task, _row in kept_pairs:
            if not ID_RE.match(card.id or ""):
                # vaultd's subtask ops name the card by an id of its own form.
                continue
            candidates.extend(
                _merge_card_subs(
                    board,
                    card,
                    task,
                    tree[task.id],
                    subs_by_card.get(card.id, []),
                    board_sub_ids=board_sub_ids,
                    board_sub_tasks=board_sub_tasks,
                    top_ids=row_ids,
                    ids=ids,
                    counts=counts,
                )
            )

    # The guard, card pairs and subtask pairs each against their own kind
    # (a card's subtask rows go with it and count as no loss of their own).
    lost = sum(cand.loss for cand in candidates)
    sub_lost = sum(cand.sub_loss for cand in candidates)
    sub_pairs = sum(1 for r in sub_rows if r.status != ROW_DETACHED)
    hold = guard and lost > MASS_LOSS_MIN and lost * 2 > len(rows)
    sub_hold = guard and sub_lost > MASS_LOSS_MIN and sub_lost * 2 > sub_pairs
    held_units = {
        id(cand.unit) for cand in candidates if (hold and cand.loss) or (sub_hold and cand.sub_loss)
    }
    counts.held = len(held_units)

    # Caps: vaultd's batch size, the planner's creates, the board's cards.
    used_ops = creates = 0
    cards = len(board.cards)
    for cand in candidates:
        unit = cand.unit
        if id(unit) in held_units:
            continue
        removes = sum(1 for op in unit.ops if op["op"] == "remove")
        adds = sum(1 for op in unit.ops if op["op"] == "add")
        if (
            used_ops + len(unit.ops) > OPS_MAX
            or (unit.create is not None and creates >= creates_max)
            or cards - removes + adds > BOARD_MAX_CARDS
        ):
            counts.waiting += 1
            continue
        used_ops += len(unit.ops)
        creates += unit.create is not None
        cards += adds - removes
        plan.units.append(unit)
        counts.add(cand.counts)
    return plan


# -- dependencies --------------------------------------------------------------------


@dataclass(frozen=True)
class DepsSide:
    """One board as `merge_deps` reads it: the board as read, its tagged
    top-level tasks, and its rows at the start of the pass."""

    board: Board
    tasks: tuple[Task, ...]
    rows: tuple[Row, ...]
    sub_rows: tuple[SubRow, ...] = ()


@dataclass
class DepsPlan:
    """`merge_deps`' result for one board: its units (an `after_set`, a
    `blocked_by` call and a `deps` row change each, the lowest priority
    of the pass: `add_units` takes what fits) and the skips and
    conflicts of the pairs with nothing to do."""

    units: list[tuple[Unit, Counts]] = field(default_factory=list)
    counts: Counts = field(default_factory=Counts)


def _label(text: str) -> str | None:
    """A link label vaultd takes (a card text with no `[`, `]`, `|`)."""
    label = norm(re.sub(r"[\[\]|]", "", text))
    return label if card_text_ok(label) else None


def merge_deps(sides: list[DepsSide], every_task: list[Task] | tuple[Task, ...]) -> dict[str, DepsPlan]:
    """Every paired card's `after:` line against its task's `blockedBy`
    (module docstring, "Dependencies"). `sides` are the boards read this
    pass (the only ones a link may be written to), `every_task` all the
    planner tasks read, subtasks included (a blocker's title and a
    task's blockers)."""
    names = {side.board.name for side in sides}
    tasks_by_id = {task.id: task for task in every_task}
    # (board, block id) <-> task id, from every board's rows.
    to_task: dict[tuple[str, str], str] = {}
    for side in sides:
        for row in side.rows:
            to_task[(side.board.name, row.block_id)] = row.task_id
        for sub_row in side.sub_rows:
            if sub_row.status != ROW_DETACHED:
                to_task[(side.board.name, sub_row.block_id)] = sub_row.task_id
    blocks_of: dict[str, list[tuple[str, str]]] = {}
    for block, task_id in sorted(to_task.items()):
        blocks_of.setdefault(task_id, []).append(block)
    mapped = set(blocks_of)
    texts: dict[tuple[str, str], str] = {}
    for side in sides:
        for card in side.board.cards:
            if card.id:
                texts.setdefault((side.board.name, card.id), card.text)
            for sub in card.subs:
                if sub.id:
                    texts.setdefault((side.board.name, sub.id), sub.text)
    # A plain title on an `after:` line names one of the paired top-level
    # cards (a row, a block id vaultd takes) of the boards read this pass.
    by_title: dict[str, list[tuple[str, str]]] = {}
    for side in sides:
        rowed = {row.block_id for row in side.rows}
        for card in side.board.cards:
            block = (side.board.name, card.id or "")
            if card.id in rowed and ID_RE.match(block[1]) and block not in by_title.get(title_key(card.text), ()):
                by_title.setdefault(title_key(card.text), []).append(block)

    def by_its_title(title: str, own: tuple[str, str]) -> tuple[str, str] | None:
        # The one card of that title on the card's own board, else the one
        # on any board; none or several: unresolved. Never the card itself.
        found = [b for b in by_title.get(title_key(title), ()) if b != own]
        here = [b for b in found if b[0] == own[0]]
        if len(here) == 1:
            return here[0]
        return found[0] if len(found) == 1 else None

    out: dict[str, DepsPlan] = {}
    for side in sorted(sides, key=lambda s: s.board.name):
        name = side.board.name
        plan = out.setdefault(name, DepsPlan())
        if not side.board.subs_known:
            continue
        cards_by_id: dict[str, Card] = {}
        for card in side.board.cards:
            if card.id and card.id not in cards_by_id:
                cards_by_id[card.id] = card
        own = {task.id: task for task in side.tasks}
        for row in sorted(side.rows, key=lambda r: r.block_id):
            card, task = cards_by_id.get(row.block_id), own.get(row.task_id)
            if card is None or task is None:
                continue
            linked: dict[str, tuple[str, str]] = {}
            unresolved: list[tuple[str, str]] = []
            for link in card.after:
                target = to_task.get(link)
                if target is None:
                    if link not in unresolved:
                        unresolved.append(link)
                elif target != task.id and target not in linked:
                    linked[target] = link
            titles_open = False
            for title in card.after_titles:
                block = by_its_title(title, (name, row.block_id))
                if block is None:
                    titles_open = True
                elif to_task[block] != task.id and to_task[block] not in linked:
                    linked[to_task[block]] = block
            # Titles all found: the line is rewritten as links even when the
            # set is the same. One not found: the line is not written.
            canonical = bool(card.after_titles) and not titles_open
            on_card = frozenset(linked)
            in_planner = frozenset(t for t in task.blocked_by if t in mapped and t != task.id)
            base = None if row.deps is None else frozenset(t for t in row.deps if t in mapped)
            card_base = None if row.deps_card is None else frozenset(t for t in row.deps_card if t in mapped)
            conflict = False
            if base is None:
                new = on_card | in_planner
            elif on_card == in_planner:
                new = on_card
            elif card_base is not None:
                # The line was not written last time: its own changes are
                # read against what it showed then, the planner's against
                # `deps`; the planner wins when both moved.
                card_moved, planner_moved = on_card != card_base, in_planner != base
                if card_moved and planner_moved:
                    new, conflict = in_planner, True
                elif card_moved:
                    new = (in_planner | (on_card - card_base)) - (card_base - on_card)
                else:
                    new = in_planner
            elif on_card == base:
                new = in_planner
            elif in_planner == base:
                new = on_card
            else:
                new, conflict = in_planner, True

            def carried(ids: frozenset[str], previous: tuple[str, ...] | None) -> tuple[str, ...]:
                # Only ids this pass could compare are replaced: a blocker on
                # a board not read this pass keeps its place in the snapshot,
                # so its absence is never read as a removal later.
                kept = {t for t in previous or () if t not in mapped and t in tasks_by_id}
                return tuple(sorted(ids | kept))

            ops: list[dict] = []
            calls: list[Call] = []
            skipped = 0
            if new != in_planner:
                keep = [t for t in task.blocked_by if (t not in mapped or t in new) and t != task.id]
                keep += sorted(new - set(keep))
                if len(keep) > BLOCKED_BY_MAX:
                    # More than the planner takes: nothing is written on
                    # either side, the snapshot stays, the pass retries.
                    plan.counts.add(Counts(conflicts=int(conflict), skipped=1))
                    continue
                calls.append(Call("blocked_by", task.id, tuple(keep)))
            card_written = new == on_card
            if new != on_card or canonical:
                links = _after_links(name, task.id, new, linked, unresolved, blocks_of, tasks_by_id, texts, names)
                if card.after_raw_extra or titles_open or links is None or not ID_RE.match(row.block_id):
                    skipped = 1
                else:
                    ops.append({"op": "after_set", "id": row.block_id, "links": links})
                    card_written = True
            if titles_open:
                skipped = 1
            snapshot = carried(new, row.deps)
            card_snapshot = None if card_written else carried(
                on_card, row.deps_card if row.deps_card is not None else row.deps
            )
            c = Counts(deps_changed=1 if ops or calls else 0, conflicts=int(conflict), skipped=skipped)
            if ops or calls or snapshot != row.deps or card_snapshot != row.deps_card:
                change = RowChange("deps", row.block_id, deps=snapshot, deps_card=card_snapshot)
                plan.units.append((Unit(ops=tuple(ops), calls=tuple(calls), row=change), c))
            else:
                plan.counts.add(c)
    return out


def _after_links(
    name: str,
    own_task: str,
    new: frozenset[str],
    linked: dict[str, tuple[str, str]],
    unresolved: list[tuple[str, str]],
    blocks_of: dict[str, list[tuple[str, str]]],
    tasks_by_id: dict[str, Task],
    texts: dict[tuple[str, str], str],
    names: set[str],
) -> list[dict] | None:
    """The `after_set` links for `new`: the links already there kept
    pointing where they did, new ones at the blocker's own block (this
    board's first), each labelled with the blocker's planner title (else
    its block's text); then the links no row resolves, unchanged. None
    when a link cannot be written."""
    out: list[dict] = []
    for task_id in [t for t in linked if t in new] + sorted(new - set(linked)):
        if task_id in linked:
            block = linked[task_id]
        else:
            choices = blocks_of[task_id]
            block = next((b for b in choices if b[0] == name), choices[0])
        task = tasks_by_id.get(task_id)
        label = (_label(task.title) if task is not None else None) or _label(texts.get(block, ""))
        if label is None:
            return None
        out.append({"board": block[0], "id": block[1], "label": label})
    for block in unresolved:
        label = _label(texts.get(block, "")) or block[1]
        out.append({"board": block[0], "id": block[1], "label": label})
    if len(out) > AFTER_LINKS_MAX:
        return None
    for link in out:
        if link["board"] not in names or not ID_RE.match(link["id"]):
            return None
    return out


def add_units(plan: Plan, deps: DepsPlan | None) -> None:
    """`deps`' units appended to `plan` as far as the board's batch has
    room (what does not fit waits), and its counts added."""
    if deps is None:
        return
    used = len(plan.ops)
    for unit, c in deps.units:
        if used + len(unit.ops) > OPS_MAX:
            plan.counts.waiting += 1
            continue
        used += len(unit.ops)
        plan.units.append(unit)
        plan.counts.add(c)
    plan.counts.add(deps.counts)


# -- a model of both sides, for the tests ---------------------------------------------


def _apply_ops(board: Board, ops: list[dict]) -> Board:
    """vaultd's ops on the board (vaultd/vaultd/boards.py). On a board
    with lanes, as vaultd places cards: a card goes to the end of its new
    lane, checking moves it into the done lane, unchecking out of it to
    the first other lane, and `move` makes the box follow the lane when
    the board has a done lane."""
    cards = list(board.cards)
    order = {title: k for k, title in enumerate(board.lanes)}
    first_open = next((t for t in board.lanes if t != board.done_lane), "")

    def at(card_id: str) -> int:
        return next(i for i, card in enumerate(cards) if card.id == card_id)

    def to_lane_end(i: int, card: Card) -> None:
        del cards[i]
        where = order.get(card.lane, 0)
        end = max((k + 1 for k, other in enumerate(cards) if order.get(other.lane, 0) <= where), default=0)
        cards.insert(end, card)

    def place(i: int, card: Card) -> None:
        """`card` (cards[i] changed) at the end of its lane, if it moved."""
        if board.lanes and card.lane != cards[i].lane:
            to_lane_end(i, card)
        else:
            cards[i] = card

    for op in ops:
        kind = op["op"]
        if kind == "tag":
            cards[op["n"]] = replace(cards[op["n"]], id=op["id"])
            continue
        if kind == "add":
            checked = bool(op.get("checked", False))
            lane = (board.done_lane if checked and board.done_lane else first_open) if board.lanes else ""
            cards.append(Card(n=len(cards), id=op["id"], text=op["text"], checked=checked, date=op.get("date"), lane=lane))
            if board.lanes:
                to_lane_end(len(cards) - 1, cards[-1])
            continue
        if kind == "move":
            index = at(op["id"])
            card = replace(cards[index], lane=op["lane"])
            if board.done_lane is not None:
                card = replace(card, checked=op["lane"] == board.done_lane)
            place(index, card)
            continue
        if kind.startswith("sub_"):
            i = at(op["card"])
            subs = list(cards[i].subs)
            if kind == "sub_tag":
                subs[op["n"]] = replace(subs[op["n"]], id=op["id"])
            elif kind == "sub_add":
                if "parent" in op:
                    p = next(k for k, sub in enumerate(subs) if sub.id == op["parent"])
                    depth, end = subs[p].depth + 1, p + 1
                    while end < len(subs) and subs[end].depth > subs[p].depth:
                        end += 1
                else:
                    depth, end = 1, len(subs)
                subs.insert(end, Sub(n=0, id=op["id"], depth=depth, text=op["text"], checked=op["checked"], date=op.get("date")))
            else:
                k = next(k for k, sub in enumerate(subs) if sub.id == op["id"])
                if kind == "sub_text":
                    subs[k] = replace(subs[k], text=op["text"])
                elif kind == "sub_check":
                    subs[k] = replace(subs[k], checked=op["checked"])
                elif kind == "sub_date":
                    subs[k] = replace(subs[k], date=op["date"])
                elif kind == "sub_remove":
                    end = k + 1
                    while end < len(subs) and subs[end].depth > subs[k].depth:
                        end += 1
                    del subs[k:end]
            subs = [replace(sub, n=k) for k, sub in enumerate(subs)]
            cards[i] = replace(cards[i], subs=tuple(subs))
            continue
        index = at(op["id"])
        if kind == "text":
            cards[index] = replace(cards[index], text=op["text"])
        elif kind == "date":
            cards[index] = replace(cards[index], date=op["date"])
        elif kind == "check":
            card = replace(cards[index], checked=op["checked"])
            if board.done_lane is not None and op["checked"]:
                card = replace(card, lane=board.done_lane)
            elif board.done_lane is not None and card.lane == board.done_lane:
                card = replace(card, lane=first_open)
            place(index, card)
        elif kind == "after_set":
            cards[index] = replace(
                cards[index],
                after=tuple((link["board"], link["id"]) for link in op["links"]),
                after_raw_extra=False,
                after_titles=(),
            )
        elif kind == "remove":
            del cards[index]
    return replace(board, cards=tuple(replace(card, n=i) for i, card in enumerate(cards)))


def apply_state(
    board: Board,
    planner: dict[str, Task],
    rows: list[Row],
    sub_rows: list[SubRow],
    plan: Plan,
    *,
    created_ids: dict[str, str] | None = None,
    states: bool = False,
) -> tuple[Board, dict[str, Task], list[Row], list[SubRow]]:
    """The state after `plan` ran in full: the board with its batch
    applied, the planner (every task, by id) with its creates and calls
    made, the rows and subtask rows with their changes. A model of what
    vaultd and the planner do, for the tests' idempotence checks and for
    nothing else; `created_ids` maps a create's block id to the task id
    the planner answered (default: `new-<block id>`); with `states`, a
    planner whose tasks carry a state."""
    created_ids = created_ids if created_ids is not None else {}
    board = _apply_ops(board, plan.ops)
    planner = dict(planner)
    for create in plan.creates:
        task_id = created_ids.setdefault(create.block_id, f"new-{create.block_id}")
        if create.parent_task_id is None:
            state = None
            if states:
                state = STATE_DONE if create.done else (create.state or STATE_PLANNED)
            planner[task_id] = Task(task_id, create.title, create.done, create.due, (create.tag,), state=state)
        else:
            siblings = [t.position for t in planner.values() if t.parent_id == create.parent_task_id]
            planner[task_id] = Task(
                task_id, create.title, create.done, create.due, (),
                parent_id=create.parent_task_id, position=max(siblings, default=0) + 1,
            )
    for call in plan.calls:
        task = planner[call.task_id]
        if call.kind == "title":
            planner[call.task_id] = replace(task, title=call.value)
        elif call.kind == "due":
            planner[call.task_id] = replace(task, due=call.value)
        elif call.kind == "done":
            # The planner's state follows done (a planner without states
            # keeps none).
            state = task.state
            if state is not None:
                state = STATE_DONE if call.value else STATE_PLANNED
            planner[call.task_id] = replace(task, done=call.value, state=state)
        elif call.kind == "state":
            planner[call.task_id] = replace(task, state=call.value, done=call.value == STATE_DONE)
        elif call.kind in ("untag", "tag"):
            planner[call.task_id] = replace(task, tags=tuple(call.value))
        elif call.kind == "blocked_by":
            planner[call.task_id] = replace(task, blocked_by=tuple(call.value))

    by_block = {row.block_id: row for row in rows}
    for change in plan.row_changes:
        if change.op == "delete":
            by_block.pop(change.block_id, None)
            continue
        if change.op == "deps":
            by_block[change.block_id] = replace(by_block[change.block_id], deps=change.deps, deps_card=change.deps_card)
            continue
        old = by_block.get(change.block_id)
        task_id = change.task_id if change.task_id is not None else created_ids[change.block_id]
        keep = old is not None and change.op == "update"
        by_block[change.block_id] = Row(
            change.block_id, task_id, change.title, change.done, change.due, change.status,
            old.deps if keep else None, old.deps_card if keep else None, change.state,
        )
    subs_by_block = {row.block_id: row for row in sub_rows}
    for change in plan.sub_row_changes:
        if change.op == "delete":
            subs_by_block.pop(change.block_id, None)
            continue
        task_id = change.task_id if change.task_id is not None else created_ids[change.block_id]
        subs_by_block[change.block_id] = SubRow(
            change.block_id, change.card_block_id, task_id, change.parent_block_id,
            change.title, change.done, change.due, change.status,
        )
    return (
        board,
        planner,
        sorted(by_block.values(), key=lambda r: r.block_id),
        sorted(subs_by_block.values(), key=lambda r: r.block_id),
    )


def view(planner: dict[str, Task], tag: str) -> tuple[list[Task], list[Task]]:
    """The planner as one board's merge reads it: its tagged top-level
    tasks, and every subtask (the merge keeps those under them)."""
    tops = [t for t in planner.values() if t.parent_id is None and tag in t.tags]
    subs = [t for t in planner.values() if t.parent_id is not None]
    return tops, subs


def apply_to_state(
    board: Board,
    tasks: list[Task],
    rows: list[Row],
    plan: Plan,
    *,
    tag: str,
    created_ids: dict[str, str] | None = None,
    untagged: list[Task] | tuple[Task, ...] = (),
    states: bool = False,
) -> tuple[Board, list[Task], list[Row]]:
    """`apply_state` for a board with no subtasks: the board, its tagged
    tasks and its rows after `plan`."""
    planner = {task.id: task for task in (*untagged, *tasks)}
    board, planner, rows, _subs = apply_state(board, planner, rows, [], plan, created_ids=created_ids, states=states)
    return board, [task for task in planner.values() if tag in task.tags], rows
