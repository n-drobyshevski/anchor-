"""Frontmatter and the note's own mark (8e plan section 3; phase-8 plan 4.4).

**What counts as frontmatter.** A single leading `---` fence whose
closing fence ends within the first 4 KB. No fence at all means "no
frontmatter".

**The loader refuses two things plain `safe_load` accepts:**

- *anchors and aliases* (`&x`, `*x`), since an alias bomb is the one
  way 4 KB of YAML can still eat memory;
- *duplicate keys*, because Obsidian Sync's merge can leave two
  `anchor:` lines behind, and `safe_load` silently keeps the last one.

**The mark** is what the note itself says, before any folder rule
(classes.py combines the two). Exactly `anchor: never`, `anchor:
personal`, `anchor: knowledge` or `anchor: lens` (lens plan section 3),
as a string at the top level of a mapping. 8a's `anchor: read` is
`legacy_read`, which classes.py counts as personal.

**Aliases, tags and a summary** (`note_meta`) are read for the graph
(lens plan section 4), and only ever for a note already resolved to
knowledge or lens: the caller checks the class first, so a personal
note's properties are never even parsed for them. Anything of the
wrong shape reads as absent rather than failing the note.

**A note whose properties cannot be read is `unknown`, not `none`.**
`none` lets a folder rule decide the class; `unknown` hides the note
whatever the folders say. A leading fence that is unclosed, over 4 KB,
behind a byte-order mark, or holds YAML the strict loader refuses might
have said `anchor: never`, so a folder rule must not be able to reveal
it (docs/decisions.md, "8e -- unreadable properties hide the note").
The same goes for a file that is not UTF-8 throughout: the bot could
not read it anyway. The bot keeps its own copy of this loader (8b); the
two never share code, by the independence rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import yaml

from vaultd.config import FRONTMATTER_MAX_BYTES

MARK_KEY = "anchor"

NoteMark = Literal["never", "personal", "knowledge", "lens", "legacy_read", "unknown", "none"]

# The property values that classify a note, and 8a's opt-in, which now
# reads as personal (classes.py).
_MARKS: dict[str, NoteMark] = {
    "never": "never",
    "personal": "personal",
    "knowledge": "knowledge",
    "lens": "lens",
    "read": "legacy_read",
}

# A graph node's `summary` is a catalog line, not a second body.
SUMMARY_MAX_CHARS = 300
_TAG_SPLIT_RE = re.compile(r"[,\s]+")

_BOM = b"\xef\xbb\xbf"


class FrontmatterError(Exception):
    pass


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that raises on anchors, aliases and duplicate keys."""

    def compose_node(self, parent, index):  # type: ignore[override]
        if self.check_event(yaml.AliasEvent):
            raise FrontmatterError("alias")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise FrontmatterError("anchor")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):  # type: ignore[override]
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
            except TypeError:
                raise FrontmatterError("unhashable key") from None
            if duplicate:
                raise FrontmatterError("duplicate key")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _fences(data: bytes) -> tuple[int, int, int] | None:
    """(YAML start, YAML end, body start) offsets, or None if there is no frontmatter."""
    head = data[: FRONTMATTER_MAX_BYTES]
    for opening in (b"---\n", b"---\r\n"):
        if head.startswith(opening):
            break
    else:
        return None
    start = len(opening)
    pos = start
    while True:
        end = head.find(b"\n", pos)
        line_end = end if end != -1 else len(head)
        line = head[pos:line_end].rstrip(b"\r")
        if line == b"---":
            # The closing fence must end inside the cap: either its
            # newline is there, or the file ends right after it.
            if end == -1 and len(data) > len(head):
                return None
            return start, pos, (end + 1 if end != -1 else len(data))
        if end == -1:
            return None
        pos = end + 1


def split(data: bytes) -> bytes | None:
    """The raw YAML between the fences, or None if there is no frontmatter."""
    fences = _fences(data)
    return None if fences is None else data[fences[0] : fences[1]]


