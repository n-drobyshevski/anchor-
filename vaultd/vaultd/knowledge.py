"""The class boundary Claude cannot cross (write-plan section 4).

Every acceptance check named in the plan runs here, inside the store's
single lock, before a byte is written:

1. **The path**: `.md` only, no dot segment, never under `Anchor/`,
   reached without a symlink (paths.py's O_NOFOLLOW walk).
2. **The existing file's effective class is `knowledge`** -- folder
   rules and the note's own property, stricter wins (classes.py),
   exactly as the manifest resolves it.
3. **A write cannot reclassify**: the new content's `anchor:` property
   must equal the old one, or both must be absent.
4. **A new file** goes only into an *existing* folder whose own rule is
   `knowledge`, and the new content's own class must also resolve to
   `knowledge`.
5. **Size, UTF-8, frontmatter.** At most `KNOWLEDGE_WRITE_MAX_BYTES`;
   valid UTF-8; frontmatter that parses with vaultd's strict loader, or
   none at all.

Every failure above raises `Refused`. The API layer turns that into the
one 403 with an empty body the plan asks for; `Missing` and the
store's own `Conflict` keep their usual 404/412, because those already
tell the bot nothing the class boundary is trying to hide.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from vaultd import classes, frontmatter, links, paths, provenance, undo
from vaultd.config import KNOWLEDGE_WRITE_MAX_BYTES
from vaultd.store import Conflict, Missing, Store, sha256

_ANCHOR_TOP = "Anchor"


class Refused(Exception):
    """Every acceptance failure that is not CAS (412) or missing-on-update (404)."""


class MidRenameFailure(Exception):
    """The new path was created but the old one could not be removed.

    Both paths are left in place with identical content -- a duplicate,
    not a loss. Recovering means removing the old path once its own
    hash is known again (a fresh read), or retrying the rename.
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_segment(rel: str) -> str:
    return unicodedata.normalize("NFC", rel).split("/", 1)[0]


def is_candidate_path(rel: str) -> bool:
    """.md, no dot segment, not under `Anchor/`. Symlinks are caught by the walk below."""
    return rel.endswith(".md") and not paths.has_dot_segment(rel) and _first_segment(rel) != _ANCHOR_TOP


def _read_at(root: Path, rel: str) -> tuple[bytes | None, bool]:
    """(bytes or None, parent folder exists), via the O_NOFOLLOW walk.

    A symlink anywhere on the way -- the folder, or the file itself --
    is `paths.Refused`, which reads exactly like "not found here":
    the caller ends up refusing the write, never following the link.
    """
    parts = rel.split("/")
    try:
        with paths.open_root(root) as root_fd:
            dir_fd = paths.open_dir(root_fd, parts[:-1])
            if dir_fd is None:
                return None, False
            try:
                return paths.read_regular_at(dir_fd, parts[-1]), True
            finally:
                os.close(dir_fd)
    except paths.Refused:
        raise Refused from None


def _class_of(rel: str, data: bytes, rules: classes.FolderRules) -> str | None:
    mark = frontmatter.note_mark(data)
    if mark == "unknown":
        return None
    return classes.effective_class(rel, mark, rules).note_class


def check_new_content(rel: str, data: bytes, rules: classes.FolderRules, *, for_create: bool) -> None:
    """Raise Refused unless `data` may become the note at `rel`."""
    if len(data) > KNOWLEDGE_WRITE_MAX_BYTES:
        raise Refused
    mark = frontmatter.note_mark(data)
    if mark == "unknown":
        raise Refused
    if for_create:
        if classes._folder_class(rel, rules) != "knowledge":  # noqa: SLF001 - same package
            raise Refused
        if classes.effective_class(rel, mark, rules).note_class != "knowledge":
            raise Refused


def perform_put(
    store: Store, rel: str, content: str, if_sha256: str | None, *, now: Callable[[], str] = _now_iso
) -> tuple[str, bytes | None]:
    """Validate and write one knowledge note. Returns (new_sha256, pre_image).

    Runs entirely under the caller's lock, in one call, so the CAS
    window is the same as any other vaultd write.
    """
    if not is_candidate_path(rel):
        raise Refused
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise Refused from None

    root = store.vault_path
    rules = classes.load_rules(root)
    current, folder_exists = _read_at(root, rel)

    if if_sha256 is None:
        if current is not None:
            raise Refused  # the name is taken
        if not folder_exists:
            raise Refused  # only into an existing folder
        check_new_content(rel, data, rules, for_create=True)
        pre_image = None
        old_anchor = None
    else:
        if current is None:
            raise Missing
        if _class_of(rel, current, rules) != "knowledge":
            raise Refused
        if sha256(current) != if_sha256:
            raise Conflict
        check_new_content(rel, data, rules, for_create=False)
        old_anchor = frontmatter.raw_anchor(current)
        pre_image = current

    if if_sha256 is not None and old_anchor != frontmatter.raw_anchor(data):
        raise Refused  # a write cannot reclassify -- creating a file has no "old" to preserve

    stamped = provenance.apply(data, now())
    new_sha = store.put_unchecked(rel, stamped, if_sha256)
    return new_sha, pre_image


