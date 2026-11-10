"""Task boards: the user's own Kanban Starlane boards, read as cards and
changed card by card.

The user keeps tasks on kanban boards at the root of the vault (`Work
tasks.md`, `General tasks.md`) and in a separate planner; the bot keeps
the two in step. This module is the vault's half: it reads a board into
a list of cards, and applies a short list of card operations to it.

It is a third kind of access, beside the note classes and Echo's own
notes, and the first that touches a file the user wrote. What keeps it
narrow:

1. **Only boards the user named.** `Anchor/settings.md` lists them
   (`task_boards`, bare file names at the vault's root, at most
   TASK_BOARDS_MAX). No list, no boards. A name that is not a Starlane
   board (`kanban-starlane:` in its frontmatter) is reported as
   `not_board` and never read further or written.
2. **Cards, not the file.** The read returns each active card's lane,
   first line, checked state, date and block id -- not lane
   descriptions, continuation lines, the archive, the history or the
   settings. The write takes operations (`add`, `text`, `check`, `date`,
   `tag`, `remove`), never file content.
3. **Everything else is kept byte for byte.** The file is handled as
   lines. An operation rewrites one card's first line, or moves or
   removes one card's lines, or inserts one new line; frontmatter, lane
   headings and descriptions, other cards, the archive, the history and
   settings blocks and the line endings are never re-serialised. The
   plugin promises the same of itself (its board-format document), so a
   board the bot touched still opens and saves unchanged.
4. **Compare-and-swap.** A write names the hash it read; a board that
   changed since (the user typed, Obsidian Sync landed) is `Conflict`
   and nothing is written. The bot reads again on its next pass.
5. **Undoable.** Each write is an Echo changeset, `purpose: boards`,
   holding the board as it was.

**The format** (the plugin's docs/dev/board-format.md): a lane is a `## `
heading; a card is a `- [c] text` list item at the start of a line under
it, with continuation lines indented by a tab or four spaces; a lane
whose heading is followed by a `**Complete**` paragraph is the done
lane; the archive starts at a thematic break; `%% kanban-starlane:...`
blocks close the file. A card's date is `@{YYYY-MM-DD}` and its block id
a trailing ` ^id`. A board whose settings change the date syntax
(`date-trigger`, `date-format`, `date-syntax`) keeps its dates to
itself: they are left in the card's text and never rewritten.

**Done and lanes.** Checking a card moves it to the done lane when the
board has one, and unchecking one that sits there moves it back to the
first lane, as the plugin itself does; otherwise the box changes in
place. A new card goes to the first lane, or the done lane if it is
already done. Lanes carry no other meaning here.

Pure apart from nothing: no filesystem, no clock. api.py reads and
writes; this module only turns bytes into cards and bytes into bytes.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

from vaultd import frontmatter
from vaultd.config import BOARD_CARD_MAX_CHARS, BOARD_MAX_BYTES, BOARD_MAX_CARDS, BOARD_OPS_MAX
from vaultd.knowledge import Refused

BOARD_KEY = "kanban-starlane"
_HEADING_RE = re.compile(r"^## +(.*?)\s*$")
_CARD_RE = re.compile(r"^- \[(.)\] ?(.*)$")
_BLOCK_ID_RE = re.compile(r" \^([A-Za-z0-9-]+)$")
_DATE_RE = re.compile(r" ?@\{(\d{4}-\d{2}-\d{2})\}")
_WIP_RE = re.compile(r"\s*\(\d+\)$")
_THEMATIC_RE = re.compile(r"^(\*\*\*+|---+|___+)\s*$")
_BLOCK_START_RE = re.compile(r"^%% (kanban-starlane:(history|settings)|kanban:settings)\s*$")
_COMPLETE_MARKER = "**Complete**"
_DATE_SETTINGS = ("date-trigger", "date-format", "date-syntax")
# Ids the bot mints for cards it tracks; the plugin's own look the same.
ID_RE = re.compile(r"^[a-z0-9]{4,16}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Card:
    """One active card. `first` and `last` are its line span (continuation
    lines included); `text` is its first line without the box, the date
    and the block id."""

    lane: int
    first: int
    last: int
    mark: str
    text: str
    date: str | None
    block_id: str | None

    @property
    def checked(self) -> bool:
        return self.mark != " "


@dataclass
class Lane:
    title: str
    heading: int
    # One past the lane's last line.
    end: int
    complete: bool = False
    marker: int | None = None
    cards: list[Card] = field(default_factory=list)


@dataclass
class Board:
    lines: list[str]
    newline: str
    lanes: list[Lane]
    # Dates are read and written only when the board uses the default syntax.
    dates: bool

    @property
    def cards(self) -> list[Card]:
        return [card for lane in self.lanes for card in lane.cards]


def _eol(line: str) -> str:
    return line.rstrip("\r\n")


def _is_continuation(line: str) -> bool:
    return line.startswith("\t") or line.startswith("    ")


def _settings_json(lines: list[str], start: int) -> dict:
    """The settings block's JSON, or {} when there is none or it cannot be read."""
    for i in range(start, len(lines)):
        if re.match(r"^%% (kanban-starlane:settings|kanban:settings)\s*$", _eol(lines[i])):
            body = []
            for line in lines[i + 1 :]:
                text = _eol(line)
                if text.startswith("```") or text == "%%":
                    if body:
                        break
                    continue
                body.append(text)
            try:
                loaded = json.loads("\n".join(body))
            except ValueError:
                return {}
            return loaded if isinstance(loaded, dict) else {}
    return {}


