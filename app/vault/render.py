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

**L3 adds a third file kind**, the lens garden's weekly report
(`render_report`, L3 spec section 3): the same digest rule, and every
model-written text escaped so the note holds no link, tag or embed the
model chose.
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


# --- L3: the lens garden's report (L3 spec section 3) ------------------------

REPORT_KEYS = ("anchor", "anchor_epoch", "anchor_week")
REPORT_MAX_BYTES = 60 * 1024
"""The note's cap (L3 spec section 3). «Структура» goes first, then
the newest of «Ещё открыто» (it lists the oldest first, and a section
keeps its head), then this run's own gaps past the cap; each cut says
how many it left out, even one that keeps no line. Telegram has the
run's own gaps either way."""

REPORT_CALLOUT = (
    "> [!note] Echo\n"
    "> Этот отчёт пишет Echo: сад линзы раз в неделю. Правки здесь Echo не читает —\n"
    "> отмечай предложения кнопками в Telegram. Если изменишь или удалишь файл,\n"
    "> Echo больше не будет его трогать.\n"
)
REPORT_HEADING = "# Сад линзы, {week}"
REPORT_KIND_HEADERS = {
    "link": "## Связи",
    "missing_note": "## Недостающие заметки",
    "tension": "## Напряжения",
    "bridge": "## Мосты",
}
REPORT_STILL_OPEN_HEADER = "## Ещё открыто"
REPORT_STRUCTURE_HEADER = "## Структура"
REPORT_STATUS = {
    "open": "открыто",
    "done": "сделано, проверю в следующем саду",
    "dismissed": "не нужно",
    "resolved": "закрыто",
    "researched": "исследовано",
}
REPORT_REOPENED = "снова"
REPORT_EMPTY = "Предложений нет."
REPORT_MORE = "- …и ещё {count}"
REPORT_STRUCTURE_LINES = {
    "hubs": "Узлы",
    "clusters": "Группы",
    "orphans": "Без связей",
    "dead_ends": "Тупики",
    "wanted": "Нужны заметки",
}

# Obsidian syntax that model text or a note title could smuggle into
# the note: a wikilink or embed (`[[`, `]]`, `![[`), a link's alias or
# a table cell (`|`), a tag or heading (`#`), a markdown link
# (`[x](y)`), an HTML tag or autolink (`<...>`), and a comment that
# would hide the rest of the line (`%%`). Brackets, the bar, the hash
# and angle brackets become full-width or lookalike characters, which
# Obsidian reads as plain text; `://` gets a space, so no URL
# autolinks; `%%` is split.
_REPORT_ESCAPES = str.maketrans(
    {"[": "［", "]": "］", "|": "｜", "#": "＃", "<": "‹", ">": "›"}
)
# A title that holds any of these cannot be a wikilink target as is,
# so it is shown as escaped text instead of a link.
_LINK_UNSAFE = ("[", "]", "|", "#", "^", "\n", "\r")


def report_escape(text: str) -> str:
    """Model text, a stored title or a wanted note's text, made inert: one
    line, no wikilink, link, tag, heading, table cell, HTML or comment
    (L3 spec section 3). Never empty-safe on its own -- callers skip
    empty values."""
    flat = _one_line(text).translate(_REPORT_ESCAPES)
    return flat.replace("://", ": //").replace("%%", "% %")


def _report_link(note_id: object, lens_titles: dict, fallback: str | None = None) -> str | None:
    """A `[[link]]` to a lens note, the only notes the report links to
    (W2 cannot rename them, so a link never blocks a rename it should
    not). A note that has left the lens, or a title Obsidian could not
    link to, falls back to escaped text; None if there is nothing to show."""
    title = lens_titles.get(note_id) if isinstance(note_id, int) else None
    if title:
        if not any(ch in title for ch in _LINK_UNSAFE):
            return f"[[{title}]]"
        return report_escape(title)
    if fallback and fallback.strip():
        return report_escape(fallback)
    return None


def _report_gap_line(gap, lens_titles: dict) -> str:
    stored = list(gap.titles)
    notes = []
    for index, note_id in enumerate(gap.note_ids):
        shown = _report_link(note_id, lens_titles, stored[index] if index < len(stored) else None)
        if shown is not None:
            notes.append(shown)
    parts = []
    if gap.title:
        # A missing note's proposed title: plain text, never a link --
        # the note does not exist, and a link would create it on a tap.
        parts.append(f"«{report_escape(gap.title)}»")
    if notes:
        parts.append(" · ".join(notes))
    head = " — ".join(parts)
    status = REPORT_STATUS.get(gap.status, report_escape(gap.status))
    if gap.reopened:
        status = f"{status}, {REPORT_REOPENED}"
    detail = report_escape(gap.detail) if gap.detail else ""
    line = f"- {head}: {detail}" if head and detail else f"- {head or detail}"
    return f"{line} _({status})_"


def _finding_ids(values: object) -> list:
    """Ids from a findings list: ints, or objects carrying an `id`."""
    ids = []
    for value in values if isinstance(values, list) else ():
        if isinstance(value, dict):
            value = value.get("id")
        if isinstance(value, int) and not isinstance(value, bool):
            ids.append(value)
    return ids


