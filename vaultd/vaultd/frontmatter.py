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
personal` or `anchor: knowledge`, as a string at the top level of a
mapping. 8a's `anchor: read` is `legacy_read`, which classes.py counts
as personal.

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

from typing import Literal

import yaml

from vaultd.config import FRONTMATTER_MAX_BYTES

MARK_KEY = "anchor"

NoteMark = Literal["never", "personal", "knowledge", "legacy_read", "unknown", "none"]

# The property values that classify a note, and 8a's opt-in, which now
# reads as personal (classes.py).
_MARKS: dict[str, NoteMark] = {
    "never": "never",
    "personal": "personal",
    "knowledge": "knowledge",
    "read": "legacy_read",
}

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


def split(data: bytes) -> bytes | None:
    """The raw YAML between the fences, or None if there is no frontmatter."""
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
            return head[start:pos]
        if end == -1:
            return None
        pos = end + 1


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
