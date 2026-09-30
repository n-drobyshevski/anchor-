"""`GET /v1/knowledge/graph`: the link graph over knowledge and lens notes (lens plan section 4).

The bot stores it (`note_link`), Claude Code reads the lens part of it,
and L3's garden will look for gaps in it. What it may say is narrow:

- **Nodes are knowledge and lens notes only**, the same set the tree
  lists (classes.py decides, stricter wins; nothing under `Anchor/`;
  nothing over NOTE_MAX_BYTES, which the manifest would not list
  either). Each carries its title, class, lens kind, and the
  `aliases`, `tags` and `summary` properties -- read by
  `frontmatter.note_meta` only after the class check has passed.
- **Edges come from knowledge and lens notes only**, parsed by links.py,
  the one wikilink parser, so the graph and a rename always agree on
  what a link is. A link resolves by basename as Obsidian's does:
  exact name first, then ignoring case; a `[[folder/name]]` form
  narrows by path suffix; among several notes with that name, the one
  in the source's own folder, then the shortest path.
- **Three kinds of edge.** To a visible knowledge or lens note:
  `{src, dst}`. To no note at all: `{src, unresolved: <target text>}`,
  text the source note itself already shows. **To a note that exists
  but is personal, never, unclassified, unreadable or Anchor's own:
  `{src, outside: true}` and nothing else** -- no name, no target text,
  no path. One such edge per distinct hidden note a source links to,
  so it is counted, never named.

**Links are read from what the bot keeps of the note, nothing more**:
the body after the frontmatter, with every `%% ... %%` aside and every
fenced code block removed first -- the same two regexes, in the same
order, as the bot's app/vault/notes_text.py. An aside is "the place for
a private aside inside a knowledge note" (8e plan section 3); the bot
never stores one, so a link written inside one must not reach
`note_link` or `lens.graph()` either, as an unresolved name, a lens
edge or an outside count. Code (`[[ -f x ]]` in bash) is not a link.
Frontmatter is left out for the same reason: the lens body never holds
it. (Rename still scans the whole file, links.py: it may find more
backlinks than the graph shows, never fewer.)

Links to attachments (`![[x.png]]`, `[[paper.pdf]]`), same-note heading
links and self-links are not edges. Duplicates collapse to one edge.
An unusable settings file answers with an empty graph, as the tree
does. At most TREE_MAX_NOTES nodes (sorted by path), `truncated: true`
past that; edges into a note cut off by the cap are dropped with it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from vaultd import classes, frontmatter, links
from vaultd.config import NOTE_MAX_BYTES, TREE_MAX_NOTES

_ANCHOR_TOP = "Anchor"

# The bot's notes_text._COMMENT and _CODE_FENCE, verbatim (module docstring).
_COMMENT = re.compile(r"%%.*?%%", re.DOTALL)
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)

# Obsidian links attachments with the same brackets; they are not notes.
_ATTACHMENT_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|bmp|avif|heic|tiff?|pdf|mp3|wav|m4a|ogg|flac|mp4|webm|mov|mkv|canvas|base)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Visible:
    note_class: str
    lens_kind: str | None
    data: bytes


@dataclass
class _Index:
    """Every note in the vault by basename -- visible or not -- so a link
    to a hidden note resolves to *something* (and becomes `outside`)
    instead of reading as unresolved and naming it."""

    exact: dict[str, list[str]] = field(default_factory=dict)
    folded: dict[str, list[str]] = field(default_factory=dict)

    def add(self, rel: str) -> None:
        base = links.basename(rel)
        self.exact.setdefault(base, []).append(rel)
        self.folded.setdefault(base.casefold(), []).append(rel)


def _empty() -> dict:
    return {"nodes": [], "edges": [], "truncated": False}


def _stem(rel: str) -> str:
    nfc = unicodedata.normalize("NFC", rel)
    return nfc[: -len(".md")] if nfc.endswith(".md") else nfc


def _folder(rel: str) -> str:
    return unicodedata.normalize("NFC", rel).rsplit("/", 1)[0] if "/" in rel else ""


def _candidates(target: str, index: _Index) -> list[str]:
    base = links.target_basename(target)
    found = index.exact.get(base) or index.folded.get(base.casefold(), [])
    path = unicodedata.normalize("NFC", target.strip())
    if path.endswith(".md"):
        path = path[: -len(".md")]
    if "/" in path and found:
        path = path.lstrip("/")
        by_path = [r for r in found if _path_matches(_stem(r), path)]
        if not by_path:
            by_path = [r for r in found if _path_matches(_stem(r).casefold(), path.casefold())]
        if by_path:
            return by_path
    return found


def _path_matches(stem: str, path: str) -> bool:
    return stem == path or stem.endswith("/" + path)


def _pick(src: str, candidates: list[str]) -> str:
    """Among notes sharing a name: the source's own folder, then the shortest path."""
    here = _folder(src)
    same = [r for r in candidates if _folder(r) == here]
    pool = same or candidates
    return min(pool, key=lambda r: (r.count("/"), len(r), r))


