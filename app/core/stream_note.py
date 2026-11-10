"""The body of one planner stream's note (Echo's stream notes).

`/flows notes` writes one note per tag stream through vaultd's
`PUT /v1/echo/streams`, which owns the frontmatter and the path; this
module renders only the body, and the bare name to ask for.

    # диплом

    Поток задач из планера. Заметку ведёт Echo: правки в ней остановят обновление.

    Открыто: 4 · просрочено: 1 · требует внимания: 2 · ближайший срок: 2026-11-12

    ## Требует внимания

    - Глава 2 — срок 2026-11-12 · просрочено

- **Dates are absolute.** «завтра» would be wrong by the next morning,
  and the note is only rewritten when the user asks.
- **Task and tag text is inert.** Every title and label passes through
  `render.report_escape` (one line, no wikilink, link, tag, heading,
  table cell, HTML or comment): a task title is the user's own text, but
  it must not be able to build structure in their vault.
- **Only tag streams get a note.** The planner's untagged bucket and its
  other groupings are views, not streams the user named.

Pure: no database, no client, no settings, no logging.
"""

from __future__ import annotations

import re
import unicodedata

from app.core import note_checks
from app.vault.render import report_escape

NOTICE = "Поток задач из планера. Заметку ведёт Echo: правки в ней остановят обновление."
TAG_KEY_PREFIX = "tag:"
MAX_STREAMS = 12
MAX_TASKS = 5
# vaultd's stream key and segment limits are 120 characters.
KEY_MAX_CHARS = 120
NAME_MAX_CHARS = 100
ATTENTION_SCORE = 0.5
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _date(value: object) -> str | None:
    return value if isinstance(value, str) and _DATE_RE.match(value) else None


def stream_key(stream: object) -> str | None:
    """The stream's key if it is a tag stream vaultd will accept, else None."""
    if not isinstance(stream, dict):
        return None
    key = stream.get("key")
    if not isinstance(key, str) or not key.startswith(TAG_KEY_PREFIX):
        return None
    key = unicodedata.normalize("NFC", key)
    if len(key) <= len(TAG_KEY_PREFIX) or len(key) > KEY_MAX_CHARS:
        return None
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in key):
        return None
    return key


def note_name(key: str) -> str | None:
    """The bare `.md` basename for a stream's note, or None when its tag
    cannot make a safe file name."""
    try:
        name = note_checks.sanitize_title(key[len(TAG_KEY_PREFIX) :][:NAME_MAX_CHARS])
    except note_checks.Refused:
        return None
    return unicodedata.normalize("NFC", name)


def _task_line(task: object) -> str | None:
    if not isinstance(task, dict) or not isinstance(task.get("title"), str):
        return None
    title = report_escape(task["title"])
    if not title:
        return None
    parts = []
    due = _date(task.get("dueDate"))
    if due:
        parts.append(f"срок {due}")
    if task.get("overdue") is True:
        parts.append("просрочено")
    return f"- {title} — " + " · ".join(parts) if parts else f"- {title}"


def render_body(stream: dict, key: str) -> str:
    """The note's body for one stream of `get_flows`' output."""
    label = report_escape(key[len(TAG_KEY_PREFIX) :]) or "—"
    summary = [f"Открыто: {_count(stream.get('open'))}"]
    if _count(stream.get("overdue")):
        summary.append(f"просрочено: {_count(stream.get('overdue'))}")
    if _count(stream.get("needsAttention")):
        summary.append(f"требует внимания: {_count(stream.get('needsAttention'))}")
    next_due = _date(stream.get("nextDue"))
    if next_due:
        summary.append(f"ближайший срок: {next_due}")
    lines = [f"# {label}", "", NOTICE, "", " · ".join(summary), ""]
    tasks = stream.get("tasks") if isinstance(stream.get("tasks"), list) else []
    urgent = []
    for task in tasks:
        score = task.get("score") if isinstance(task, dict) else None
        if isinstance(score, (int, float)) and not isinstance(score, bool) and score >= ATTENTION_SCORE:
            line = _task_line(task)
            if line:
                urgent.append(line)
    if urgent:
        lines += ["## Требует внимания", "", *urgent[:MAX_TASKS], ""]
    return "\n".join(lines)


def notes_for(flows: object) -> list[tuple[str, str, str]]:
    """`(name, key, body)` for each tag stream of a `get_flows` payload,
    in its order, at most MAX_STREAMS, one per name. Never raises on a
    malformed payload."""
    streams = flows.get("streams") if isinstance(flows, dict) else None
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for stream in streams if isinstance(streams, list) else []:
        key = stream_key(stream)
        name = note_name(key) if key else None
        if key is None or name is None or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append((name, key, render_body(stream, key)))
        if len(out) == MAX_STREAMS:
            break
    return out


def tag_stream_count(flows: object) -> int:
    """How many tag streams a `get_flows` payload holds, whether or not
    each can have a note (`notes_for` may return fewer: the cap, a tag no
    file name can be made from, two tags that share a name)."""
    streams = flows.get("streams") if isinstance(flows, dict) else None
    return sum(1 for stream in streams if stream_key(stream)) if isinstance(streams, list) else 0


__all__ = ["tag_stream_count", "notes_for", "render_body", "note_name", "stream_key", "NOTICE", "MAX_STREAMS"]
