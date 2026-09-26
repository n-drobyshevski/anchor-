"""Turn a note's raw file content into indexable chunks (milestone 8d, phase 1).

Pure and synchronous: no DB, no vaultd client, no model call.
`app/vault/sync.py` (8d, phase 2) fetches a note's content and title and
calls `prepare(content, title)`; this module's only job is text in,
`Chunk`s out.

**Pipeline**, each step earning its place from the phase-8 plan's
section 9 and the 8e plan's section 9 amendment:

1. strip frontmatter (`app/vault/frontmatter.split`);
2. remove every `%% … %%` Obsidian comment, inline or multi-line (8e
   plan section 3: "the place for a private aside inside a knowledge
   note");
3. drop fenced code blocks and embeds (`![[…]]`), and turn `[[target|
   label]]` into `label`, `[[target]]` into `target`'s last path
   segment without a `#heading`;
4. **mask, over the whole cleaned text, before any chunk boundary is
   decided** -- see `_mask`'s docstring for why that ordering is what
   keeps a secret from ever being split across a chunk boundary;
5. split at headings, then paragraph boundaries, into chunks of at most
   `NOTE_CHUNK_CHARS`, each carrying a `heading` of the note's title
   plus its nearest heading path;
6. drop empty chunks.

**A file whose frontmatter fence cannot be split safely** (no leading
`---` line, or a fence that never closes within 4 KB) is not an error
here: `frontmatter.split` already treats that as "no frontmatter", the
same rule vaultd's own parser uses, and this module follows it --
`prepare` indexes the whole file as body text rather than refusing it.
Malformed *YAML inside* a well-formed fence is likewise not this
module's concern: it only removes the fenced block, and never loads it,
so a duplicate key or an alias bomb inside frontmatter cannot make
`prepare` do anything but strip one more block of text.
"""

from __future__ import annotations

import re

from app.core import redact
from app.vault import frontmatter
from app.vault import secrets as vault_secrets
from app.vault._chunks import Chunk

NOTE_CHUNK_CHARS = 800
"""A constant, not a Settings field -- same call as
app/core/memory.py's RETRIEVAL_MIN_SCORE and app/vault/limits.py's caps:
a deploy must not be able to widen every prompt citation by pasting a
bigger number into the environment. 800 is picked, not measured: large
enough that a chunk usually holds a whole paragraph or a short section
(so `ts_rank_cd` has enough text to score against and the chunk reads
as one coherent thought in the prompt), comfortably under the
`note_chunk_*` tables' 1200-char CHECK, and small enough that
`VAULT_PERSONAL_IN_PROMPT`/`VAULT_KNOWLEDGE_IN_PROMPT` chunks together
stay a few hundred prompt tokens (phase-8 plan section 14's cost
estimate)."""

HEADING_MAX_CHARS = 200
"""Matches `note_chunk_*`'s `heading` CHECK (`char_length(heading) <=
200`) and the phase-8 plan's own cap on the rendered heading path."""

MASK = "[скрыто]"

# Comments: %% ... %%, possibly spanning several lines (DOTALL), and
# non-greedy so two separate comments on one page are removed
# separately rather than everything between the first %% and the last
# swallowed as one.
_COMMENT = re.compile(r"%%.*?%%", re.DOTALL)

# Fenced code blocks: ``` ... ```, DOTALL for the same reason, and
# non-greedy for the same reason as _COMMENT.
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)

# Embeds: ![[anything]]. Dropped whole -- there is no label to keep.
_EMBED = re.compile(r"!\[\[[^\]]*\]\]")

# [[target|label]] -> label.
_WIKILINK_PIPED = re.compile(r"\[\[([^\]|]*)\|([^\]]*)\]\]")

# [[target]] -> target's last path segment, without a #heading suffix.
_WIKILINK_PLAIN = re.compile(r"\[\[([^\]]*)\]\]")

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

_PARAGRAPH_SPLIT = re.compile(r"\n[ \t]*\n")


