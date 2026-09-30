"""A note's effective class: personal, knowledge, lens, or invisible (8e plan sections 3-4; lens plan section 3).

Two sources can set a class: the note's own property (frontmatter.py's
mark) and a folder rule in `Anchor/settings.md`, a file the user writes
and vaultd only reads. **When they disagree, the stricter class wins:**
`never` > `personal` > `knowledge` > `lens`. Anything that ends up
unclassified or `never` is invisible to the bot, exactly as an
un-opted-in note was in 8a: not listed, and the same 404 as a missing
file.

**Lens is a kind of knowledge, and the least strict class** (lens plan
section 3). Stricter wins applies to it like to any other class, so:

- `anchor: knowledge` on a note inside a lens folder keeps it out of
  the lens -- the way to exclude one note;
- `anchor: lens` inside a personal or never folder is not lens;
- a lens folder *nested inside* a knowledge, personal or never folder
  makes the whole file invalid. Stricter wins would turn every note in
  it into the outer class, leaving a lens that is silently empty (and,
  under a knowledge folder, writable by Claude). Failing closed makes
  `/vault` say so instead. A single note can still opt out with its
  own property.

A lens note is about a **person** when a `lens_person_folders` rule
covers it, else it is a **concept** (lens plan section 14.3). Every
`lens_person_folders` entry must lie inside (or equal) some
`lens_folders` entry; one that does not makes the whole file invalid,
because a person rule that covers nothing in the lens is a typo the
user would never see otherwise.

**This module is the one place the rule lives.** The manifest,
`GET /v1/file`, the knowledge routes and the graph all call
`effective_class`, never a copy of it, and
only after paths.py has already refused dot-folders and symlinks.

**`Anchor/settings.md` fails closed.** If it exists but cannot be used
-- invalid YAML, an alias, a duplicate key, an unknown key, a wrong
type, a bad folder entry, or no `anchor: settings` -- every note is
invisible until it is fixed. Dropping only the folder rules would also
drop `never_folders`, and a diary note carrying `anchor: knowledge`
would appear. An unknown key is fatal for the same reason: a typo like
`never_folder:` must not silently drop the never-rules
(docs/decisions.md, "8e -- the settings file"). A missing file is fine:
no folder rules.

**A case-mismatched name (`Anchor/Settings.md` on a case-sensitive
disk) is also fine, and just as invisible as a missing file** --
`wrong_case_settings_present` only reports the mismatch for the
manifest's diagnostic `settings` field (manifest.py); it is never read
as rules. Reading it would mean guessing the user meant this file,
which the fail-closed rule above exists to avoid.

**Folder names are compared segment by segment after NFC on both
sides**, because macOS can write a Cyrillic folder name in NFD. `Life`
covers `Life/Diary/x.md` and not `Lifestyle/x.md`. A `never` rule also
matches case-insensitively, so a never-rule typed in the wrong case
still hides; the two readable classes match exactly.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from vaultd import frontmatter, paths
from vaultd.config import NOTE_MAX_BYTES
from vaultd.frontmatter import NoteMark

SETTINGS_PATH = "Anchor/settings.md"
SETTINGS_MARK = "settings"
_SETTINGS_NAME = SETTINGS_PATH.rsplit("/", 1)[1]  # "settings.md"

NoteClass = Literal["personal", "knowledge", "lens"]
LensKind = Literal["person", "concept"]
SettingsState = Literal["ok", "absent", "invalid"]

# The classes the knowledge routes read (GET /v1/knowledge, the tree, the
# graph). Only `knowledge` is ever writable: the lens is the user's alone.
READABLE_KNOWLEDGE: frozenset[str] = frozenset({"knowledge", "lens"})

_LIST_KEYS = (
    "knowledge_folders",
    "personal_folders",
    "never_folders",
    "lens_folders",
    "lens_person_folders",
)
_ALLOWED_KEYS = frozenset({frontmatter.MARK_KEY, *_LIST_KEYS})

# never > personal > knowledge > lens.
_RANK = {"lens": 0, "knowledge": 1, "personal": 2, "never": 3}

# What a note's own mark asks for, before folder rules. `unknown` is
# handled before this table is consulted: it hides the note outright.
_PROPERTY_CLASS: dict[str, str | None] = {
    "never": "never",
    "personal": "personal",
    "knowledge": "knowledge",
    "lens": "lens",
    "legacy_read": "personal",
    "none": None,
}

Segments = tuple[str, ...]


@dataclass(frozen=True)
class FolderRules:
    state: SettingsState
    knowledge: tuple[Segments, ...] = ()
    personal: tuple[Segments, ...] = ()
    # Casefolded, see the module docstring.
    never: tuple[Segments, ...] = ()
    lens: tuple[Segments, ...] = ()
    # Each one inside some `lens` entry (parse_settings checks).
    lens_person: tuple[Segments, ...] = ()


SETTINGS_ABSENT = FolderRules("absent")
SETTINGS_INVALID = FolderRules("invalid")


@dataclass(frozen=True)
class Resolution:
    """The effective class, and the flags `/vault` counts (never a path)."""

    note_class: NoteClass | None
    conflict: bool = False
    legacy_read: bool = False
    unknown_value: bool = False
    # Set exactly when note_class is "lens".
    lens_kind: LensKind | None = None


def _segments(rel: str) -> Segments:
    return tuple(unicodedata.normalize("NFC", rel).split("/"))


def _folder_entry(raw: object) -> Segments | None:
    """A valid folder entry as NFC segments, or None if it is not one."""
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith("/") or raw.endswith("/") or "\\" in raw or "\x00" in raw:
        return None
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        return None
    parts = _segments(raw)
    if any(not part or part.startswith(".") for part in parts):
        return None
    return parts


def parse_settings(data: bytes) -> FolderRules:
    """The folder rules in a settings file's bytes, or SETTINGS_INVALID."""
    if len(data) > NOTE_MAX_BYTES:
        return SETTINGS_INVALID
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return SETTINGS_INVALID
    block = frontmatter.split(data)
    if block is None:
        return SETTINGS_INVALID
    try:
        loaded = yaml.load(block.decode("utf-8"), Loader=frontmatter.StrictLoader)  # noqa: S506 - StrictLoader is a SafeLoader
    except (UnicodeDecodeError, yaml.YAMLError, frontmatter.FrontmatterError, RecursionError, ValueError):
        return SETTINGS_INVALID
    if not isinstance(loaded, dict) or not set(loaded) <= _ALLOWED_KEYS:
        return SETTINGS_INVALID
    mark = loaded.get(frontmatter.MARK_KEY)
    if not isinstance(mark, str) or mark != SETTINGS_MARK:
        return SETTINGS_INVALID
    lists: dict[str, tuple[Segments, ...]] = {}
    for key in _LIST_KEYS:
        raw = loaded.get(key, [])
        if not isinstance(raw, list):
            return SETTINGS_INVALID
        entries = []
        for item in raw:
            parts = _folder_entry(item)
            if parts is None:
                return SETTINGS_INVALID
            entries.append(parts)
        lists[key] = tuple(entries)
    for person in lists["lens_person_folders"]:
        if not any(_covers(lens, person) for lens in lists["lens_folders"]):
            return SETTINGS_INVALID
    for lens in lists["lens_folders"]:
        folded = tuple(p.casefold() for p in lens)
        if any(_covers(rule, lens) for rule in lists["knowledge_folders"] + lists["personal_folders"]):
            return SETTINGS_INVALID
        if any(_covers(tuple(p.casefold() for p in rule), folded) for rule in lists["never_folders"]):
            return SETTINGS_INVALID
    return FolderRules(
        "ok",
        knowledge=lists["knowledge_folders"],
        personal=lists["personal_folders"],
        never=tuple(tuple(p.casefold() for p in parts) for parts in lists["never_folders"]),
        lens=lists["lens_folders"],
        lens_person=lists["lens_person_folders"],
    )


