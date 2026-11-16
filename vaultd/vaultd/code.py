"""The code window's read side (anchor-code-window-plan.md section 3).

Two read-only routes, `POST /v1/code/tree` and `POST /v1/code/file`,
through which the bot relays notes to Claude Code while the user has a
window open. They exist only when `VAULT_CODE_ACCESS` is on (api.py
does not register them otherwise). This module guarantees:

- **One decision.** Whether a note may be served is
  `classes.code_class`, for the tree and the file alike, so a file the
  tree lists is a file the file route serves, and nothing else is.
  `never` (property or folder rule), `Anchor/` in any case, dot
  segments, non-`.md`, non-UTF-8 and files over CODE_FILE_MAX_BYTES
  stay closed whatever the bot asks.
- **No symlink is followed.** Every folder is opened relative to its
  parent's descriptor with `O_NOFOLLOW` (paths.py), roots included, and
  every file's size and mtime come from the `fstat` of the descriptor
  that is then read.
- **Roots confine by path segment**, after NFC on both sides: `Notes`
  covers `Notes/a.md`, never `Notes2/a.md`. An empty list is the whole
  vault. A root that does not exist, is not a folder, crosses a
  symlink, or is itself closed contributes nothing, so a root cannot be
  used to probe.
- **Fail closed on the settings file.** When `Anchor/settings.md` is
  invalid, or only a wrong-case name of it exists, a `never_folders`
  rule the user wrote is not being applied: both routes raise
  `SettingsUnusable` (409) and serve nothing. A missing file is fine
  (no folder rules; a note's own `anchor: never` still closes it).
- **Nothing is cached or logged** here: no path, root or text. The
  caller logs counts only.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from vaultd import classes, paths
from vaultd.config import CODE_FILE_MAX_BYTES, CODE_TREE_MAX


class SettingsUnusable(Exception):
    """409: the settings file is invalid or present only in the wrong case."""


def _usable_rules(root: Path) -> classes.FolderRules:
    rules = classes.load_rules(root)
    if rules.state == "invalid":
        raise SettingsUnusable
    if rules.state == "absent" and classes.wrong_case_settings_present(root):
        raise SettingsUnusable
    return rules


def _nfc_segments(rel: str) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", rel).split("/"))


def under_roots(rel: str, roots: list[str]) -> bool:
    """`rel` lies strictly inside one of `roots`, segment by segment
    (module docstring); any path when `roots` is empty."""
    if not roots:
        return True
    segs = _nfc_segments(rel)
    for root in roots:
        prefix = _nfc_segments(root)
        if len(prefix) < len(segs) and segs[: len(prefix)] == prefix:
            return True
    return False


def _closed_folder(rel: str) -> bool:
    """A folder no code route walks into: a dot segment, or `Anchor/`."""
    return paths.has_dot_segment(rel) or classes.is_anchor_path(rel)


def _mtime_iso(st: os.stat_result) -> str:
    return datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_at(dir_fd: int, name: str) -> tuple[bytes, os.stat_result] | None:
    """A regular file's bytes and `fstat`, or None when it is absent, a
    symlink, not a regular file, or over CODE_FILE_MAX_BYTES (checked
    on the descriptor before a byte is read)."""
    try:
        fd = paths.open_regular_at(dir_fd, name)
    except paths.Refused:
        return None
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        if st.st_size > CODE_FILE_MAX_BYTES:
            return None
        return paths.read_all(fd), st
    finally:
        os.close(fd)


def _walk(dir_fd: int, prefix: str, rules: classes.FolderRules, found: dict[str, dict]) -> None:
    try:
        with os.scandir(dir_fd) as it:
            listing = list(it)
    except OSError:
        return
    for entry in listing:
        name = entry.name
        if name.startswith("."):
            continue
        rel = prefix + name
        try:
            if entry.is_symlink():
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            if _closed_folder(rel):
                continue
            try:
                sub = paths.open_dir(dir_fd, [name])
            except paths.Refused:
                continue
            if sub is None:
                continue
            try:
                _walk(sub, rel + "/", rules, found)
            finally:
                os.close(sub)
            continue
        if not name.endswith(".md") or rel in found:
            continue
        got = _read_at(dir_fd, name)
        if got is None:
            continue
        data, st = got
        note_class = classes.code_class(rel, data, rules)
        if note_class is None:
            continue
        found[rel] = {"path": rel, "class": note_class, "bytes": len(data), "mtime": _mtime_iso(st)}


def build_tree(root: Path, roots: list[str]) -> dict:
    """`POST /v1/code/tree`'s body: every `.md` under `roots` that
    `classes.code_class` allows, sorted by path, at most CODE_TREE_MAX,
    with `truncated` saying whether the cap cut it. No content."""
    rules = _usable_rules(root)
    found: dict[str, dict] = {}
    with paths.open_root(root) as root_fd:
        for start in dict.fromkeys(roots or [""]):
            if start and _closed_folder(start):
                continue
            try:
                start_fd = paths.open_dir(root_fd, start.split("/") if start else [])
            except paths.Refused:
                continue
            if start_fd is None:
                continue
            try:
                _walk(start_fd, start + "/" if start else "", rules, found)
            finally:
                os.close(start_fd)
    ordered = sorted(found)
    return {
        "files": [found[rel] for rel in ordered[:CODE_TREE_MAX]],
        "truncated": len(ordered) > CODE_TREE_MAX,
    }


def read_file(root: Path, rel: str, roots: list[str]) -> dict | None:
    """`POST /v1/code/file`'s body, or None for every refusal (the one
    404): outside the roots, missing, a folder, a symlink on the way, or
    closed by `classes.code_class`."""
    rules = _usable_rules(root)
    if not under_roots(rel, roots) or not paths.stays_under(root, rel):
        return None
    parts = rel.split("/")
    try:
        with paths.open_root(root) as root_fd:
            dir_fd = paths.open_dir(root_fd, parts[:-1])
            if dir_fd is None:
                return None
            try:
                got = _read_at(dir_fd, parts[-1])
            finally:
                os.close(dir_fd)
    except paths.Refused:
        return None
    if got is None:
        return None
    data, _st = got
    note_class = classes.code_class(rel, data, rules)
    if note_class is None:
        return None
    return {
        "path": rel,
        "class": note_class,
        "sha256": hashlib.sha256(data).hexdigest(),
        "text": data.decode("utf-8"),
    }
