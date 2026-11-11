"""Inside a board card (boards.py): its subtasks and its `after:` line.

A card's continuation lines are the user's own; two kinds of them are read
here, and every other line is left opaque, byte for byte and in place:

- **Subtasks**, a nested checklist. A line `- [c] text` indented by d
  units (a unit is one tab or four spaces, 1 <= d <= BOARD_SUB_MAX_DEPTH)
  is a subtask of depth d, as long as d is at most one deeper than the
  subtask before it (the first one is at depth 1). A line that jumps
  deeper is opaque. Its first line gives the mark (any non-space is
  checked, the character itself is kept), the text, a trailing ` ^id` and,
  when the board uses the default date syntax, an `@{YYYY-MM-DD}` date.
  A subtask's block is its line and the lines right under it indented
  further than it (its own continuation text and its children; a tab
  counts as four spaces), up to the first line that is not: that block
  is what goes with it.
- **Dependencies**, one line `after: [[Board#^id|Label]], [[#^id|Label]]`
  indented by exactly one unit (`after:` in any case). Each link names a
  card by block id, on another board (its file name without `.md`) or on
  this one (no name); the label is optional. The line is split on the
  commas outside `[[...]]`; a segment may also be a plain card title
  (`after: Buy soil, [[#^card02|Water]]`): no `[`, `]` or `|`, and a text
  a card could have (`clean_text`). The bot resolves titles to cards and
  rewrites the line as links. Anything else on the line is reported as
  `after_raw_extra` and is dropped only when the bot rewrites the line.
  Only the first such line counts; others are opaque.

New lines are indented in the board's own style: tabs when the first
indented continuation line of the board's cards starts with a tab, four
spaces otherwise.

Pure: lines in, structure out. boards.py does the writing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from vaultd.config import BOARD_CARD_MAX_CHARS, BOARD_SUB_MAX_DEPTH

BLOCK_ID_RE = re.compile(r" \^([A-Za-z0-9-]+)$")
DATE_RE = re.compile(r" ?@\{(\d{4}-\d{2}-\d{2})\}")
SUB_RE = re.compile(rf"^((?:\t|    ){{1,{BOARD_SUB_MAX_DEPTH}}})- \[(.)\] (.*)$")
AFTER_RE = re.compile(r"^(\t|    )after:\s*(.*)$", re.IGNORECASE)
LINK_RE = re.compile(r"\[\[([^\[\]|#^]*)#\^([A-Za-z0-9-]+)(?:\|([^\[\]|]*))?\]\]")
_UNIT_RE = re.compile(r"\t|    ")


@dataclass
class Sub:
    """One subtask. `line` is its first line, `end` the last line of its
    block (children and continuation text included); `prefix` is its
    indent as written."""

    line: int
    end: int
    depth: int
    prefix: str
    mark: str
    text: str
    date: str | None
    block_id: str | None

    @property
    def checked(self) -> bool:
        return self.mark != " "


@dataclass
class Inside:
    subs: list[Sub] = field(default_factory=list)
    after_line: int | None = None
    # (target as written, without `.md` or empty, block id, label or None)
    after: list[tuple[str, str, str | None]] = field(default_factory=list)
    # The plain titles on the line, in order (clean_text's form).
    after_titles: list[str] = field(default_factory=list)
    after_raw_extra: bool = False


def eol(line: str) -> str:
    return line.rstrip("\r\n")


def width(line: str) -> int:
    """How far `line` is indented, a tab counting as four spaces."""
    n = 0
    for c in line:
        if c == " ":
            n += 1
        elif c == "\t":
            n += 4
        else:
            break
    return n


def split_rest(rest: str, *, dates: bool) -> tuple[str, str | None, str | None]:
    """(text, date, block id) of what follows a checkbox."""
    block_id = None
    found = BLOCK_ID_RE.search(rest)
    if found:
        block_id, rest = found.group(1), rest[: found.start()]
    date = None
    if dates:
        found = DATE_RE.search(rest)
        if found:
            date = found.group(1)
            rest = rest[: found.start()] + rest[found.end() :]
    return rest.strip(), date, block_id


def clean_text(text: str) -> str | None:
    """`text` as a card's text (NFC, stripped), or None when it is not one
    the bot may write: one line that reads back as the same text, so no
    line break or control character, no trailing block id, no date token,
    no comment fence."""
    text = unicodedata.normalize("NFC", text).strip()
    if not text or len(text) > BOARD_CARD_MAX_CHARS:
        return None
    if any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") for c in text):
        return None
    if "%%" in text or BLOCK_ID_RE.search(text) or DATE_RE.search(text):
        return None
    return text


def _segments(raw: str) -> list[str]:
    """`raw` split on the commas outside `[[...]]`."""
    out, start, depth, i = [], 0, 0, 0
    while i < len(raw):
        pair = raw[i : i + 2]
        if pair == "[[":
            depth, i = depth + 1, i + 2
        elif pair == "]]" and depth:
            depth, i = depth - 1, i + 2
        elif raw[i] == "," and not depth:
            out.append(raw[start:i])
            start, i = i + 1, i + 1
        else:
            i += 1
    out.append(raw[start:])
    return out


def parse_after(raw: str) -> tuple[list[tuple[str, str, str | None]], list[str], bool]:
    """The links on an `after:` line, its plain titles, and whether
    anything else is there."""
    links: list[tuple[str, str, str | None]] = []
    titles: list[str] = []
    extra = False
    for segment in _segments(raw):
        if not segment.strip():
            continue
        if any(c in segment for c in "[]|"):
            links += [(m.group(1).strip(), m.group(2), m.group(3)) for m in LINK_RE.finditer(segment)]
            extra = extra or bool(LINK_RE.sub("", segment).strip())
            continue
        title = clean_text(segment)
        if title is None:
            extra = True
        else:
            titles.append(title)
    return links, titles, extra


def scan(lines: list[str], first: int, last: int, *, dates: bool) -> Inside:
    """The subtasks and the `after:` line among a card's continuation
    lines `first + 1 .. last`."""
    inside = Inside()
    prev = 0
    for i in range(first + 1, last + 1):
        text = eol(lines[i])
        match = SUB_RE.match(text)
        if match:
            depth = len(_UNIT_RE.findall(match.group(1)))
            if depth <= prev + 1:
                rest_text, date, block_id = split_rest(match.group(3), dates=dates)
                inside.subs.append(
                    Sub(line=i, end=i, depth=depth, prefix=match.group(1), mark=match.group(2), text=rest_text, date=date, block_id=block_id)
                )
                prev = depth
                continue
        if inside.after_line is None:
            found = AFTER_RE.match(text)
            if found:
                inside.after_line = i
                inside.after, inside.after_titles, inside.after_raw_extra = parse_after(found.group(2))
    by_line = {sub.line: sub for sub in inside.subs}
    for sub in inside.subs:
        j = sub.line + 1
        while j <= last:
            other = by_line.get(j)
            if other is not None:
                if other.depth <= sub.depth:
                    break
            elif j == inside.after_line or width(lines[j]) <= width(sub.prefix):
                break
            j += 1
        sub.end = j - 1
    return inside


def indent_unit(lines: list[str], spans: list[tuple[int, int]]) -> str:
    """The board's indent: a tab when the first continuation line of its
    cards starts with one, else four spaces."""
    for first, last in spans:
        if last > first:
            return "\t" if lines[first + 1].startswith("\t") else "    "
    return "    "


def sub_line(prefix: str, mark: str, text: str, date: str | None, block_id: str | None, newline: str) -> str:
    out = f"{prefix}- [{mark}] {text}"
    if date:
        out += f" @{{{date}}}"
    if block_id:
        out += f" ^{block_id}"
    return out + newline


def link_text(target: str, block_id: str, label: str) -> str:
    return f"[[{target}#^{block_id}|{label}]]"