def load_rules(root: Path) -> FolderRules:
    """Read `Anchor/settings.md` from the vault. Anything odd is invalid."""
    try:
        data = paths.read_file(root, SETTINGS_PATH)
    except (paths.Refused, OSError):
        return SETTINGS_INVALID
    if data is None:
        return SETTINGS_ABSENT
    return parse_settings(data)


def wrong_case_settings_present(root: Path) -> bool:
    """True when `Anchor/settings.md` is absent but `Anchor/` holds a
    regular file whose NFC-normalised name matches it case-insensitively
    (the production bug: `Anchor/Settings.md` on a case-sensitive Linux
    disk, written by a case-insensitive client). Only called once
    `load_rules` has already found the exact path missing.

    **Never reads the file.** Reporting `wrong_case` is diagnostic only
    -- the manifest still carries no folder rules, exactly as if nothing
    were there (module docstring: fail closed)."""
    try:
        with paths.open_root(root) as root_fd:
            dir_fd = paths.open_dir(root_fd, ["Anchor"])
            if dir_fd is None:
                return False
            try:
                for entry in os.scandir(dir_fd):
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    name = unicodedata.normalize("NFC", entry.name)
                    if name == _SETTINGS_NAME:
                        continue  # the exact name -- load_rules would have found it
                    if name.casefold() == _SETTINGS_NAME.casefold():
                        return True
                return False
            finally:
                os.close(dir_fd)
    except paths.Refused:
        return False


