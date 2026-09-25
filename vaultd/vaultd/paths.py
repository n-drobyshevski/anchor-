"""The path rules (plan section 5.4), checked before anything touches the disk.

Three questions, in this order, on every route:

1. **Is it a well-formed vault-relative path?** Non-empty, no leading
   `/`, no backslash, no NUL, no empty, `.` or `..` segment, valid
   UTF-8. Anything else is malformed and refused outright.
2. **Is it writable?** Exactly `^Anchor/(Memory|Journal)/[^/]+\\.md$`,
   and the name does not start with a dot. Nothing else is writable,
   ever: not a subfolder, not another extension, not `Anchor/` itself.
3. **Can it be reached without a symlink?** Every component is opened
   relative to its parent's file descriptor with `O_NOFOLLOW`, so a
   symlink anywhere on the way is refused rather than followed, and
   the check cannot be raced by swapping a directory for a link
   between an `lstat` and an `open`. `realpath` must also stay under
   the vault -- redundant after the descriptor walk, and kept because
   the plan names it and belts are cheap.

Anything under a dot-folder (`.obsidian`, `.trash`) is never readable;
the manifest never lists it either.
"""

from __future__ import annotations

import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

WRITABLE_RE = re.compile(r"^Anchor/(Memory|Journal)/[^/]+\.md$")
ANCHOR_DIRS = ("Anchor/Memory", "Anchor/Journal")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class Malformed(Exception):
    """The path is not a well-formed vault-relative path."""


class Refused(Exception):
    """The path is well-formed but crosses a symlink or leaves the vault."""


def parse_rel(raw: str) -> str:
    if not raw:
        raise Malformed
    if "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise Malformed
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate: what a percent-encoded invalid byte decodes to.
        raise Malformed from None
    if any(part in ("", ".", "..") for part in raw.split("/")):
        raise Malformed
    return raw


def is_writable(rel: str) -> bool:
    return bool(WRITABLE_RE.match(rel)) and not rel.rsplit("/", 1)[1].startswith(".")


def has_dot_segment(rel: str) -> bool:
    return any(part.startswith(".") for part in rel.split("/"))


@contextmanager
def open_root(root: Path) -> Iterator[int]:
    fd = os.open(root, _DIR_FLAGS)
    try:
        yield fd
    finally:
        os.close(fd)


def open_dir(root_fd: int, parts: list[str], *, create: bool = False) -> int | None:
    """Walk `parts` below `root_fd` without following a symlink.

    Returns a new directory descriptor, or None when a component does
    not exist and `create` is false. Raises Refused when a component is
    a symlink or not a directory.
    """
    fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                try:
                    nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError:
                    raise Refused from None
            except OSError:
                # ELOOP (a symlink under O_NOFOLLOW) or ENOTDIR (a file
                # where a folder should be): either way, not a path we take.
                raise Refused from None
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def read_regular_at(dir_fd: int, name: str) -> bytes | None:
    """The bytes of a regular file in `dir_fd`, or None if it is absent.

    Raises Refused for a symlink or anything that is not a regular file.
    """
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refused from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def stays_under(root: Path, rel: str) -> bool:
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(real_root, rel))
    return os.path.commonpath([real_root, real]) == real_root


def read_file(root: Path, rel: str) -> bytes | None:
    """Read a vault file by relative path; None if any part of it is absent."""
    if not stays_under(root, rel):
        raise Refused
    parts = rel.split("/")
    with open_root(root) as root_fd:
        dir_fd = open_dir(root_fd, parts[:-1])
        if dir_fd is None:
            return None
        try:
            return read_regular_at(dir_fd, parts[-1])
        finally:
            os.close(dir_fd)
