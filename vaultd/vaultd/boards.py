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
   first line, checked state, date and block id, and on request
   (`describe(subs=True)`, `GET /v1/boards?subs=1`) its subtasks' first
   lines and the cards its `after:` line names (`&titles=1`: by title
   too) -- not lane descriptions,
   other continuation lines, the archive, the history or the settings.
   The write takes operations, never file content: on cards `add`,
   `text`, `check`, `date`, `tag`, `remove`, `move`; on a card's subtasks
   `sub_add`, `sub_text`, `sub_check`, `sub_date`, `sub_tag`,
   `sub_remove`; on its dependencies `after_set`.
3. **Everything else is kept byte for byte.** The file is handled as
   lines. An operation rewrites one card's or subtask's first line or a
   card's `after:` line, or moves or removes one card's or subtask's
   lines, or inserts one new line; frontmatter, lane
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

**Inside a card** (board_subs.py), two kinds of continuation line are
read; every other one stays opaque and in place:

    - [ ] Plan the trip @{2026-11-14} ^card01
        after: [[#^card02|Water the plants]], [[General tasks#^gen001|Buy soil]]
        - [x] Pick dates ^sub001
        - [ ] Book the train @{2026-11-17} ^sub002
            - [ ] Compare fares ^sub003

A subtask is a `- [c] text` line indented 1..BOARD_SUB_MAX_DEPTH units
(a tab or four spaces each), at most one deeper than the subtask before
it; it carries a mark, text, date and block id like a card, and its
block (the lines under it indented further, children included) goes
with it. The `after:` line, indented one unit, names the cards this one
waits for by block id: `[[#^id]]` on this board, `[[Board#^id]]` on
another named board. `sub_add` appends a subtask after its parent's
block, or a top-level one after the last subtask's block (after the
card's last continuation line when it has none); `after_set` rewrites
the `after:` line in place or writes it right under the card's first
line, and removes it for an empty list. New lines use the card's indent
style, or the board's (tabs when its first continuation line has one).
Checking, moving and removing a card carry its subtasks, since they are
its continuation lines.

**Done and lanes.** Checking a card moves it to the done lane when the
board has one, and unchecking one that sits there moves it back to the
first lane, as the plugin itself does; otherwise the box changes in
place. A new card goes to the first lane, or the done lane if it is
already done. `move` puts a card, with all its lines, at the end of the
lane named by its title (the first lane of that title; a lane of the
active part, never the archive); a card already in that lane stays where
it is. On a board with a done lane the box follows, as with `check`:
moving into a complete lane checks it, moving into any other lane
unchecks it. On a board without one the box is left as it is. (A board may mark
more than one lane `**Complete**`, a "Cancelled" beside "Done": each is
complete, and `check` uses the first.) Read with `lanes=True` (`GET
/v1/boards?lanes=1`), a board also names its done lane
(`done_lane_title`: the first complete lane, null when it has none) and
says which lanes are complete (`lane_complete`, one flag per title in
`lanes`), so the bot can read a card's lane as a state (a complete lane:
done, the first other lane: planned, any other: in progress); the keys'
presence is also how the bot knows this vaultd takes `move`.

Pure apart from nothing: no filesystem, no clock. api.py reads and
writes; this module only turns bytes into cards and bytes into bytes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from vaultd import board_subs, frontmatter
from vaultd.board_subs import Inside, Sub
from vaultd.config import (
    BOARD_AFTER_LINKS_MAX,
    BOARD_MAX_BYTES,
    BOARD_MAX_CARDS,
    BOARD_OPS_MAX,
    BOARD_SUB_MAX_DEPTH,
    BOARD_SUBS_PER_CARD_MAX,
)
from vaultd.knowledge import Refused

BOARD_KEY = "kanban-starlane"
_HEADING_RE = re.compile(r"^## +(.*?)\s*$")
_CARD_RE = re.compile(r"^- \[(.)\] ?(.*)$")
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
    # Its subtasks and its `after:` line (board_subs.py).
    inside: Inside = field(default_factory=Inside)

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
    # The indent new continuation lines are written with: a tab or four spaces.
    indent: str = "    "

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
    spans = []
    for lane in lanes:
        for card in lane.cards:
            card.inside = board_subs.scan(lines, card.first, card.last, dates=dates)
            spans.append((card.first, card.last))
    indent = board_subs.indent_unit(lines, spans)
    return Board(lines=lines, newline=newline, lanes=lanes, dates=dates, indent=indent)


def _split(line: str, *, dates: bool) -> tuple[str, str, str | None, str | None]:
    """(mark, text, date, block id) of a card's first line."""
    match = _CARD_RE.match(line)
    assert match is not None
    text, date, block_id = board_subs.split_rest(match.group(2), dates=dates)
    return match.group(1), text, date, block_id


def _card(lane: int, first: int, last: int, mark: str, rest: str) -> Card:
    _, text, date, block_id = _split(f"- [{mark}] {rest}", dates=True)
    return Card(lane=lane, first=first, last=last, mark=mark, text=text, date=date, block_id=block_id)


def _link_board(target: str, name: str | None) -> str | None:
    """The file an `after:` link names: this board's own for `[[#^id]]`."""
    if not target:
        return name
    return target if target.endswith(".md") else f"{target}.md"


def describe(
    board: Board, *, name: str | None = None, subs: bool = False, titles: bool = False, lanes: bool = False
) -> dict:
    """What the bot may know of a board: its lanes' titles and its cards.
    With `subs`, each card also carries its subtasks (first lines only) and
    the cards its `after:` line names, `name` being this board's file name
    (what a same-board link resolves to). With `titles` as well, also the
    plain titles on that line (`after_titles`); without it a title counts
    as `after_raw_extra`, which is all an older bot knows to keep. With
    `lanes`, the board also names its done lane (`done_lane_title`, the
    first complete lane, None when it has none) and flags each complete
    lane (`lane_complete`, parallel to `lanes`); an older bot, which checks
    a board's keys exactly, asks without them."""
    cards = []
    for n, card in enumerate(board.cards):
        item = {
            "n": n,
            "id": card.block_id,
            "lane": board.lanes[card.lane].title,
            "text": card.text,
            "checked": card.checked,
            "date": card.date,
        }
        if subs:
            item["subs"] = [
                {"n": k, "id": sub.block_id, "depth": sub.depth, "text": sub.text, "checked": sub.checked, "date": sub.date}
                for k, sub in enumerate(card.inside.subs)
            ]
            item["after"] = [{"board": _link_board(target, name), "id": block_id} for target, block_id, _ in card.inside.after]
            if titles:
                item["after_titles"] = list(card.inside.after_titles)
                item["after_raw_extra"] = card.inside.after_raw_extra
            else:
                item["after_raw_extra"] = card.inside.after_raw_extra or bool(card.inside.after_titles)
        cards.append(item)
    out = {
        "lanes": [lane.title for lane in board.lanes],
        "done_lane": any(lane.complete for lane in board.lanes),
        "dates": board.dates,
        "cards": cards,
    }
    if lanes:
        done = next((lane for lane in board.lanes if lane.complete), None)
        out["done_lane_title"] = done.title if done is not None else None
        out["lane_complete"] = [lane.complete for lane in board.lanes]
    return out


# -- writing -----------------------------------------------------------------------


def _check_text(text: object) -> str:
    """A card's text as the bot may write it: one line that reads back as
    the same text. Anything that would become structure -- a line break, a
    trailing block id, a date token, a comment fence -- is refused, not
    escaped: an escape would travel back to the planner as the title."""
    if not isinstance(text, str):
        raise Refused("bad_card")
    clean = board_subs.clean_text(text)
    if clean is None:
        raise Refused("bad_card")
    return clean


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


def _named_lane(board: Board, title: object) -> Lane:
    """The first lane of the active part titled `title` (as the read
    reports titles: without a WIP count)."""
    if not isinstance(title, str) or not title:
        raise Refused("bad_card")
    for lane in board.lanes:
        if lane.title == title:
            return lane
    raise Refused("bad_card")


def _set_box(board: Board, card: Card, checked: bool) -> None:
    """Only the box changes: the rest of the line stays as written."""
    if card.checked != checked:
        card.mark = "x" if checked else " "
        line = board.lines[card.first]
        board.lines[card.first] = f"- [{card.mark}]" + line[5:]


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


def _ids(board: Board) -> set[str]:
    """Every block id on the board's active cards and their subtasks."""
    out = set()
    for card in board.cards:
        if card.block_id:
            out.add(card.block_id)
        out.update(sub.block_id for sub in card.inside.subs if sub.block_id)
    return out


def _is_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _find_sub(card: Card, block_id: object) -> Sub:
    wanted = _check_id(block_id)
    for sub in card.inside.subs:
        if sub.block_id == wanted:
            return sub
    raise Refused("card_missing")


def _insert(board: Board, at: int, new: list[str]) -> None:
    if at > 0 and not board.lines[at - 1].endswith("\n"):
        board.lines[at - 1] += board.newline
    board.lines[at:at] = new


def _unit(board: Board, card: Card) -> str:
    """The indent a new line in `card` gets: the card's own style when it
    has continuation lines, else the board's."""
    if card.last > card.first:
        return "\t" if board.lines[card.first + 1].startswith("\t") else "    "
    return board.indent


def _expect_sub(board: Board, card_id: str, sub_id: str) -> Board:
    """The reparsed board must show the subtask the op wrote."""
    card = next(card for card in board.cards if card.block_id == card_id)
    if not any(sub.block_id == sub_id for sub in card.inside.subs):
        raise Refused("bad_card")
    return board


def _check_label(label: object) -> str:
    label = _check_text(label)
    if any(c in label for c in "[]|"):
        raise Refused("bad_card")
    return label


def _apply_sub(board: Board, op: dict, kind: str) -> Board:
    """The subtask operations: each names its card by block id."""
    keys = set(op)
    if kind == "sub_tag":
        if keys != {"op", "card", "n", "id"} or not _is_index(op["n"]):
            raise Refused("bad_card")
        card = _find(board, op["card"])
        subs = card.inside.subs
        if not 0 <= op["n"] < len(subs) or subs[op["n"]].block_id is not None:
            raise Refused("card_missing")
        new_id = _check_id(op["id"])
        if new_id in _ids(board):
            raise Refused("bad_card")
        sub = subs[op["n"]]
        board.lines[sub.line] = f"{_eol(board.lines[sub.line])} ^{new_id}{board.newline}"
        return _expect_sub(_reparse(board), card.block_id, new_id)
    if kind == "sub_add":
        required = {"op", "card", "id", "text", "checked"}
        if not required <= keys or not keys <= required | {"parent", "date"} or not isinstance(op["checked"], bool):
            raise Refused("bad_card")
        card = _find(board, op["card"])
        new_id = _check_id(op["id"])
        if new_id in _ids(board):
            raise Refused("bad_card")
        text = _check_text(op["text"])
        date = _check_date(op.get("date"))
        if date is not None and not board.dates:
            raise Refused("bad_card")
        subs = card.inside.subs
        if len(subs) >= BOARD_SUBS_PER_CARD_MAX:
            raise Refused("bad_card")
        unit = _unit(board, card)
        if "parent" in op:
            parent = _find_sub(card, op["parent"])
            if parent.depth + 1 > BOARD_SUB_MAX_DEPTH:
                raise Refused("bad_card")
            prefix = parent.prefix + ("\t" if parent.prefix.endswith("\t") else "    ")
            at = parent.end + 1
        else:
            prefix = unit
            at = max(sub.end for sub in subs) + 1 if subs else card.last + 1
        mark = "x" if op["checked"] else " "
        _insert(board, at, [board_subs.sub_line(prefix, mark, text, date, new_id, board.newline)])
        return _expect_sub(_reparse(board), card.block_id, new_id)
    if kind == "sub_text":
        if keys != {"op", "card", "id", "text"}:
            raise Refused("bad_card")
        card = _find(board, op["card"])
        sub = _find_sub(card, op["id"])
        text = _check_text(op["text"])
        board.lines[sub.line] = board_subs.sub_line(sub.prefix, sub.mark, text, sub.date, sub.block_id, board.newline)
        return _expect_sub(_reparse(board), card.block_id, sub.block_id)
    if kind == "sub_check":
        if keys != {"op", "card", "id", "checked"} or not isinstance(op["checked"], bool):
            raise Refused("bad_card")
        card = _find(board, op["card"])
        sub = _find_sub(card, op["id"])
        if sub.checked != op["checked"]:
            # Only the box changes: the rest of the line stays as written.
            line, at = board.lines[sub.line], len(sub.prefix) + 3
            board.lines[sub.line] = line[:at] + ("x" if op["checked"] else " ") + line[at + 1 :]
        return _reparse(board)
    if kind == "sub_date":
        if keys != {"op", "card", "id", "date"} or not board.dates:
            raise Refused("bad_card")
        card = _find(board, op["card"])
        sub = _find_sub(card, op["id"])
        date = _check_date(op["date"])
        board.lines[sub.line] = board_subs.sub_line(sub.prefix, sub.mark, sub.text, date, sub.block_id, board.newline)
        return _expect_sub(_reparse(board), card.block_id, sub.block_id)
    if kind == "sub_remove":
        if keys != {"op", "card", "id"}:
            raise Refused("bad_card")
        card = _find(board, op["card"])
        sub = _find_sub(card, op["id"])
        del board.lines[sub.line : sub.end + 1]
        return _reparse(board)
    raise Refused("bad_card")


def _apply_after(board: Board, op: dict, name: str | None, named: tuple[str, ...]) -> Board:
    """Rewrite a card's `after:` line from `links`, or remove it."""
    if set(op) != {"op", "id", "links"} or name is None:
        raise Refused("bad_card")
    links = op["links"]
    if not isinstance(links, list) or len(links) > BOARD_AFTER_LINKS_MAX:
        raise Refused("bad_card")
    card = _find(board, op["id"])
    written, seen = [], set()
    for link in links:
        if not isinstance(link, dict) or set(link) != {"board", "id", "label"}:
            raise Refused("bad_card")
        target = link["board"]
        if not isinstance(target, str) or not target.endswith(".md") or (target != name and target not in named):
            raise Refused("bad_card")
        if any(c in target for c in "[]|#^"):
            raise Refused("bad_card")
        link_id = _check_id(link["id"])
        key = (target, link_id)
        if key in seen:
            raise Refused("bad_card")
        seen.add(key)
        label = _check_label(link["label"])
        written.append(board_subs.link_text("" if target == name else target[: -len(".md")], link_id, label))
    at = card.inside.after_line
    if not written:
        if at is not None:
            del board.lines[at]
        return _reparse(board)
    if at is not None:
        indent = board_subs.AFTER_RE.match(_eol(board.lines[at])).group(1)
        board.lines[at] = f"{indent}after: {', '.join(written)}{board.newline}"
    else:
        _insert(board, card.first + 1, [f"{_unit(board, card)}after: {', '.join(written)}{board.newline}"])
    return _reparse(board)


def _apply_one(board: Board, op: dict, name: str | None = None, named: tuple[str, ...] = ()) -> Board:
    kind = op.get("op")
    if isinstance(kind, str) and kind.startswith("sub_"):
        return _apply_sub(board, op, kind)
    if kind == "after_set":
        return _apply_after(board, op, name, named)
    if kind == "tag":
        # Give a card that has no block id one, so it can be followed.
        if set(op) != {"op", "n", "id"} or not isinstance(op["n"], int) or isinstance(op["n"], bool):
            raise Refused("bad_card")
        cards = board.cards
        if not 0 <= op["n"] < len(cards) or cards[op["n"]].block_id is not None:
            raise Refused("card_missing")
        new_id = _check_id(op["id"])
        if new_id in _ids(board):
            raise Refused("bad_card")
        card = cards[op["n"]]
        line = _eol(board.lines[card.first])
        board.lines[card.first] = f"{line} ^{new_id}{board.newline}"
        return _reparse(board)
    if kind == "add":
        if not {"op", "id", "text"} <= set(op) or not set(op) <= {"op", "id", "text", "date", "checked"}:
            raise Refused("bad_card")
        new_id = _check_id(op["id"])
        if new_id in _ids(board):
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
        _set_box(board, card, op["checked"])
        here = board.lanes[card.lane]
        target = _target_lane(board, op["checked"])
        moves = (op["checked"] and target.complete and not here.complete) or (not op["checked"] and here.complete)
        return _move(board, card, target) if moves and target is not here else _reparse(board)
    if kind == "move":
        # A card, with all its lines, to the end of the lane of that title.
        if set(op) != {"op", "id", "lane"}:
            raise Refused("bad_card")
        card = _find(board, op["id"])
        target = _named_lane(board, op["lane"])
        if any(lane.complete for lane in board.lanes):
            # The box follows the lane, as `check` makes the lane follow the box.
            _set_box(board, card, target.complete)
        here = board.lanes[card.lane]
        return _move(board, card, target) if target is not here else _reparse(board)
    if kind == "remove":
        if set(op) != {"op", "id"}:
            raise Refused("bad_card")
        card = _find(board, op["id"])
        del board.lines[card.first : card.last + 1]
        return _reparse(board)
    raise Refused("bad_card")


def apply(data: bytes, ops: object, *, name: str | None = None, named: tuple[str, ...] = ()) -> bytes:
    """`data` with `ops` applied in order, all or nothing. Raises Refused
    (`not_board`, `bad_card`, `card_missing`, `board_no_lane`). `name` is
    this board's file name and `named` the other boards the user named:
    the boards an `after_set` link may point at (none without `name`)."""
    board = parse(data)
    if board is None:
        raise Refused("not_board")
    if not isinstance(ops, list) or not 0 < len(ops) <= BOARD_OPS_MAX:
        raise Refused("bad_card")
    for op in ops:
        if not isinstance(op, dict):
            raise Refused("bad_card")
        board = _apply_one(board, op, name, named)
    out = "".join(board.lines).encode("utf-8")
    if len(out) > BOARD_MAX_BYTES:
        raise Refused("too_large")
    return out