def _linkable(data: bytes) -> str:
    """The text links are read from: the body, asides and code removed (module docstring)."""
    text = frontmatter.body(data).decode("utf-8")
    text = _COMMENT.sub("", text)
    return _CODE_FENCE.sub("", text)


def _node(rel: str, note: _Visible, text: str) -> dict:
    meta = frontmatter.note_meta(note.data)
    return {
        "path": rel,
        "title": rel.rsplit("/", 1)[-1][: -len(".md")],
        "class": note.note_class,
        "lens_kind": note.lens_kind,
        "aliases": list(meta.aliases),
        "tags": list(meta.tags),
        "summary": meta.summary,
        "chars": len(text),
    }


def build_graph(root: Path, rules: classes.FolderRules) -> dict:
    """The graph's JSON body (module docstring)."""
    if rules.state == "invalid":
        return _empty()
    index = _Index()
    visible: dict[str, _Visible] = {}
    for rel, data in links.iter_notes(root):
        index.add(rel)
        if unicodedata.normalize("NFC", rel).split("/", 1)[0] == _ANCHOR_TOP:
            continue
        if len(data) > NOTE_MAX_BYTES:
            continue
        mark = frontmatter.note_mark(data)
        if mark == "unknown":
            continue
        resolved = classes.effective_class(rel, mark, rules)
        if resolved.note_class not in classes.READABLE_KNOWLEDGE:
            continue
        visible[rel] = _Visible(resolved.note_class, resolved.lens_kind, data)
    if rules.state == "ok":
        # links.iter_notes skips the settings file; it is still a note
        # of Anchor's that a link could name.
        index.add(classes.SETTINGS_PATH)

    ordered = sorted(visible)
    kept = ordered[:TREE_MAX_NOTES]
    kept_set = set(kept)
    nodes: list[dict] = []
    edges: list[dict] = []
    for src in kept:
        note = visible[src]
        # Decodes: a note that is not UTF-8 throughout is `unknown`, never visible.
        text = note.data.decode("utf-8")
        nodes.append(_node(src, note, text))
        seen: set[tuple[str, str]] = set()
        for target in links.targets(_linkable(note.data)):
            if _ATTACHMENT_RE.search(target):
                continue
            found = _candidates(target, index)
            if not found:
                key = ("unresolved", unicodedata.normalize("NFC", target.strip()))
                edge = {"src": src, "unresolved": key[1]}
            else:
                chosen = _pick(src, found)
                if chosen == src or (chosen in visible and chosen not in kept_set):
                    continue
                if chosen in kept_set:
                    key = ("dst", chosen)
                    edge = {"src": src, "dst": chosen}
                else:
                    # The key stays in this function; the edge names nothing.
                    key = ("outside", chosen)
                    edge = {"src": src, "outside": True}
            if key in seen:
                continue
            seen.add(key)
            edges.append(edge)
    return {"nodes": nodes, "edges": edges, "truncated": len(ordered) > TREE_MAX_NOTES}