def _structure_lines(findings: dict, lens_titles: dict) -> list[str]:
    """«Структура» from the run's findings (app/core/lens_graph.py's step
    1, stored as `lens_garden_run.findings`). Read tolerantly, since the
    note must render whatever an older run stored:

    - `hubs`, `orphans`, `dead_ends`: lists of lens note ids, or of
      objects with an `id` (a hub's score is not shown);
    - `clusters`: objects with an optional `name` (the model's, escaped)
      and their `members` (or `note_ids`);
    - `wanted`: unresolved link texts from lens notes, as strings or
      objects with a `text`: plain escaped text, never a link.

    Ids that are not lens notes now are left out: the report links to
    lens notes only, and names nothing else."""

    def notes(ids: list) -> str:
        shown = [_report_link(i, lens_titles) for i in ids if i in lens_titles]
        return ", ".join(s for s in shown if s)

    lines = []
    for key in ("hubs", "orphans", "dead_ends"):
        shown = notes(_finding_ids(findings.get(key)))
        if shown:
            lines.append(f"- {REPORT_STRUCTURE_LINES[key]}: {shown}")
    clusters = []
    for cluster in findings.get("clusters") or ():
        if not isinstance(cluster, dict):
            continue
        members = notes(_finding_ids(cluster.get("members", cluster.get("note_ids"))))
        if not members:
            continue
        name = cluster.get("name")
        label = report_escape(name) if isinstance(name, str) and name.strip() else ""
        clusters.append(f"  - {label}: {members}" if label else f"  - {members}")
    if clusters:
        lines.append(f"- {REPORT_STRUCTURE_LINES['clusters']}:")
        lines.extend(clusters)
    wanted = []
    for item in findings.get("wanted") or ():
        text = item.get("text") if isinstance(item, dict) else item
        if isinstance(text, str) and text.strip():
            wanted.append(report_escape(text))
    if wanted:
        lines.append(f"- {REPORT_STRUCTURE_LINES['wanted']}: " + ", ".join(wanted))
    return lines


def _fit(lines: list[str], room: int) -> tuple[list[str], int]:
    """As many of `lines` as fit in `room` bytes, each with its newline,
    and how many were left out."""
    kept, used = [], 0
    for line in lines:
        size = len(line.encode("utf-8")) + 1
        if used + size > room:
            break
        kept.append(line)
        used += size
    return kept, len(lines) - len(kept)


def render_report(data, epoch: str) -> Rendered:
    """The lens garden's note for one run (L3 spec section 3). Pure.

    `data` is app/vault/lens.py's `ReportData` (passed in, not imported:
    this module stays out of the lens module's importers): the run's
    week, its gaps with their status now, the open gaps of earlier runs,
    the run's findings and the current lens titles by lens note id.

    Frontmatter `anchor: report`, `anchor_epoch`, `anchor_week`; a
    callout saying edits here are not read; the run's gaps by kind; «Ещё
    открыто»; «Структура». Every text is escaped (`report_escape`), and
    only lens notes are links. At most REPORT_MAX_BYTES: «Структура» is
    dropped first, then «Ещё открыто» is cut, then the run's own gaps.
    The digest covers the keys and the body, as for a journal day, so a
    tap in Telegram that changes a status rewrites the note once."""
    keys = {"anchor": "report", "anchor_epoch": epoch, "anchor_week": data.iso_week}
    head = [REPORT_CALLOUT.rstrip("\n"), "", REPORT_HEADING.format(week=report_escape(data.iso_week))]
    titles = data.lens_titles

    own_sections: list[tuple[str, list[str]]] = []
    for kind, header in REPORT_KIND_HEADERS.items():
        lines = [_report_gap_line(gap, titles) for gap in data.gaps if gap.kind == kind]
        if lines:
            own_sections.append((header, lines))
    other = [_report_gap_line(gap, titles) for gap in data.gaps if gap.kind not in REPORT_KIND_HEADERS]
    if other:
        own_sections.append(("## Другое", other))
    still_open = [_report_gap_line(gap, titles) for gap in data.still_open]
    structure = _structure_lines(data.findings or {}, titles)

    frontmatter_size = len(("---\n" + dump_keys(keys) + "---\n").encode("utf-8"))
    budget = REPORT_MAX_BYTES - frontmatter_size

    def size(lines: list[str]) -> int:
        return sum(len(line.encode("utf-8")) + 1 for line in lines)

    def block(header: str, lines: list[str]) -> list[str]:
        return ["", header, *lines]

    body_lines = list(head)
    if not own_sections:
        body_lines += ["", REPORT_EMPTY]
    sections = [block(header, lines) for header, lines in own_sections]
    if still_open:
        sections.append(block(REPORT_STILL_OPEN_HEADER, still_open))
    if structure:
        sections.append(block(REPORT_STRUCTURE_HEADER, structure))
    if size(body_lines) + sum(size(section) for section in sections) <= budget:
        body_lines += [line for section in sections for line in section]
    else:
        # Over the cap: «Структура» goes first, then each section keeps
        # what fits (this run's own gaps before «Ещё открыто»). Room for
        # a "…и ещё N" line is reserved only in a section that is cut,
        # and a section that keeps no line still says how many it had,
        # when its header and that line fit.
        more_room = len(REPORT_MORE.format(count=10**6).encode("utf-8")) + 1
        rest = budget - size(body_lines)
        trimmed = list(own_sections) + ([(REPORT_STILL_OPEN_HEADER, still_open)] if still_open else [])
        for header, lines in trimmed:
            head_room = rest - size(["", header])
            kept, left = _fit(lines, max(0, head_room))
            if left:
                kept, left = _fit(lines, max(0, head_room - more_room))
                if not kept and head_room < more_room:
                    continue
            section = block(header, kept + ([REPORT_MORE.format(count=left)] if left else []))
            body_lines += section
            rest -= size(section)
    body = "\n".join(body_lines) + "\n"
    return Rendered(_assemble(keys, "", body), _digest(keys, body))
