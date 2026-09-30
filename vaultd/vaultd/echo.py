"""Echo's own writer: one new knowledge note in the inbox (lens L4).

anchor-lens-plan.md sections 9, 13 and 14.5, the L4 spec section 5.
When the user adopts the results of a lens research («в Inbox»), the
bot sends one note to `PUT /v1/echo/inbox`. This module holds every
check that route makes, all of them inside the store's single lock and
before a byte is written:

1. **The inbox exists.** `Anchor/settings.md` is valid and resolves an
   inbox (classes.py's `echo_inbox`: the explicit value, or the default
   `Echo/Inbox` when it conflicts with no rule). No settings file, an
   unusable one or a conflicting default: `inbox_unavailable` (or
   `settings_invalid`). Echo never guesses a folder.
2. **vaultd builds the path.** The bot sends a bare `.md` basename,
   never a path: no `/`, no leading `.`, no control character, NFC,
   at most 120 characters (knowledge.py's `_safe_segment`). The note
   lands directly in the inbox; there is no subfolder, `../` or other
   folder to name. Anything else is `bad_name`.
3. **The content is Echo's shape and nothing else.** At most
   `ECHO_WRITE_MAX_BYTES`, UTF-8, frontmatter that vaultd's strict
   loader parses, whose keys are exactly `anchor`, `source_urls` and
   `gap` (`frontmatter_keys` otherwise: no `aliases`, no provenance
   of its own, no `anchor_edited_*`), with `anchor: knowledge`
   (`content_not_knowledge` otherwise: never `lens`, which only the
   user sets) and `source_urls` a list of strings, `gap` a positive
   integer. The note's resolved class must be knowledge. The check is
   made again on the stamped bytes: the stamp lengthens the
   frontmatter, and a block pushed past `FRONTMATTER_MAX_BYTES` would
   leave a note vaultd itself could no longer read as knowledge.
4. **Create-only.** `os.link` onto the target (store.py), trying
   `name`, then `name 2` up to `name 9`, all under the lock; a name
   taken nine times over (`name`, then `name 2` to `name 9`) is
   `name_taken`. There is no Echo update,
   rename or delete: only undo removes what Echo wrote.
5. **Only the inbox's own missing folders** are created (say, `Echo`
   and `Echo/Inbox` on the first adoption), each a safe segment, with
   no symlink on the way, and recorded in the changeset so undo removes
   them again if they are still empty.

Every note is stamped `anchor_edited_by: echo` (provenance.py) and
recorded in the undo store as an `echo` changeset (undo.py), under
Echo's own caps. A replayed changeset -- the bot retrying a write whose
answer it lost -- returns the note it already made, `replayed: true`,
and writes nothing.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from vaultd import classes, frontmatter, paths, provenance
from vaultd.config import ECHO_NAME_SUFFIX_MAX, ECHO_WRITE_MAX_BYTES
from vaultd.knowledge import Refused, _safe_segment
from vaultd.store import Conflict, Store

FRONTMATTER_KEYS = frozenset({frontmatter.MARK_KEY, "source_urls", "gap"})
_SUFFIX = ".md"


@dataclass(frozen=True)
class EchoPlan:
    """A checked write: the inbox folder, the stamped bytes, the
    candidate names in order, and the folders the write must create."""

    inbox: str
    data: bytes
    names: tuple[str, ...]
    new_folders: tuple[str, ...]


def inbox_rel(rules: classes.FolderRules) -> str | None:
    """The inbox as a vault-relative folder, or None when there is none."""
    if rules.state != "ok" or rules.echo_inbox is None:
        return None
    return "/".join(rules.echo_inbox)


def _check_name(name: str) -> None:
    if not name.endswith(_SUFFIX) or not name[: -len(_SUFFIX)].strip():
        raise Refused("bad_name")
    if not _safe_segment(name):
        raise Refused("bad_name")


def candidate_names(name: str) -> tuple[str, ...]:
    """`name`, then `name 2` .. `name 9`, each still a safe segment."""
    stem = name[: -len(_SUFFIX)]
    names = [name]
    for n in range(2, ECHO_NAME_SUFFIX_MAX + 1):
        candidate = f"{stem} {n}{_SUFFIX}"
        if _safe_segment(candidate):
            names.append(candidate)
    return tuple(names)


def check_content(data: bytes) -> None:
    """Raise Refused unless `data` is an Echo note (module docstring, 3)."""
    if len(data) > ECHO_WRITE_MAX_BYTES:
        raise Refused("too_large")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("bad_utf8") from None
    if frontmatter.note_mark(data) == "unknown":
        raise Refused("bad_frontmatter")
    loaded = frontmatter.load(data)
    if loaded is None or set(loaded) != FRONTMATTER_KEYS:
        raise Refused("frontmatter_keys")
    urls, gap = loaded["source_urls"], loaded["gap"]
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        raise Refused("frontmatter_keys")
    if not isinstance(gap, int) or isinstance(gap, bool) or gap <= 0:
        raise Refused("frontmatter_keys")
    if loaded[frontmatter.MARK_KEY] != "knowledge":
        raise Refused("content_not_knowledge")


def _missing_folders(root: Path, inbox: str) -> tuple[str, ...]:
    """The inbox's folders that do not exist yet, shallow to deep. A
    symlink anywhere on the way is refused, as on every other route."""
    parts = inbox.split("/")
    try:
        existing = paths.existing_prefix_length(root, parts)
    except paths.Refused:
        raise Refused("symlink") from None
    for seg in parts[existing:]:
        if not _safe_segment(seg):
            raise Refused("folder_name_bad")
    return tuple("/".join(parts[: i + 1]) for i in range(existing, len(parts)))


def plan(store: Store, name: str, content: str, *, now: Callable[[], str]) -> EchoPlan:
    """Every check, in order, with nothing written. Raises Refused."""
    rules = classes.load_rules(store.vault_path)
    if rules.state == "invalid":
        raise Refused("settings_invalid")
    inbox = inbox_rel(rules)
    if inbox is None:
        raise Refused("inbox_unavailable")
    _check_name(name)
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise Refused("bad_utf8") from None
    check_content(data)
    rel = f"{inbox}/{name}"
    if classes.effective_class(rel, "knowledge", rules).note_class != "knowledge":
        raise Refused("content_not_knowledge")
    new_folders = _missing_folders(store.vault_path, inbox)
    stamped = provenance.apply(data, now(), by="echo")
    # The stamp adds ~66 bytes inside the frontmatter: content whose block
    # closed just under FRONTMATTER_MAX_BYTES would pass check_content and
    # then be written as a note vaultd cannot parse (module docstring, 3).
    if frontmatter.note_mark(stamped) != "knowledge" or frontmatter.load(stamped) is None:
        raise Refused("bad_frontmatter")
    return EchoPlan(inbox=inbox, data=stamped, names=candidate_names(name), new_folders=new_folders)


def perform(store: Store, planned: EchoPlan) -> tuple[str, str]:
    """Create the note under the first free name. Returns (rel, sha256).

    Create-only at every candidate: `store.put_unchecked` with no
    expected hash links the temp file onto the name and fails with
    Conflict if it exists, so a name `ob` fills between two tries is
    simply skipped. Its O_NOFOLLOW walk creates the inbox's missing
    folders on the first try, and refuses a symlink."""
    for candidate in planned.names:
        rel = f"{planned.inbox}/{unicodedata.normalize('NFC', candidate)}"
        try:
            sha = store.put_unchecked(rel, planned.data, None)
        except Conflict:
            continue
        except paths.Refused:
            raise Refused("symlink") from None
        return rel, sha
    raise Refused("name_taken")