def parse(data: bytes) -> Board | None:
    """The board in `data`, or None when it is not a Starlane board (or is
    too large, not UTF-8, or has frontmatter the strict loader refuses)."""
    if len(data) > BOARD_MAX_BYTES:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    loaded = frontmatter.load(data)
    if loaded is None or BOARD_KEY not in loaded:
        return None
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"

    # Skip the frontmatter: the first line is its opening fence.
    body_start = 1
    while body_start < len(lines) and _eol(lines[body_start]) != "---":
        body_start += 1
    body_start += 1

    # The active part ends at the archive's thematic break or a %% block.
    active_end = len(lines)
    in_fence = False
    for i in range(body_start, len(lines)):
        line = _eol(lines[i])
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        if _BLOCK_START_RE.match(line) or (_THEMATIC_RE.match(line) and not _is_continuation(lines[i])):
            active_end = i
            break

    lanes: list[Lane] = []
    for i in range(body_start, active_end):
        match = _HEADING_RE.match(_eol(lines[i]))
        if match:
            if lanes:
                lanes[-1].end = i
            lanes.append(Lane(title=_WIP_RE.sub("", match.group(1)), heading=i, end=active_end))
    for index, lane in enumerate(lanes):
        i = lane.heading + 1
        while i < lane.end:
            line = _eol(lines[i])
            match = _CARD_RE.match(line)
            if match is None:
                if not lane.cards and line.strip() == _COMPLETE_MARKER:
                    lane.complete, lane.marker = True, i
                i += 1
                continue
            last = i
            while last + 1 < lane.end and _is_continuation(lines[last + 1]) and lines[last + 1].strip():
                last += 1
            lane.cards.append(_card(index, i, last, match.group(1), match.group(2)))
            i = last + 1
    if sum(len(lane.cards) for lane in lanes) > BOARD_MAX_CARDS:
        return None
    dates = not any(key in _settings_json(lines, active_end) for key in _DATE_SETTINGS)
    if not dates:
        # The date stays in the text, exactly as the user wrote it.
        for lane in lanes:
            for card in lane.cards:
                card.text, card.date = _split(_eol(lines[card.first]), dates=False)[1:3]
    return Board(lines=lines, newline=newline, lanes=lanes, dates=dates)


