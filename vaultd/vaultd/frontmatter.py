"""Frontmatter and the opt-in rule (plan sections 4.3 and 4.4).

**What counts as frontmatter.** A single leading `---` fence whose
closing fence ends within the first 4 KB. Anything else -- no fence, an
unclosed fence, a longer block, a byte-order mark, invalid UTF-8 --
means "no frontmatter", and so "not opted in".

**The loader refuses two things plain `safe_load` accepts:**

- *anchors and aliases* (`&x`, `*x`), since an alias bomb is the one
  way 4 KB of YAML can still eat memory;
- *duplicate keys*, because Obsidian Sync's merge can leave two
  `anchor:` lines behind, and `safe_load` silently keeps the last one.
  A note whose opt-in depends on which of two lines wins is not opted
  in.

**Opting in** takes exactly `anchor: read`, as a string, at the top
level of a mapping. Not a folder, not a tag, not `anchor: Read`, not
`anchor: [read]`. The bot keeps its own copy of this loader (5b); the
two never share code, by the independence rule.
"""

from __future__ import annotations

import yaml

from vaultd.config import FRONTMATTER_MAX_BYTES

OPT_IN_KEY = "anchor"
OPT_IN_VALUE = "read"


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


def is_opted_in(data: bytes) -> bool:
    """True iff the note says `anchor: read` and is readable text throughout.

    The whole file must be UTF-8: a note the bot could not decode is a
    note it cannot read, and listing it would only produce a GET that
    fails.
    """
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    meta = load(data)
    if meta is None:
        return False
    value = meta.get(OPT_IN_KEY)
    return isinstance(value, str) and value == OPT_IN_VALUE
