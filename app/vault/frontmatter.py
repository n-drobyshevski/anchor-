"""The bot's frontmatter rules (phase-5 plan section 4.4).

**Its own copy.** vaultd has the same loader; the two projects share
nothing but an HTTP API (tests/test_vault_isolation.py), so the rule is
written twice and each copy is tested on its own side.

**What counts as frontmatter.** A single leading `---` fence whose
closing fence ends within the first 4 KB. Anything else means "no
frontmatter".

**The loader** is a `SafeLoader` that raises on anchors and aliases (an
alias bomb is the one way 4 KB of YAML can still eat memory) and on
duplicate keys (Sync's merge can leave two `fact:` lines, and plain
`safe_load` silently keeps the last one).

**Writing** keeps the user's own keys in their original order *and
form*: `extra_segments` returns each non-Anchor top-level key's lines
verbatim, cut out of the source by the composer's line marks, so a
re-render never re-quotes or re-folds a property Anchor does not own.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

FRONTMATTER_MAX_BYTES = 4096
FENCE = "---"


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


@dataclass(frozen=True)
class Split:
    yaml_text: str
    body: str


def split(content: str) -> Split | None:
    """The YAML between the fences and the body after them, or None."""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != FENCE:
        return None
    consumed = len(lines[0].encode("utf-8"))
    for index in range(1, len(lines)):
        line = lines[index]
        consumed += len(line.encode("utf-8"))
        if consumed > FRONTMATTER_MAX_BYTES:
            return None
        if line.rstrip("\r\n") == FENCE:
            return Split("".join(lines[1:index]), "".join(lines[index + 1 :]))
    return None


def load(content: str) -> dict | None:
    """The frontmatter mapping, or None when absent or unusable."""
    parts = split(content)
    if parts is None:
        return None
    try:
        loaded = yaml.load(parts.yaml_text, Loader=StrictLoader)  # noqa: S506 - a SafeLoader
    except (yaml.YAMLError, FrontmatterError, RecursionError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def extra_segments(content: str, owned_keys: tuple[str, ...]) -> str | None:
    """The raw lines of every top-level key Anchor does not own, in order.

    Returns "" when the file has no frontmatter or only Anchor's keys,
    and None when the frontmatter exists but does not pass the strict
    loader -- the caller must not rewrite a file whose properties it
    cannot read, or it would drop the user's.
    """
    parts = split(content)
    if parts is None:
        return ""
    if load(content) is None:
        return None if parts.yaml_text.strip() else ""
    try:
        node = yaml.compose(parts.yaml_text, Loader=StrictLoader)  # noqa: S506 - a SafeLoader
    except (yaml.YAMLError, FrontmatterError):
        return None
    if not isinstance(node, yaml.MappingNode):
        return None
    lines = parts.yaml_text.splitlines(keepends=True)
    starts = [key_node.start_mark.line for key_node, _ in node.value]
    keep: list[str] = []
    for index, (key_node, _value) in enumerate(node.value):
        start = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        if key_node.value in owned_keys:
            continue
        keep.extend(lines[start:end])
    text = "".join(keep)
    if text and not text.endswith("\n"):
        text += "\n"
    return text
