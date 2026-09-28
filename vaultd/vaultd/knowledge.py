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
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from vaultd import classes, frontmatter, links, paths, provenance, undo
from vaultd.config import FOLDER_MAX_DEPTH, KNOWLEDGE_WRITE_MAX_BYTES, TREE_MAX_NOTES
from vaultd.store import Conflict, Missing, Store, sha256

_ANCHOR_TOP = "Anchor"
_CONTROL = frozenset(chr(c) for c in list(range(0x20)) + [0x7F])
_MAX_SEGMENT_CHARS = 120


class Refused(Exception):
    """Every acceptance failure that is not CAS (412) or missing-on-update (404).

    `reason` is required and must be one of `undo.REFUSAL_REASONS` -- a
    bare `Refused()` is a `TypeError`, so no raise site can skip naming
    which check failed. The HTTP layer (api.py) still turns every one of
    these into the same 403 with an empty body; only the operator log
    sees `reason`.
    """

    def __init__(self, reason: str) -> None:
        if reason not in undo.REFUSAL_REASONS:
            raise ValueError(f"unknown refusal reason: {reason!r}")
        super().__init__(reason)
        self.reason = reason


class MidRenameFailure(Exception):
    """The new path was created but the old one could not be removed.

    Both paths are left in place with identical content -- a duplicate,
    not a loss. The one thing that is certain to have happened --
    creating `new_rel` -- is on `entries`, so the caller can still
    record it in the changeset: undoing that changeset later deletes
    the duplicate (compare-and-swap, like any other undo), which is
    the recovery path. Recovering the old path otherwise means a fresh
    read of it (its hash is no longer known here) or retrying the rename.
    """

    def __init__(self, new_rel: str, entries: list["undo.FileEntry"]) -> None:
        super().__init__(new_rel)
        self.new_rel = new_rel
        self.entries = entries


