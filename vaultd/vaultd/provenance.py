"""Provenance stamps on every file vaultd writes for Claude (write-plan section 6.6).

`anchor_edited_by: claude` and `anchor_edited_at: "<UTC ISO, seconds, Z>"`
are set by vaultd itself, never taken from the request: any
`anchor_edited_*` line already in the incoming frontmatter is dropped,
and vaultd's own two lines are appended. Every other line is kept
**byte for byte** -- this never loads the YAML into a dict and
re-dumps it, which would reformat quoting, key order and comments the
user did not touch. It only ever inserts or removes whole lines at the
top of the fence.

A file with no frontmatter at all gets a fresh two-line fence prepended
ahead of its body, which is also left untouched.
"""

from __future__ import annotations

from vaultd.config import FRONTMATTER_MAX_BYTES

_STRIPPED_KEYS = (b"anchor_edited_by", b"anchor_edited_at")


def _line_key(line: bytes) -> bytes:
    return line.split(b":", 1)[0].strip()


def _locate_fence(data: bytes) -> tuple[int, int, int] | None:
    """(yaml_start, yaml_end, after_closing_fence) for a closed leading fence.

    Mirrors frontmatter.split's search exactly (same cap, same fence
    forms), so "a frontmatter block vaultd's own loader parsed" and "a
    fence this function can locate" are the same set of files.
    """
    head = data[:FRONTMATTER_MAX_BYTES]
    for opening in (b"---\n", b"---\r\n"):
        if head.startswith(opening):
            break
    else:
        return None
    start = len(opening)
    pos = start
    while True:
        end = head.find(b"\n", pos)
        line_end = end if end != -1 else len(head)
        line = head[pos:line_end].rstrip(b"\r")
        if line == b"---":
            if end == -1:
                if len(data) > len(head):
                    return None
                return start, pos, line_end
            return start, pos, end + 1
        if end == -1:
            return None
        pos = end + 1


def apply(data: bytes, edited_at_iso: str) -> bytes:
    """`data` with `anchor_edited_by`/`anchor_edited_at` set, everything else kept as is."""
    stamp = f"anchor_edited_by: claude\nanchor_edited_at: \"{edited_at_iso}\"\n".encode()
    located = _locate_fence(data)
    if located is None:
        return b"---\n" + stamp + b"---\n" + data
    start, yaml_end, after_fence = located
    block = data[start:yaml_end]
    kept = [line for line in block.splitlines(keepends=True) if _line_key(line) not in _STRIPPED_KEYS]
    new_block = b"".join(kept) + stamp
    return data[:start] + new_block + data[yaml_end:]
