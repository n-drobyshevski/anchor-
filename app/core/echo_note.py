"""The note Echo writes into the inbox when research is adopted (lens L4).

anchor-lens-plan.md sections 9 and 14.5, the L4 spec section 5. «в Inbox»
on a research's result message makes **one** knowledge note holding every
visible pending card of the gap, one section per distilled point:

    ---
    anchor: knowledge
    source_urls:
      - "https://plato.stanford.edu/entries/..."
    gap: 12
    ---
    Исследование Echo; это не линза.

    Линза: [[Ashby]] · [[Beer]]

    ## <the card's text>

    > <its verbatim quote>

    Источник: <https://plato.stanford.edu/entries/...>

- **The frontmatter keys are exactly** `anchor`, `source_urls` and
  `gap`: vaultd refuses any other set (`frontmatter_keys`) and stamps
  `anchor_edited_by: echo` itself. `anchor: knowledge`, never `lens`:
  the note is material to read, and only the user promotes it (move it
  into a lens folder *and* mark it `anchor: lens`).
- **The first line says so**: research by Echo, not the lens.
- **`[[links]]` only to lens notes** the gap names, by their current
  titles (W2 cannot rename a lens note, so a link never blocks a rename
  it should not); a title Obsidian could not link to is escaped text.
- **Web text is inert.** Every card text and quote passes through
  `render.report_escape` (one line, no wikilink, link, tag, heading,
  table cell, HTML or comment), as the garden report's model text does.
  A source URL -- copied by code from the fetched page, never from the
  model -- is shown only when it is a plain http(s) URL with nothing
  Markdown could read as structure; the frontmatter keeps it JSON-quoted.
- **The name** is the gap's proposed title for a missing note, so the
  garden's recheck finds the note by that title and resolves the gap;
  otherwise the two notes' titles, `A — B`. vaultd adds ` 2`..` 9` when
  the name is taken. Sanitised by app/core/note_checks.py's
  `sanitize_title`, the same as a Claude-made note's, and NFC, at most
  NAME_MAX_CHARS, so the suffix still fits vaultd's 120.

Pure: no database, no client, no settings, no logging.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from typing import Protocol

from app.core import note_checks
from app.vault.render import report_escape

INBOX_NOTICE = "Исследование Echo; это не линза."
# vaultd's segment limit is 120 characters, and it may add " 9".
NAME_MAX_CHARS = 100
# app/vault/render.py's `_LINK_UNSAFE`: a title holding any of these
# cannot be a wikilink target as is.
_LINK_UNSAFE = ("[", "]", "|", "#", "^", "\n", "\r")
_SAFE_URL = re.compile(r"https?://[^\s<>\[\]|`\"\\]+")


class Card(Protocol):
    """What a card must carry (app/research/jobs.py's `LensCard`)."""

    text: str
    quote: str
    source_url: str


def note_name(*, gap_id: int, kind: str, title: str | None, titles: Sequence[str]) -> str:
    """The note's basename, `.md` included (module docstring)."""
    if kind == "missing_note" and title and title.strip():
        stem = title
    elif [t for t in titles if t.strip()]:
        stem = " — ".join(t.strip() for t in titles if t.strip())
    else:
        stem = title or ""
    stem = unicodedata.normalize("NFC", stem).replace("\x7f", "_").strip()[:NAME_MAX_CHARS]
    try:
        return note_checks.sanitize_title(stem)
    except note_checks.Refused:
        return f"Исследование {gap_id}.md"


def _link(title: str) -> str | None:
    if not title.strip():
        return None
    if any(ch in title for ch in _LINK_UNSAFE):
        return report_escape(title)
    return f"[[{title}]]"


def _urls(cards: Sequence[Card]) -> list[str]:
    return list(dict.fromkeys(card.source_url for card in cards))


def render(*, gap_id: int, titles: Sequence[str], cards: Sequence[Card]) -> str:
    """The whole note (module docstring). `titles` are the lens notes'
    current titles (app/vault/lens.py's `research_target`), `cards` the
    research's visible pending cards in order."""
    if gap_id <= 0:
        raise ValueError("gap_id must be positive")
    lines = ["---", "anchor: knowledge"]
    urls = _urls(cards)
    if urls:
        lines.append("source_urls:")
        lines.extend(f"  - {json.dumps(url)}" for url in urls)
    else:
        lines.append("source_urls: []")
    lines += [f"gap: {int(gap_id)}", "---", INBOX_NOTICE, ""]
    links = [shown for shown in (_link(title) for title in titles) if shown]
    if links:
        lines += ["Линза: " + " · ".join(links), ""]
    for card in cards:
        heading = report_escape(card.text) or "—"
        lines += [f"## {heading}", ""]
        quote = report_escape(card.quote)
        if quote:
            lines += [f"> {quote}", ""]
        if _SAFE_URL.fullmatch(card.source_url):
            lines += [f"Источник: <{card.source_url}>", ""]
    return "\n".join(lines).rstrip("\n") + "\n"