def _split(line: str, *, dates: bool) -> tuple[str, str, str | None, str | None]:
    """(mark, text, date, block id) of a card's first line."""
    match = _CARD_RE.match(line)
    assert match is not None
    mark, rest = match.group(1), match.group(2)
    block_id = None
    found = _BLOCK_ID_RE.search(rest)
    if found:
        block_id, rest = found.group(1), rest[: found.start()]
    date = None
    if dates:
        found = _DATE_RE.search(rest)
        if found:
            date = found.group(1)
            rest = rest[: found.start()] + rest[found.end() :]
    return mark, rest.strip(), date, block_id


def _card(lane: int, first: int, last: int, mark: str, rest: str) -> Card:
    _, text, date, block_id = _split(f"- [{mark}] {rest}", dates=True)
    return Card(lane=lane, first=first, last=last, mark=mark, text=text, date=date, block_id=block_id)


def describe(board: Board) -> dict:
    """What the bot may know of a board: its lanes' titles and its cards."""
    return {
        "lanes": [lane.title for lane in board.lanes],
        "done_lane": any(lane.complete for lane in board.lanes),
        "dates": board.dates,
        "cards": [
            {
                "n": n,
                "id": card.block_id,
                "lane": board.lanes[card.lane].title,
                "text": card.text,
                "checked": card.checked,
                "date": card.date,
            }
            for n, card in enumerate(board.cards)
        ],
    }


# -- writing -----------------------------------------------------------------------


def _check_text(text: object) -> str:
    """A card's text as the bot may write it: one line that reads back as
    the same text. Anything that would become structure -- a line break, a
    trailing block id, a date token, a comment fence -- is refused, not
    escaped: an escape would travel back to the planner as the title."""
    if not isinstance(text, str):
        raise Refused("bad_card")
    text = unicodedata.normalize("NFC", text).strip()
    if not text or len(text) > BOARD_CARD_MAX_CHARS:
        raise Refused("bad_card")
    if any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") for c in text):
        raise Refused("bad_card")
    if "%%" in text or _BLOCK_ID_RE.search(text) or _DATE_RE.search(text):
        raise Refused("bad_card")
    return text


def _check_date(date: object) -> str | None:
    if date is None:
        return None
    if not isinstance(date, str) or not _ISO_DATE_RE.match(date):
        raise Refused("bad_card")
    return date


def _check_id(block_id: object) -> str:
    if not isinstance(block_id, str) or not ID_RE.match(block_id):
        raise Refused("bad_card")
    return block_id


def _line(board: Board, mark: str, text: str, date: str | None, block_id: str | None) -> str:
    out = f"- [{mark}] {text}"
    if date and board.dates:
        out += f" @{{{date}}}"
    if block_id:
        out += f" ^{block_id}"
    return out + board.newline


def _find(board: Board, block_id: object) -> Card:
    wanted = _check_id(block_id)
    for card in board.cards:
        if card.block_id == wanted:
            return card
    raise Refused("card_missing")


def _rewrite(board: Board, card: Card) -> None:
    board.lines[card.first] = _line(board, card.mark, card.text, card.date, card.block_id)


def _reparse(board: Board) -> Board:
    again = parse("".join(board.lines).encode("utf-8"))
    if again is None:
        raise Refused("bad_card")
    return again


def _insert_at(board: Board, lane: Lane) -> tuple[int, list[str]]:
    """Where a new card goes in `lane`, and any blank line it needs first:
    after the lane's last card; in an empty lane, after the done marker, or
    after a blank line under the heading or description."""
    if lane.cards:
        return lane.cards[-1].last + 1, []
    last = lane.heading
    for i in range(lane.heading + 1, lane.end):
        if board.lines[i].strip():
            last = i
    if lane.marker is not None and last == lane.marker:
        return last + 1, []
    if last + 1 < lane.end and not board.lines[last + 1].strip():
        return last + 2, []
    return last + 1, [board.newline]


def _target_lane(board: Board, checked: bool) -> Lane:
    if not board.lanes:
        raise Refused("board_no_lane")
    if checked:
        for lane in board.lanes:
            if lane.complete:
                return lane
    for lane in board.lanes:
        if not lane.complete:
            return lane
    return board.lanes[0]


def _ensure_newline(board: Board) -> None:
    if board.lines and not board.lines[-1].endswith("\n"):
        board.lines[-1] += board.newline