def is_settings_file(rel: str) -> bool:
    return unicodedata.normalize("NFC", rel) == SETTINGS_PATH


def _covers(rule: Segments, folders: Segments) -> bool:
    return len(rule) <= len(folders) and folders[: len(rule)] == rule


def _folder_class(rel: str, rules: FolderRules) -> str | None:
    folders = _segments(rel)[:-1]
    if any(_covers(rule, tuple(p.casefold() for p in folders)) for rule in rules.never):
        return "never"
    if any(_covers(rule, folders) for rule in rules.personal):
        return "personal"
    if any(_covers(rule, folders) for rule in rules.knowledge):
        return "knowledge"
    if any(_covers(rule, folders) for rule in rules.lens):
        return "lens"
    return None


def lens_kind(rel: str, rules: FolderRules) -> LensKind:
    """`person` under a `lens_person_folders` rule, else `concept`.

    Only meaningful for a note whose effective class is already lens;
    matched exactly like the readable classes' folder rules.
    """
    folders = _segments(rel)[:-1]
    if any(_covers(rule, folders) for rule in rules.lens_person):
        return "person"
    return "concept"


def effective_class(rel: str, mark: NoteMark, rules: FolderRules) -> Resolution:
    """Combine the note's mark and the folder rules; the stricter wins.

    `conflict` counts only a property *looser* than its folder (a
    `knowledge` note in a personal folder, or an `anchor: lens` note in
    a knowledge folder): that is the case where the user's own word was
    not followed. A stricter property, such as
    `anchor: never` inside a knowledge folder, is a deliberate
    tightening and is not counted (docs/decisions.md).
    """
    if rules.state == "invalid":
        return Resolution(None)
    if mark == "unknown":
        return Resolution(None, unknown_value=True)
    legacy = mark == "legacy_read"
    wanted = _PROPERTY_CLASS[mark]
    folder = _folder_class(rel, rules)
    conflict = wanted is not None and folder is not None and _RANK[wanted] < _RANK[folder]
    candidates = [c for c in (wanted, folder) if c is not None]
    if not candidates:
        return Resolution(None, legacy_read=legacy)
    strictest = max(candidates, key=_RANK.__getitem__)
    if strictest == "never":
        return Resolution(None, conflict=conflict, legacy_read=legacy)
    kind = lens_kind(rel, rules) if strictest == "lens" else None
    return Resolution(strictest, conflict=conflict, legacy_read=legacy, lens_kind=kind)  # type: ignore[arg-type]
