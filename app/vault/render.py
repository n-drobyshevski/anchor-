"""Rendering the database into files (phase-8 plan sections 4.1 and 4.2).

Pure functions: rows in, text and a digest out. No session, no client.
The sync pass (app/vault/sync.py) gathers the rows and does the I/O.

**The digest decides whether a file needs rewriting** (plan 4.1). It is
the sha256 of a canonical JSON of Anchor's own keys, in their fixed
order, plus the body -- never `last_used_at`, `use_count` or the user's
extra properties. Rendering an unchanged fact yields the same digest,
so a pass over an unchanged database writes nothing, and an edit the
user made to their own properties never triggers a rewrite.

**Anchor-created names are ASCII** (`0142-k3f9qa.md`), which sidesteps
NFC/NFD drift between macOS and Linux. The epoch in every name is what
keeps a file from before a /delete from ever sharing a path with one
after it.

**The callout says what is true now.** In `mirror` -- the only mode
8b implements; `sync` acts as mirror until 8c -- editing a file changes
nothing, so the fact callout says that instead of plan 4.1's text. The
wording changes with 8c, and that one-time rewrite of every fact file
is paced by the write cap like any other.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass

import yaml

FACT_KEYS = ("anchor", "anchor_epoch", "anchor_id", "kind", "pinned", "fact", "source", "created")
JOURNAL_KEYS = ("anchor", "anchor_epoch", "date")

FACT_CALLOUT_MIRROR = (
    "> [!note] Anchor\n"
    "> Это копия факта из Anchor. Пока правки здесь не применяются: следующее\n"
    "> изменение факта в Anchor перезапишет файл.\n"
)
# sync mode (plan section 4.1): fact/kind/pinned are genuinely editable
# here, so the callout says so instead of mirror's "not applied yet".
# Swapping this in changes every fact file's digest, so the first sync
# pass after enabling `sync` rewrites every file once -- paced by the
# write budget like any other write (plan section 4.1's own note).
FACT_CALLOUT_SYNC = (
    "> [!note] Anchor\n"
    "> Меняй `fact`, `kind` и `pinned`. Удали файл — Anchor забудет этот факт.\n"
    "> Остальное ведёт Anchor.\n"
)
JOURNAL_CALLOUT = (
    "> [!note] Anchor\n"
    "> Этот файл пишет Anchor. Если изменишь или удалишь его, Anchor больше не будет его трогать.\n"
)
HISTORY_HEADER = "## Раньше"
SOURCE_LINE = "Источник: {domain}"
CHECKIN_HEADER = "## Чек-ин"
JOURNAL_HEADER = "## Журнал"
RATING_LINE = "- Оценка дня: {rating}/5"
DUE_LINE = "- Главное действие: {label}"
NOTE_LINE = "- Заметка: {note}"


@dataclass(frozen=True)
class FactView:
    memory_id: int
    kind: str
    text: str
    pinned: bool
    source: str
    created: datetime.date
    # (local date, text) of each predecessor, newest first.
    history: tuple[tuple[datetime.date, str], ...] = ()
    # (domain, verbatim quote) for a technique, from its card and clip.
    technique_source: tuple[str, str] | None = None


@dataclass(frozen=True)
class JournalView:
    local_date: datetime.date
    day_rating: int | None = None
    due_label: str | None = None
    note: str | None = None
    entries: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return (
            self.day_rating is None
            and self.due_label is None
            and not self.note
            and not self.entries
        )


@dataclass(frozen=True)
class Rendered:
    content: str
    digest: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def fact_path(memory_id: int, epoch: str) -> str:
    return f"Anchor/Memory/{memory_id:04d}-{epoch}.md"


def journal_path(local_date: datetime.date, epoch: str) -> str:
    return f"Anchor/Journal/{local_date.isoformat()}-{epoch}.md"


def _one_line(text: str) -> str:
    """Collapse whitespace so a stored text can never break a list item."""
    return " ".join(text.split())


def dump_keys(keys: dict) -> str:
    """Anchor's keys as YAML: Russian stays Russian, nothing is folded.

    `safe_dump` quotes any string that would otherwise read back as
    another type -- "2026-09-20", "no", "123" -- so a fact that happens
    to look like a boolean still round-trips as text.
    """
    return yaml.safe_dump(
        keys, allow_unicode=True, sort_keys=False, width=1_000_000, default_flow_style=False
    )


def _digest(keys: dict, body: str) -> str:
    canonical = json.dumps(
        {"keys": list(keys.items()), "body": body},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assemble(keys: dict, extras: str, body: str) -> str:
    return "---\n" + dump_keys(keys) + extras + "---\n" + body


def fact_keys(view: FactView, epoch: str) -> dict:
    return {
        "anchor": "fact",
        "anchor_epoch": epoch,
        "anchor_id": view.memory_id,
        "kind": view.kind,
        "pinned": view.pinned,
        "fact": view.text,
        "source": view.source,
        "created": view.created.isoformat(),
    }


def fact_body(view: FactView, callout: str = FACT_CALLOUT_MIRROR) -> str:
    parts = [callout]
    if view.technique_source is not None:
        domain, quote = view.technique_source
        parts.append("\n" + SOURCE_LINE.format(domain=_one_line(domain)) + "\n")
        parts.append("\n> " + _one_line(quote) + "\n")
    if view.history:
        lines = [f"- {day.isoformat()} — {_one_line(text)}" for day, text in view.history]
        parts.append("\n" + HISTORY_HEADER + "\n" + "\n".join(lines) + "\n")
    return "".join(parts)


def render_fact(
    view: FactView, epoch: str, extras: str = "", callout: str = FACT_CALLOUT_MIRROR
) -> Rendered:
    keys = fact_keys(view, epoch)
    body = fact_body(view, callout)
    return Rendered(_assemble(keys, extras, body), _digest(keys, body))


def render_journal(view: JournalView, epoch: str) -> Rendered:
    keys = {"anchor": "journal", "anchor_epoch": epoch, "date": view.local_date.isoformat()}
    parts = [JOURNAL_CALLOUT]
    checkin_lines = []
    if view.day_rating is not None:
        checkin_lines.append(RATING_LINE.format(rating=view.day_rating))
    if view.due_label is not None:
        checkin_lines.append(DUE_LINE.format(label=view.due_label))
    if view.note:
        checkin_lines.append(NOTE_LINE.format(note=_one_line(view.note)))
    if checkin_lines:
        parts.append("\n" + CHECKIN_HEADER + "\n" + "\n".join(checkin_lines) + "\n")
    if view.entries:
        lines = [f"- {_one_line(entry)}" for entry in view.entries]
        parts.append("\n" + JOURNAL_HEADER + "\n" + "\n".join(lines) + "\n")
    body = "".join(parts)
    return Rendered(_assemble(keys, "", body), _digest(keys, body))