def _strip_frontmatter(content: str) -> str:
    parts = frontmatter.split(content)
    return content if parts is None else parts.body


def _strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def _drop_code_fences(text: str) -> str:
    return _CODE_FENCE.sub("", text)


def _resolve_wikilinks(text: str) -> str:
    text = _EMBED.sub("", text)
    text = _WIKILINK_PIPED.sub(lambda m: m.group(2), text)

    def _plain(match: re.Match[str]) -> str:
        target = match.group(1).split("#", 1)[0]
        return target.rsplit("/", 1)[-1]

    return _WIKILINK_PLAIN.sub(_plain, text)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _mask(text: str) -> str:
    """Replace every secret span with `MASK`, over the whole text at once.

    This runs *before* the text is split into chunks. That ordering is
    what makes "a secret split across a chunk boundary" impossible
    rather than merely unlikely: chunk boundaries are chosen by
    `_chunk_section` on the string this function returns, in which
    every secret has already been collapsed to the seven characters of
    `MASK`. There is no later step that could still cut a JWT or a PEM
    block in half, because by the time chunking runs, no secret-shaped
    text remains for a boundary to land inside.
    """
    spans = _merge_spans(redact.secret_spans(text) + vault_secrets.spans(text))
    if not spans:
        return text
    pieces: list[str] = []
    pos = 0
    for start, end in spans:
        pieces.append(text[pos:start])
        pieces.append(MASK)
        pos = end
    pieces.append(text[pos:])
    return "".join(pieces)


def _heading_path(title: str, stack: list[str]) -> str:
    parts = [title, *[level for level in stack if level]]
    path = " › ".join(parts)
    if len(path) > HEADING_MAX_CHARS:
        path = path[: HEADING_MAX_CHARS - 1] + "…"
    return path


def _sections(text: str, title: str) -> list[tuple[str, str]]:
    """`(heading_path, section_text)` pairs, split at heading lines.

    `stack[i]` holds the current heading text at level `i+1`
    (`#`..`######`). A heading at level `n` replaces `stack[n-1]` and
    drops everything deeper, which is what keeps the path "nearest
    heading", not "every heading ever seen".
    """
    stack: list[str] = []
    current: list[str] = []
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        body = "\n".join(current).strip()
        if body:
            sections.append((_heading_path(title, stack), body))

    for line in text.splitlines():
        match = _HEADING_LINE.match(line)
        if match:
            flush()
            current.clear()
            level = len(match.group(1))
            del stack[level - 1 :]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(match.group(2))
        else:
            current.append(line)
    flush()
    return sections


def _chunk_section(text: str, limit: int) -> list[str]:
    """Paragraphs of `text`, greedily packed into pieces of at most `limit`.

    A paragraph longer than `limit` on its own is hard-split rather
    than left oversized -- it can only happen to a paragraph that
    already had no secrets left to protect a boundary for (§`_mask`
    ran first), so a hard cut here never lands inside a masked span.
    """
    pieces: list[str] = []
    buffer = ""
    for paragraph in _PARAGRAPH_SPLIT.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > limit:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(paragraph[i : i + limit] for i in range(0, len(paragraph), limit))
            continue
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) <= limit:
            buffer = candidate
        else:
            pieces.append(buffer)
            buffer = paragraph
    if buffer:
        pieces.append(buffer)
    return pieces


def prepare(content: str, title: str) -> list[Chunk]:
    """Content of one note, plus its title, into chunks ready for indexing."""
    body = _strip_frontmatter(content)
    body = _strip_comments(body)
    body = _drop_code_fences(body)
    body = _resolve_wikilinks(body)
    body = _mask(body)
    chunks: list[Chunk] = []
    for heading, section_text in _sections(body, title):
        for piece in _chunk_section(section_text, NOTE_CHUNK_CHARS):
            piece = piece.strip()
            if piece:
                chunks.append(Chunk(heading, piece))
    return chunks
