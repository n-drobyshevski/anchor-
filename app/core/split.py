"""Split long replies into Telegram-sized chunks (plan section 11).

Telegram's hard limit is 4096 characters; we split at 4000 for margin.
Split priority: paragraph break, then sentence end, then whitespace,
then a hard cut. Operates on `str` (Python characters), so Cyrillic
(one codepoint per character here) is handled the same as ASCII.

The hard-cut fallback is what guarantees forward progress: every branch
below only accepts a cut point strictly greater than 0, so each loop
iteration always consumes at least one character and a chunk is never
empty.
"""

from __future__ import annotations

_SENTENCE_ENDERS = (".", "!", "?", "…")  # . ! ? …


def _find_cut(window: str) -> int:
    """Return an index in [1, len(window)] at which to cut `window`.

    `window` is already truncated to `limit` characters by the caller.
    Preference order: paragraph break > sentence end > whitespace >
    hard cut at len(window) (i.e. the full window). The returned index
    is where the chunk ends; the separator itself (if any) is dropped
    by split()'s lstrip, not included in either chunk.
    """
    idx = window.rfind("\n\n")
    if idx > 0:
        return idx  # drop the blank line itself from both chunks

    idx = max((window.rfind(ch) for ch in _SENTENCE_ENDERS), default=-1)
    if idx > 0:
        return idx + 1  # keep the punctuation mark, drop it from the next chunk

    for i in range(len(window) - 1, 0, -1):
        if window[i].isspace():
            return i  # drop the whitespace character itself from both chunks

    return len(window)  # hard cut: no separator found anywhere in the window


def split(text: str, limit: int = 4000) -> list[str]:
    """Split `text` into chunks of at most `limit` characters.

    Returns `[]` for empty input, `[text]` unchanged when it already
    fits. Never returns an empty chunk.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = _find_cut(remaining[:limit])
        chunk = remaining[:cut]
        chunks.append(chunk)
        # Whichever separator was cut on (blank line, punctuation's
        # trailing space, or a lone space) is dropped here rather than
        # carried into the next chunk.
        remaining = remaining[cut:].lstrip(" \n")

    if remaining:
        chunks.append(remaining)
    return chunks