def _move(board: Board, card: Card, lane: Lane) -> Board:
    block = board.lines[card.first : card.last + 1]
    del board.lines[card.first : card.last + 1]
    board = _reparse(board)
    target = next(x for x in board.lanes if x.title == lane.title and x.complete == lane.complete)
    at, lead = _insert_at(board, target)
    _ensure_newline(board)
    board.lines[at:at] = lead + block
    return _reparse(board)


def _apply_one(board: Board, op: dict) -> Board:
    kind = op.get("op")
    if kind == "tag":
        # Give a card that has no block id one, so it can be followed.
        if set(op) != {"op", "n", "id"} or not isinstance(op["n"], int) or isinstance(op["n"], bool):
            raise Refused("bad_card")
        cards = board.cards
        if not 0 <= op["n"] < len(cards) or cards[op["n"]].block_id is not None:
            raise Refused("card_missing")
        new_id = _check_id(op["id"])
        if any(card.block_id == new_id for card in cards):
            raise Refused("bad_card")
        card = cards[op["n"]]
        line = _eol(board.lines[card.first])
        board.lines[card.first] = f"{line} ^{new_id}{board.newline}"
        return _reparse(board)
    if kind == "add":
        if not {"op", "id", "text"} <= set(op) or not set(op) <= {"op", "id", "text", "date", "checked"}:
            raise Refused("bad_card")
        new_id = _check_id(op["id"])
        if any(card.block_id == new_id for card in board.cards):
            raise Refused("bad_card")
        checked = op.get("checked", False)
        if not isinstance(checked, bool):
            raise Refused("bad_card")
        lane = _target_lane(board, checked)
        at, lead = _insert_at(board, lane)
        _ensure_newline(board)
        new = _line(board, "x" if checked else " ", _check_text(op["text"]), _check_date(op.get("date")), new_id)
        board.lines[at:at] = lead + [new]
        return _reparse(board)
    if kind == "text":
        if set(op) != {"op", "id", "text"}:
            raise Refused("bad_card")
        card = _find(board, op["id"])
        card.text = _check_text(op["text"])
        _rewrite(board, card)
        return _reparse(board)
    if kind == "date":
        if set(op) != {"op", "id", "date"} or not board.dates:
            raise Refused("bad_card")
        card = _find(board, op["id"])
        card.date = _check_date(op["date"])
        _rewrite(board, card)
        return _reparse(board)
    if kind == "check":
        if set(op) != {"op", "id", "checked"} or not isinstance(op["checked"], bool):
            raise Refused("bad_card")
        card = _find(board, op["id"])
        if card.checked != op["checked"]:
            card.mark = "x" if op["checked"] else " "
            # Only the box changes: the rest of the line stays as written.
            line = board.lines[card.first]
            board.lines[card.first] = f"- [{card.mark}]" + line[5:]
        here = board.lanes[card.lane]
        target = _target_lane(board, op["checked"])
        moves = (op["checked"] and target.complete and not here.complete) or (not op["checked"] and here.complete)
        return _move(board, card, target) if moves and target is not here else _reparse(board)
    if kind == "remove":
        if set(op) != {"op", "id"}:
            raise Refused("bad_card")
        card = _find(board, op["id"])
        del board.lines[card.first : card.last + 1]
        return _reparse(board)
    raise Refused("bad_card")


def apply(data: bytes, ops: object) -> bytes:
    """`data` with `ops` applied in order, all or nothing. Raises Refused
    (`not_board`, `bad_card`, `card_missing`, `board_no_lane`)."""
    board = parse(data)
    if board is None:
        raise Refused("not_board")
    if not isinstance(ops, list) or not 0 < len(ops) <= BOARD_OPS_MAX:
        raise Refused("bad_card")
    for op in ops:
        if not isinstance(op, dict):
            raise Refused("bad_card")
        board = _apply_one(board, op)
    out = "".join(board.lines).encode("utf-8")
    if len(out) > BOARD_MAX_BYTES:
        raise Refused("too_large")
    return out