def body(data: bytes) -> bytes:
    """Everything after the closing fence; the whole file when there is no frontmatter."""
    fences = _fences(data)
    return data if fences is None else data[fences[2] :]


def load(data: bytes) -> dict | None:
    """The frontmatter mapping, or None when there is none or it is unusable."""
    block = split(data)
    if block is None:
        return None
    try:
        text = block.decode("utf-8")
        loaded = yaml.load(text, Loader=StrictLoader)  # noqa: S506 - StrictLoader is a SafeLoader
    except (UnicodeDecodeError, yaml.YAMLError, FrontmatterError, RecursionError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _has_leading_fence(data: bytes) -> bool:
    """True when the file opens with a `---` line, byte-order mark or not."""
    if data.startswith(_BOM):
        data = data[len(_BOM) :]
    first = data.split(b"\n", 1)[0].rstrip(b"\r")
    return first == b"---"


def raw_anchor(data: bytes) -> str | None:
    """The literal `anchor:` value in `data`'s frontmatter, or None if absent.

    Unlike `note_mark`, this is not mapped through `_MARKS`: a knowledge
    write must compare the exact string a note carried before and after,
    so `anchor: read` and `anchor: knowledge` are never conflated with
    `legacy_read` here. Callers must already know the frontmatter parses
    (`note_mark(data) != "unknown"`); an unparsable block reads as absent.
    """
    loaded = load(data)
    if loaded is None or MARK_KEY not in loaded:
        return None
    value = loaded[MARK_KEY]
    return value if isinstance(value, str) else None


def note_mark(data: bytes) -> NoteMark:
    """What the note's own properties say about it (module docstring)."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"
    if not _has_leading_fence(data):
        return "none"
    block = split(data)
    if block is None:
        return "unknown"
    try:
        loaded = yaml.load(block.decode("utf-8"), Loader=StrictLoader)  # noqa: S506 - StrictLoader is a SafeLoader
    except (UnicodeDecodeError, yaml.YAMLError, FrontmatterError, RecursionError, ValueError):
        return "unknown"
    if loaded is None:
        return "none"
    if not isinstance(loaded, dict):
        return "unknown"
    if MARK_KEY not in loaded:
        return "none"
    value = loaded[MARK_KEY]
    if not isinstance(value, str):
        return "unknown"
    return _MARKS.get(value, "unknown")


@dataclass(frozen=True)
class NoteMeta:
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    summary: str | None = None


def _strings(value: object, *, split_tags: bool = False) -> tuple[str, ...]:
    """A list of strings, or one string, as a tuple; anything else is ().

    Non-string items in a list (a number, a nested mapping) are dropped,
    not fatal. A single tags string may hold several, the way Obsidian
    reads `tags: a, b`; a single alias string is one alias.
    """
    if isinstance(value, str):
        items = _TAG_SPLIT_RE.split(value) if split_tags else [value]
    elif isinstance(value, list):
        items = [item for item in value if isinstance(item, str)]
    else:
        return ()
    out: list[str] = []
    for item in items:
        item = item.strip()
        if split_tags:
            item = item.lstrip("#")
        if item and item not in out:
            out.append(item)
    return tuple(out)


def note_meta(data: bytes) -> NoteMeta:
    """`aliases`, `tags` and `summary` from a knowledge or lens note's properties.

    The caller must already know the note is knowledge or lens (module
    docstring). No frontmatter, or frontmatter the strict loader
    refuses, reads as no metadata.
    """
    loaded = load(data)
    if loaded is None:
        return NoteMeta()
    summary = loaded.get("summary")
    if isinstance(summary, str):
        summary = summary.strip()[:SUMMARY_MAX_CHARS] or None
    else:
        summary = None
    return NoteMeta(
        aliases=_strings(loaded.get("aliases")),
        tags=_strings(loaded.get("tags"), split_tags=True),
        summary=summary,
    )