@dataclass(frozen=True)
class RenamePlan:
    old_rel: str
    new_rel: str
    old_data: bytes
    old_sha256: str
    # (rel, current bytes, rewritten text) for every knowledge note that links here.
    backlinks: tuple[tuple[str, bytes, str], ...]


def plan_rename(root: Path, old_rel: str, new_rel: str, if_sha256: str) -> RenamePlan:
    """Validate a rename and find every backlink. Touches no disk beyond reading."""
    if not is_candidate_path(old_rel) or not is_candidate_path(new_rel):
        raise Refused
    if old_rel == new_rel:
        raise Refused

    old_data, _ = _read_at(root, old_rel)
    if old_data is None:
        raise Missing
    if sha256(old_data) != if_sha256:
        raise Conflict

    rules = classes.load_rules(root)
    old_mark = frontmatter.note_mark(old_data)
    if old_mark == "unknown" or classes.effective_class(old_rel, old_mark, rules).note_class != "knowledge":
        raise Refused

    new_current, new_folder_exists = _read_at(root, new_rel)
    if not new_folder_exists:
        raise Refused
    if new_current is not None:
        raise Refused  # destination taken
    if classes._folder_class(new_rel, rules) != "knowledge":  # noqa: SLF001
        raise Refused
    if classes.effective_class(new_rel, old_mark, rules).note_class != "knowledge":
        raise Refused

    old_base = links.basename(old_rel)
    new_base = links.basename(new_rel)

    ambiguous = False
    backlinks: list[tuple[str, bytes, str]] = []
    for rel, data in links.iter_notes(root):
        if rel == old_rel:
            continue
        if links.basename(rel) == old_base:
            ambiguous = True
        try:
            new_text, count = links.rewrite(data, old_base, new_base)
        except UnicodeDecodeError:
            continue
        if count == 0:
            continue
        note_class = _class_of(rel, data, rules)
        if note_class != "knowledge":
            raise Refused  # a personal/never/unclassified note links here
        backlinks.append((rel, data, new_text))
    if ambiguous:
        raise Refused  # the basename is ambiguous: another file already has it

    return RenamePlan(old_rel, new_rel, old_data, if_sha256, tuple(backlinks))


def perform_rename(store: Store, plan: RenamePlan, *, now: Callable[[], str] = _now_iso) -> list[undo.FileEntry]:
    """Move the file and rewrite its backlinks. Returns the changeset's file entries.

    Create-only at the new path, then remove the old one -- the same
    shape as any other create-only write (store.py). If the removal
    fails after the link succeeded, both paths are left in place
    (MidRenameFailure): a duplicate, never a loss.
    """
    try:
        new_sha = store.put_unchecked(plan.new_rel, plan.old_data, None)
    except (Conflict, paths.Refused) as exc:
        # Nothing was written yet: an ordinary refusal (a race filled the
        # destination, or turned it into a symlink), not a mid-rename state.
        raise Refused from exc
    try:
        store.delete_unchecked(plan.old_rel, plan.old_sha256)
    except (Missing, Conflict, paths.Refused) as exc:
        raise MidRenameFailure(plan.new_rel) from exc

    entries = [
        undo.FileEntry(plan.new_rel, None, new_sha),
        undo.FileEntry(plan.old_rel, plan.old_data, None),
    ]
    when = now()
    for rel, old_content, new_text in plan.backlinks:
        stamped = provenance.apply(new_text.encode("utf-8"), when)
        try:
            written = store.put_unchecked(rel, stamped, sha256(old_content))
        except (Conflict, Missing, paths.Refused):
            # That backlink file changed since the plan was read. Stop
            # here rather than write over it; everything already done
            # is still recorded, so it stays undoable.
            break
        entries.append(undo.FileEntry(rel, old_content, written))
    return entries


def undo_one(store: Store, entry: undo.FileEntry) -> tuple[bool, undo.FileEntry | None]:
    """Restore one file from its stored pre-image. (restored?, the undo's own record)."""
    try:
        if entry.written_sha256 is None:
            if entry.pre_image is None:
                return True, None
            store.put_unchecked(entry.path, entry.pre_image, None)
            return True, undo.FileEntry(entry.path, None, sha256(entry.pre_image))
        if entry.pre_image is None:
            store.delete_unchecked(entry.path, entry.written_sha256)
            return True, undo.FileEntry(entry.path, None, None)
        store.put_unchecked(entry.path, entry.pre_image, entry.written_sha256)
        return True, undo.FileEntry(entry.path, None, sha256(entry.pre_image))
    except (Conflict, Missing, paths.Refused):
        return False, None