class RenameRaced(Exception):
    """A backlink file changed between planning and writing it.

    Everything this rename had already written in this request --
    every backlink rewritten so far, and the move itself -- was rolled
    back to its pre-image before this was raised. The vault is exactly
    as it was before the request; the caller answers 412, the same as
    any other compare-and-swap loss.
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_segment(rel: str) -> str:
    return unicodedata.normalize("NFC", rel).split("/", 1)[0]


def candidate_path_reason(rel: str) -> str | None:
    """None if `rel` may hold a knowledge note, else the refusal code that says why not."""
    if not rel.endswith(".md"):
        return "not_md"
    if paths.has_dot_segment(rel):
        return "dot_segment"
    if _first_segment(rel) == _ANCHOR_TOP:
        return "anchor_path"
    return None


def is_candidate_path(rel: str) -> bool:
    """.md, no dot segment, not under `Anchor/`. Symlinks are caught by the walk below."""
    return candidate_path_reason(rel) is None


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
        raise Refused("symlink") from None


def _class_of(rel: str, data: bytes, rules: classes.FolderRules) -> str | None:
    mark = frontmatter.note_mark(data)
    if mark == "unknown":
        return None
    return classes.effective_class(rel, mark, rules).note_class


def _safe_segment(seg: str) -> bool:
    """A new folder segment vaultd may create (rev. 3, plan section 14,
    BUILD item 1d): non-empty, no leading '.', no '/' or '\\', no
    control character, at most 120 chars, and already NFC-normalised --
    vaultd never silently renormalises a name it is about to create, or
    a Cyrillic folder could exist in two different byte forms.

    No separate 'never literally Anchor' check: a *new* segment is only
    ever considered once `plan_new_folders` has already found a
    covering `knowledge_folders` rule for its existing ancestor (below),
    and that rule is always a non-empty path -- so the first new
    segment can never be the very first path segment, which is the only
    position `candidate_path_reason`'s own `Anchor/` exclusion (and
    `is_settings_file`) ever cares about. A `rel` starting with
    `Anchor/` is refused before this function is even called."""
    if not seg or seg.startswith("."):
        return False
    if "/" in seg or "\\" in seg:
        return False
    if any(c in _CONTROL for c in seg):
        return False
    if len(seg) > _MAX_SEGMENT_CHARS:
        return False
    if unicodedata.normalize("NFC", seg) != seg:
        return False
    return True


def _covering_knowledge_rule(ancestor: classes.Segments, rules: classes.FolderRules) -> classes.Segments | None:
    """The most specific `knowledge_folders` rule covering `ancestor`
    (NFC-normalised segments), or None. "Most specific" (the longest
    matching rule) so a nested rule -- `knowledge_folders: [Library,
    Library/Deep]` -- roots a new folder's depth budget at whichever
    rule actually names that ancestor, not an outer one."""
    matches = [rule for rule in rules.knowledge if classes._covers(rule, ancestor)]  # noqa: SLF001 - same package
    return max(matches, key=len) if matches else None


def plan_new_folders(root: Path, rel: str, rules: classes.FolderRules) -> tuple[str, ...]:
    """The new folder rel-paths (shallow to deep) `rel`'s write must
    create, or `()` if its folder already exists. Pure and read-only --
    never creates anything, never touches disk beyond the O_NOFOLLOW
    directory walk -- so a caller can precheck caps and log a refusal
    before any byte is written (rev. 3, plan section 14, BUILD item 1):

    (a) the nearest EXISTING ancestor folder must be covered by a
        `knowledge_folders` rule; that rule's own folder is the "root"
        depth is measured from (c). An ancestor with no such rule --
        including the knowledge root itself, when it was never created
        on disk -- reuses `folder_missing`: there is no knowledge
        ancestor to build from, which reads the same as "the folder is
        missing" from the caller's side, and never creates a top-level
        folder either way.
    (b) the FULL new folder path's own effective class (folder rules
        only) must still resolve to knowledge -- a `never`/`personal`
        rule intercepting a deeper segment refuses with
        `folder_not_under_knowledge`, even though the ancestor in (a)
        was fine.
    (c) depth below that root is at most FOLDER_MAX_DEPTH.
    (d) every new segment is a safe name (`_safe_segment`).
    (e) no symlink anywhere on the way -- `paths.existing_prefix_length`'s
        own O_NOFOLLOW walk.
    """
    parts = rel.split("/")[:-1]
    if not parts:
        raise Refused("folder_missing")
    existing = paths.existing_prefix_length(root, parts)
    if existing == len(parts):
        return ()
    ancestor = tuple(unicodedata.normalize("NFC", p) for p in parts[:existing])
    root_rule = _covering_knowledge_rule(ancestor, rules)
    if root_rule is None:
        raise Refused("folder_missing")
    if classes._folder_class(rel, rules) != "knowledge":  # noqa: SLF001 - same package
        raise Refused("folder_not_under_knowledge")
    if len(parts) - len(root_rule) > FOLDER_MAX_DEPTH:
        raise Refused("folder_too_deep")
    for seg in parts[existing:]:
        if not _safe_segment(seg):
            raise Refused("folder_name_bad")
    return tuple("/".join(parts[: existing + i + 1]) for i in range(len(parts) - existing))


def pending_new_folders(store: Store, rel: str, if_sha256: str | None) -> tuple[str, ...]:
    """The new folders `perform_put` will need to create for this write,
    computed the same way it will -- so `PUT /v1/knowledge`'s handler
    can precheck the folder caps and record what was created for undo
    before any byte is written. `()` for an update (`if_sha256` is not
    None -- an existing file's folder always already exists) or a
    create whose folder already exists; any other refusal here is one
    `perform_put` would raise anyway, just discovered earlier."""
    if if_sha256 is not None:
        return ()
    if candidate_path_reason(rel) is not None:
        return ()
    root = store.vault_path
    rules = classes.load_rules(root)
    if rules.state == "invalid":
        return ()
    _current, folder_exists = _read_at(root, rel)
    if folder_exists:
        return ()
    return plan_new_folders(root, rel, rules)


def remove_folder_if_empty(store: Store, rel: str) -> bool:
    """rmdir `rel` if it is still empty, O_NOFOLLOW, never recursive
    (rev. 3, plan section 14). Left in place -- silently, no error -- if
    it no longer exists, a symlink sits anywhere on the way, or it is
    not empty (a later write, or one of your own files, landed in it):
    undo must never delete something it did not itself create there."""
    parts = rel.split("/")
    parent_parts, name = parts[:-1], parts[-1]
    try:
        with paths.open_root(store.vault_path) as root_fd:
            parent_fd = paths.open_dir(root_fd, parent_parts)
            if parent_fd is None:
                return False
            try:
                os.rmdir(name, dir_fd=parent_fd)
                return True
            except OSError:
                return False
            finally:
                os.close(parent_fd)
    except paths.Refused:
        return False


def check_new_content(rel: str, data: bytes, rules: classes.FolderRules, *, for_create: bool) -> None:
    """Raise Refused unless `data` may become the note at `rel`."""
    if len(data) > KNOWLEDGE_WRITE_MAX_BYTES:
        raise Refused("too_large")
    mark = frontmatter.note_mark(data)
    if mark == "unknown":
        raise Refused("bad_frontmatter")
    if for_create:
        if classes._folder_class(rel, rules) != "knowledge":  # noqa: SLF001 - same package
            raise Refused("folder_not_knowledge")
        if classes.effective_class(rel, mark, rules).note_class != "knowledge":
            raise Refused("content_not_knowledge")


def perform_put(
    store: Store, rel: str, content: str, if_sha256: str | None, *, now: Callable[[], str] = _now_iso
) -> tuple[str, bytes | None]:
    """Validate and write one knowledge note. Returns (new_sha256, pre_image).

    Runs entirely under the caller's lock, in one call, so the CAS
    window is the same as any other vaultd write.
    """
    path_reason = candidate_path_reason(rel)
    if path_reason is not None:
        raise Refused(path_reason)
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise Refused("bad_utf8") from None

    root = store.vault_path
    rules = classes.load_rules(root)
    if rules.state == "invalid":
        raise Refused("settings_invalid")
    current, folder_exists = _read_at(root, rel)

    if if_sha256 is None:
        if current is not None:
            raise Refused("name_taken")
        if not folder_exists:
            # Validates eligibility only (rev. 3); the actual mkdir
            # happens below, inside `store.put_unchecked`'s own
            # O_NOFOLLOW `create=True` walk, once every check has
            # passed. `api.py` has already called this same pure
            # function itself, before this thread ever started, to
            # precheck the folder caps and record what it is about to
            # create for undo.
            plan_new_folders(root, rel, rules)
        check_new_content(rel, data, rules, for_create=True)
        pre_image = None
        old_anchor = None
    else:
        if current is None:
            raise Missing
        if _class_of(rel, current, rules) != "knowledge":
            raise Refused("not_knowledge")
        if sha256(current) != if_sha256:
            raise Conflict
        check_new_content(rel, data, rules, for_create=False)
        old_anchor = frontmatter.raw_anchor(current)
        pre_image = current

    if if_sha256 is not None and old_anchor != frontmatter.raw_anchor(data):
        raise Refused("reclassify")  # a write cannot reclassify -- creating a file has no "old" to preserve

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
    # New folder rel-paths (shallow to deep) the destination needs (rev. 3); () if
    # `new_rel`'s folder already exists.
    new_folders: tuple[str, ...] = ()


def plan_rename(root: Path, old_rel: str, new_rel: str, if_sha256: str) -> RenamePlan:
    """Validate a rename and find every backlink. Touches no disk beyond reading."""
    old_reason = candidate_path_reason(old_rel)
    if old_reason is not None:
        raise Refused(old_reason)
    new_reason = candidate_path_reason(new_rel)
    if new_reason is not None:
        raise Refused(new_reason)
    if old_rel == new_rel:
        raise Refused("same_path")

    old_data, _ = _read_at(root, old_rel)
    if old_data is None:
        raise Missing
    if sha256(old_data) != if_sha256:
        raise Conflict

    rules = classes.load_rules(root)
    if rules.state == "invalid":
        raise Refused("settings_invalid")
    old_mark = frontmatter.note_mark(old_data)
    if old_mark == "unknown" or classes.effective_class(old_rel, old_mark, rules).note_class != "knowledge":
        raise Refused("not_knowledge")

    new_current, new_folder_exists = _read_at(root, new_rel)
    new_folders: tuple[str, ...] = ()
    if not new_folder_exists:
        # Validates eligibility only (rev. 3); same shape as
        # `perform_put`'s own call -- the destination folder is
        # actually created later, inside `perform_rename`'s
        # `store.put_unchecked` call.
        new_folders = plan_new_folders(root, new_rel, rules)
    if new_current is not None:
        raise Refused("dest_taken")
    if classes._folder_class(new_rel, rules) != "knowledge":  # noqa: SLF001
        raise Refused("dest_not_knowledge")
    # No separate check of the destination's resolved class: the guard
    # above already proved `old_mark` is "knowledge" or "none" (nothing
    # else can pass it), and combined with a folder rule of "knowledge"
    # -- just proved too -- `effective_class` always resolves the pair
    # to "knowledge" (stricter-wins can only raise the rank, and there
    # is nothing above knowledge left to raise it to).

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
        if _first_segment(rel) == _ANCHOR_TOP:
            raise Refused("linked_from_non_knowledge")  # a fact/journal page (or any other Anchor file) links here
        note_class = _class_of(rel, data, rules)
        if note_class != "knowledge":
            raise Refused("linked_from_non_knowledge")  # a personal/never/unclassified note links here
        backlinks.append((rel, data, new_text))
    if ambiguous:
        raise Refused("ambiguous_basename")  # the basename is ambiguous: another file already has it

    return RenamePlan(old_rel, new_rel, old_data, if_sha256, tuple(backlinks), new_folders)


def perform_rename(store: Store, plan: RenamePlan, *, now: Callable[[], str] = _now_iso) -> list[undo.FileEntry]:
    """Move the file and rewrite its backlinks. Returns the changeset's file entries.

    Create-only at the new path, then remove the old one -- the same
    shape as any other create-only write (store.py). If the removal
    fails after the link succeeded, both paths are left in place
    (`MidRenameFailure`): a duplicate, never a loss.

    Every backlink write after that is compare-and-swapped against the
    hash `plan_rename` read. If one has changed since -- `ob` landed a
    sync in the gap between planning and writing -- this does not leave
    a half-renamed vault: every backlink already rewritten in this call,
    and the move itself, are rolled back to their pre-images, and
    `RenameRaced` is raised. The vault ends exactly where it started.
    """
    try:
        new_sha = store.put_unchecked(plan.new_rel, plan.old_data, None)
    except Conflict as exc:
        # Nothing was written yet: an ordinary refusal (a race filled the
        # destination), not a mid-rename state.
        raise Refused("dest_taken") from exc
    except paths.Refused as exc:
        # Nothing was written yet: a race turned the destination into a
        # symlink, not a mid-rename state.
        raise Refused("symlink") from exc
    try:
        store.delete_unchecked(plan.old_rel, plan.old_sha256)
    except (Missing, Conflict, paths.Refused) as exc:
        raise MidRenameFailure(plan.new_rel, [undo.FileEntry(plan.new_rel, None, new_sha)]) from exc

    entries = [
        undo.FileEntry(plan.new_rel, None, new_sha),
        undo.FileEntry(plan.old_rel, plan.old_data, None),
    ]
    when = now()
    written_backlinks: list[tuple[str, bytes, str]] = []
    for rel, old_content, new_text in plan.backlinks:
        stamped = provenance.apply(new_text.encode("utf-8"), when)
        try:
            written = store.put_unchecked(rel, stamped, sha256(old_content))
        except (Conflict, Missing, paths.Refused):
            _rollback_rename(store, plan, new_sha, written_backlinks)
            raise RenameRaced from None
        written_backlinks.append((rel, old_content, written))
        entries.append(undo.FileEntry(rel, old_content, written))
    return entries


def _rollback_rename(
    store: Store, plan: RenamePlan, new_sha: str, written_backlinks: list[tuple[str, bytes, str]]
) -> None:
    """Best-effort undo of everything `perform_rename` had already written.

    Same shape as `undo_one`: each file is put back only by compare-
    and-swap against what this call itself just wrote there, never
    unconditionally. If one of these somehow also fails (another
    concurrent write, vanishingly unlikely on top of the first race),
    that one file is left as it is rather than raising a second time --
    `RenameRaced` still fires, so the write is refused either way, and
    the caller can `GET /v1/knowledge` to see exactly what is on disk.
    """
    for rel, old_content, written_hash in reversed(written_backlinks):
        try:
            store.put_unchecked(rel, old_content, written_hash)
        except (Conflict, Missing, paths.Refused):
            pass
    try:
        store.delete_unchecked(plan.new_rel, new_sha)
    except (Missing, Conflict, paths.Refused):
        pass
    try:
        store.put_unchecked(plan.old_rel, plan.old_data, None)
    except (Conflict, paths.Refused):
        pass


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


# -- GET /v1/knowledge/tree: what Claude sees before it writes (rev. 3) -----

_TREE_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _read_regular_by_full_path(full: str) -> bytes | None:
    """A defensive, re-checked O_NOFOLLOW read of a file `os.scandir`
    already found -- the same TOCTOU guard `links.py`'s own walk uses,
    duplicated locally rather than imported (each walker here keeps its
    own copy of this tiny helper, matching paths.py/links.py/manifest.py)."""
    try:
        fd = os.open(full, _TREE_FILE_FLAGS)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def build_tree(root: Path, rules: classes.FolderRules) -> dict:
    """`GET /v1/knowledge/tree`'s body (rev. 3, BUILD item 4): every
    folder under a `knowledge_folders` root whose own class is
    knowledge, and every `.md` note whose *effective* class (folder
    rule plus its own property, stricter wins) is knowledge -- a note
    marked personal/never inside a knowledge folder never appears, and
    neither does anything under `Anchor/`. No file content, ever.
    Capped at TREE_MAX_NOTES notes, with `truncated: true` past it.
    Invalid settings: empty lists, same as everywhere else in this
    module."""
    if rules.state == "invalid":
        return {"folders": [], "notes": [], "truncated": False}
    folders: list[str] = []
    notes: list[dict] = []
    truncated = False

    def walk(directory: Path, prefix: str) -> None:
        nonlocal truncated
        try:
            listing = list(os.scandir(directory))
        except OSError:
            return
        for entry in sorted(listing, key=lambda e: e.name):
            if entry.name.startswith("."):
                continue
            rel = prefix + entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if _first_segment(rel) == _ANCHOR_TOP:
                        continue
                    if classes._folder_class(rel + "/x", rules) == "knowledge":  # noqa: SLF001
                        folders.append(rel)
                    walk(Path(entry.path), rel + "/")
                    continue
                if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".md"):
                    continue
            except OSError:
                continue
            if classes.is_settings_file(rel) or _first_segment(rel) == _ANCHOR_TOP:
                continue
            data = _read_regular_by_full_path(entry.path)
            if data is None:
                continue
            mark = frontmatter.note_mark(data)
            if mark == "unknown":
                continue
            resolved = classes.effective_class(rel, mark, rules)
            if resolved.note_class != "knowledge":
                continue
            if len(notes) >= TREE_MAX_NOTES:
                truncated = True
                continue
            notes.append({"path": rel, "title": rel.rsplit("/", 1)[-1][: -len(".md")]})

    walk(root, "")
    return {"folders": folders, "notes": notes, "truncated": truncated}
