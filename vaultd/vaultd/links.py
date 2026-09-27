"""Obsidian-style wikilinks, resolved by basename (write-plan section 4, "Rename and backlinks").

Obsidian links a note by its **basename**, not its full path: `[[Old]]`
finds `Old.md` wherever it lives, and links break silently if two notes
share a basename -- which is exactly why an ambiguous basename refuses
a rename rather than guessing. The four forms this module knows:
`[[old]]`, `[[old|label]]`, `[[old#heading]]` and `![[old]]` (an
embed). A `[[folder/old]]` path form is also recognised, by resolving
its own basename the same way a plain `[[old]]` resolves to `old.md`;
this is "resolve reasonably" from the plan, not full Obsidian path
resolution (a relative link from a different subtree, or a link that
already carries an unambiguous path, is matched on basename alone, and
the folder prefix is dropped when the link is rewritten).

This module never returns a personal note's content to a caller; it is
used only inside vaultd's rename planning (knowledge.py), which reads
every note's bytes solely to search for links and to compute its
class, and both stay in process.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Iterator

from vaultd import classes

_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

# ![[target]] | [[target#heading|label]], heading and label both optional,
# label (if any) always last. The interior is parsed by hand below so that
# a `#` or `|` inside a heading/label round-trips untouched.
_LINK_RE = re.compile(r"(?P<bang>!)?\[\[(?P<inner>[^\[\]]+)\]\]")


def basename(rel: str) -> str:
    """The note's link-target name: the last path segment, minus `.md`, NFC."""
    nfc = unicodedata.normalize("NFC", rel)
    tail = nfc.rsplit("/", 1)[-1]
    if tail.endswith(".md"):
        tail = tail[:-3]
    return tail


def _target_basename(target: str) -> str:
    t = target.strip()
    if t.endswith(".md"):
        t = t[:-3]
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    return unicodedata.normalize("NFC", t)


def _parse_inner(inner: str) -> tuple[str, str | None, str | None]:
    """(target, heading, label), each exactly as written between the brackets."""
    if "|" in inner:
        head, label = inner.split("|", 1)
    else:
        head, label = inner, None
    if "#" in head:
        target, heading = head.split("#", 1)
    else:
        target, heading = head, None
    return target, heading, label


def rewrite(data: bytes, old_basename: str, new_basename: str) -> tuple[str, int]:
    """Retarget every wikilink to `old_basename`, preserving `|label` and `#heading`.

    Returns the rewritten text (decoded; callers re-encode) and how many
    links were changed. Raises UnicodeDecodeError if `data` is not UTF-8 --
    callers treat that as "does not link here", since a non-UTF-8 note
    cannot hold a wikilink vaultd can read.
    """
    text = data.decode("utf-8")
    count = 0

    def repl(match: re.Match) -> str:
        nonlocal count
        target, heading, label = _parse_inner(match.group("inner"))
        if _target_basename(target) != old_basename:
            return match.group(0)
        count += 1
        inner = new_basename
        if heading is not None:
            inner += "#" + heading
        if label is not None:
            inner += "|" + label
        bang = match.group("bang") or ""
        return f"{bang}[[{inner}]]"

    return _LINK_RE.sub(repl, text), count


def iter_notes(root: Path) -> Iterator[tuple[str, bytes]]:
    """Every `.md` file's (rel path, bytes) in the whole vault, `Anchor/` included.

    Rename must refuse when ANY non-knowledge file links to the note
    being renamed, and the write-plan is explicit that this covers
    `Anchor/`'s own fact and journal pages, not only ordinary notes: a
    fact file that happens to hold `[[Old]]` is exactly the kind of
    outside link a rename must not silently leave dangling, or rewrite
    without the same scrutiny a personal note gets. `Anchor/settings.md`
    is skipped (it is not a note Obsidian links to), and so are
    dot-folders/files and symlinks, the same set the manifest walks.

    Every file this yields, Anchor's own included, stays in-process:
    `knowledge.plan_rename` reads it only to search for a link and to
    compute its class, and neither its path nor its bytes are ever
    returned from the API.
    """
    yield from _walk(root, "")


def _walk(directory: Path, prefix: str) -> Iterator[tuple[str, bytes]]:
    try:
        listing = list(os.scandir(directory))
    except OSError:
        return
    for entry in listing:
        if entry.name.startswith("."):
            continue
        rel = prefix + entry.name
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _walk(Path(entry.path), rel + "/")
                continue
            if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".md"):
                continue
        except OSError:
            continue
        if classes.is_settings_file(rel):
            continue
        data = _read_regular(entry.path)
        if data is not None:
            yield rel, data


def _read_regular(full: str) -> bytes | None:
    """Re-checked, O_NOFOLLOW read of a file already found by scandir.

    A defensive re-check against the same TOCTOU the manifest guards
    against: `entry.is_symlink()` in the caller was already false, but
    nothing stops it from becoming one before this open.
    """
    try:
        fd = os.open(full, _FILE_FLAGS)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
