"""Echo's stream notes: one note per planner stream, kept current.

The planner groups the user's tasks into streams (by tag, or a saved
stream); the bot renders each as a short note -- what is open, what needs
attention, the nearest deadlines -- and sends it to
`PUT /v1/echo/streams`. This module holds every check that route makes,
inside the store's single lock and before a byte is written.

It is a second, separate writer beside echo.py, not a loosening of it:
echo.py stays create-only. What is different here, and why it is safe:

1. **The note is `anchor: never`.** A stream note is a digest of the
   user's task list, and nothing the bot should ever learn back from the
   vault. `never` is the strictest class (classes.py): the manifest does
   not list the note, `GET /v1/file` and the knowledge routes answer 404,
   the graph skips it, and no folder rule can reveal it. So it never
   reaches the bot's memory or search, or Claude's connector, by the
   rule that already hides the user's own `never` notes -- no new class.
2. **vaultd writes the frontmatter, all of it.** The bot sends a bare
   basename, the stream's key and the body text; never a path, never a
   property. The block is exactly `anchor`, `echo_stream`,
   `echo_body_sha256` and the `echo` provenance stamp.
3. **Only inside the inbox.** The folder is `<echo_inbox>/Streams`
   (classes.py's `echo_inbox`, the one place Echo may create notes), so
   there is no new settings key and no inbox means no stream notes.
4. **Create, or replace its own untouched note -- nothing else.** An
   existing file at the name is replaced only when it is provably the
   note this writer left there: the same five keys, `anchor: never`,
   stamped `echo`, the same `echo_stream`, and a body that still hashes
   to `echo_body_sha256`. A body the user edited is `stream_edited`; any
   other file (or another stream's note) is `name_taken`. Either way the
   file is left exactly as it is. There is no suffixing: a stream has
   one name, and the bot stops writing a stream that was refused.
5. **An unchanged body writes nothing**, and spends no changeset.
6. **Every write is undoable.** It is recorded as an Echo changeset,
   `purpose: streams`, with the replaced bytes as its pre-image, so
   `/v1/undo?writer=echo` restores the note as it was. The caps are
   this purpose's own (config.py's `STREAM_*`): one changeset per
   refresh holds every note of that refresh, and these changesets do
   not spend the inbox writer's daily budget, nor it theirs.

There is no rename and no delete: a stream that ends leaves its last
note behind, for the user to remove.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Callable

from vaultd import classes, echo, frontmatter, paths, provenance
from vaultd.config import ECHO_WRITE_MAX_BYTES, STREAM_KEY_MAX_CHARS, STREAMS_SUBFOLDER
from vaultd.knowledge import Refused
from vaultd.store import Conflict, Store, sha256

STREAM_KEY = "echo_stream"
BODY_SHA_KEY = "echo_body_sha256"
OWN_KEYS = frozenset(
    {frontmatter.MARK_KEY, STREAM_KEY, BODY_SHA_KEY, "anchor_edited_by", "anchor_edited_at"}
)
_MARK = "never"


@dataclass(frozen=True)
class StreamPlan:
    """A checked write. `data` is None when the note already holds this
    body (nothing to write); `pre_image` is the bytes being replaced, or
    None for a new note."""

    rel: str
    data: bytes | None
    pre_image: bytes | None
    new_folders: tuple[str, ...]


def folder_rel(rules: classes.FolderRules) -> str | None:
    """The stream notes' folder, or None when there is no inbox."""
    inbox = echo.inbox_rel(rules)
    return None if inbox is None else f"{inbox}/{STREAMS_SUBFOLDER}"


def _check_stream(stream: str) -> None:
    if not stream or len(stream) > STREAM_KEY_MAX_CHARS:
        raise Refused("bad_name")
    if unicodedata.normalize("NFC", stream) != stream:
        raise Refused("bad_name")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in stream):
        raise Refused("bad_name")


def compose(stream: str, body: bytes, edited_at_iso: str) -> bytes:
    """The whole note: vaultd's own frontmatter, then the bot's body."""
    head = (
        f"---\n{frontmatter.MARK_KEY}: {_MARK}\n"
        f"{STREAM_KEY}: {json.dumps(stream, ensure_ascii=False)}\n"
        f"{BODY_SHA_KEY}: \"{sha256(body)}\"\n---\n"
    ).encode("utf-8")
    return provenance.apply(head + body, edited_at_iso, by="echo")


def own_body(data: bytes, stream: str) -> bytes:
    """The body of an existing note, if it is this writer's untouched
    note for `stream` (module docstring, 4). Raises Refused otherwise."""
    loaded = frontmatter.load(data)
    if loaded is None or set(loaded) != OWN_KEYS:
        raise Refused("name_taken")
    if loaded[frontmatter.MARK_KEY] != _MARK or loaded["anchor_edited_by"] != "echo":
        raise Refused("name_taken")
    if loaded[STREAM_KEY] != stream:
        raise Refused("name_taken")
    body = frontmatter.body(data)
    if loaded[BODY_SHA_KEY] != sha256(body):
        raise Refused("stream_edited")
    return body


def plan(store: Store, name: str, stream: str, body: str, *, now: Callable[[], str]) -> StreamPlan:
    """Every check, in order, with nothing written. Raises Refused."""
    rules = classes.load_rules(store.vault_path)
    if rules.state == "invalid":
        raise Refused("settings_invalid")
    folder = folder_rel(rules)
    if folder is None:
        raise Refused("inbox_unavailable")
    echo._check_name(name)  # noqa: SLF001 - same package, the same bare-basename rule
    _check_stream(stream)
    try:
        body_bytes = body.encode("utf-8")
    except UnicodeEncodeError:
        raise Refused("bad_utf8") from None
    if len(body_bytes) > ECHO_WRITE_MAX_BYTES:
        raise Refused("too_large")
    rel = f"{folder}/{unicodedata.normalize('NFC', name)}"
    new_folders = echo._missing_folders(store.vault_path, folder)  # noqa: SLF001 - same package
    try:
        existing = None if new_folders else paths.read_file(store.vault_path, rel)
    except paths.Refused:
        raise Refused("symlink") from None
    if existing is not None and own_body(existing, stream) == body_bytes:
        return StreamPlan(rel=rel, data=None, pre_image=existing, new_folders=())
    data = compose(stream, body_bytes, now())
    # What was just composed must read back as this writer's own note, or
    # the next refresh could not replace it (and a block the loader cannot
    # parse would hide nothing by rule, only by accident).
    if frontmatter.note_mark(data) != _MARK:
        raise Refused("bad_frontmatter")
    own_body(data, stream)
    return StreamPlan(rel=rel, data=data, pre_image=existing, new_folders=new_folders)


def perform(store: Store, planned: StreamPlan) -> str:
    """Write the planned note; returns its sha256. Create-only for a new
    note, compare-and-swap against the bytes `plan` read for a
    replacement, so a file that appeared or changed in between is left
    alone (`name_taken` / `stream_edited`)."""
    assert planned.data is not None
    expected = None if planned.pre_image is None else sha256(planned.pre_image)
    try:
        return store.put_unchecked(planned.rel, planned.data, expected)
    except Conflict:
        if expected is None:
            raise Refused("name_taken") from None
        raise Refused("stream_edited") from None
    except paths.Refused:
        raise Refused("symlink") from None
