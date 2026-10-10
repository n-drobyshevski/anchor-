"""Footer link pruning: keep the link graph of knowledge and lens notes lean.

`GET /v1/graph/prune` is a dry run: the plan and a Markdown report.
`POST /v1/graph/prune` applies the plan the caller read, and only that
plan (its hash must still match), as one undoable Echo changeset.

**The graph.** Nodes are the notes `graph.build_graph` shows: knowledge
and lens notes, nothing under `Anchor/`. Links resolve exactly as
there (graph.py's index and pick), and links to hidden or missing
notes are not edges. It is undirected: A and B are connected if either
links the other, from its body or its footer.

**Body and footer.** The footer is the last line that equals the
configured heading (default `## Связи`) up to the next heading or the
end of the file; the body is the rest after the frontmatter. Links are
read with `%% asides %%`, fenced code and inline code removed, and
embeds (`![[x]]`) and attachments are never links.

**The rule** (the one the user applied by hand once; keep it identical).
A connection A-B may be removed only if all of these hold:

1. Every direction that exists is footer only: neither note links the
   other from its body. Body links are never touched: the vault is
   dense clusters, and pruning body links would remove about half of
   it, obvious pairs included.
2. After removal A and B still share a neighbour.
3. The whole graph stays as connected as it was. Rule 2 implies it
   (A-w-B is still a path); `plan` checks the component count anyway.
4. Neither the pair nor either note is protected: `Anchor/graph.md`'s
   `protected_pairs` and `protected` (a note name, a note path or a
   folder), or a `graph-keep: [Target, ...]` list in either note's own
   frontmatter, which this module honours and never writes.

Also kept: a footer link that is not alone in its item (`[[A]] и [[B]]`
between two separators), since removing it would mean editing prose.

Candidates go greedily, highest degree sum first (degrees before any
removal; ties by path), and rule 2 is re-checked against the current
graph after every removal, because removals can cancel each other's
detours.

**Applying** edits footers only: the item and one adjacent separator go,
no doubled or dangling separator is left, a grouped label line
(`**Люди:** [[A]] · [[B]]`) that ends up empty is dropped, and every
other byte stays as it was. The frontmatter gets Echo's provenance
stamp (provenance.py) and nothing else. Each file is compare-and-swap
written against what the plan read; if one changed in between, every
file already written is put back and the apply is refused as
`plan_changed`. The run is idempotent: right after an apply, the plan
is empty.

Nothing here logs a path, a title or text; api.py logs counts only.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from vaultd import classes, frontmatter, graph, links, paths, provenance
from vaultd.config import NOTE_MAX_BYTES, PRUNE_MAX_FILES, TREE_MAX_NOTES
from vaultd.store import Conflict, Missing, Store, sha256

CONFIG_PATH = "Anchor/graph.md"
DEFAULT_HEADING = "## Связи"
DEFAULT_SEPARATOR = " · "
DEFAULT_SMALL_COMPONENT = 3
KEEP_KEY = "graph-keep"
_CONFIG_KEYS = frozenset({"protected_pairs", "protected", "footer_heading", "separator", "small_component_max"})

_ANCHOR_TOP = "Anchor"
_COMMENT = re.compile(r"%%.*?%%", re.DOTALL)
_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.DOTALL | re.MULTILINE)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_HEADING = re.compile(r"^#{1,6}\s")
# What precedes a footer line's items: a list marker, a bold label, or both.
_LABEL = re.compile(r"^(?P<label>\s*(?:[-*+][ \t]+)?(?:\*\*[^*\n]+?\*\*:?[ \t]*)?)")
_ONE_LINK = re.compile(r"^\s*\[\[(?P<inner>[^\[\]]+)\]\]\s*$")


class ConfigError(Exception):
    """`Anchor/graph.md` is unusable; the message names what, never a note."""


class PlanChanged(Exception):
    """The vault no longer matches the plan the caller read."""


class TooLarge(Exception):
    """More notes than the graph shows, or more files than one apply may touch."""


@dataclass(frozen=True)
class Config:
    heading: str = DEFAULT_HEADING
    separator: str = DEFAULT_SEPARATOR
    small_component_max: int = DEFAULT_SMALL_COMPONENT
    protected: tuple[str, ...] = ()
    protected_pairs: tuple[tuple[str, str], ...] = ()


def _str_list(value, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{key}: expected a list of names")
    return tuple(unicodedata.normalize("NFC", v.strip()) for v in value)


def parse_config(data: bytes | None) -> Config:
    """`Anchor/graph.md`'s frontmatter, strictly: an unknown key or a bad
    value is an error, so a typo never silently unprotects a pair."""
    if data is None:
        return Config()
    if frontmatter.note_mark(data) == "unknown":
        raise ConfigError("frontmatter does not parse")
    loaded = frontmatter.load(data) or {}
    unknown = sorted(set(loaded) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"unknown key: {unknown[0]}")
    heading = loaded.get("footer_heading", DEFAULT_HEADING)
    if not isinstance(heading, str) or not _HEADING.match(heading.strip() + " "):
        raise ConfigError("footer_heading: expected a Markdown heading")
    separator = loaded.get("separator", DEFAULT_SEPARATOR)
    if not isinstance(separator, str) or not separator.strip():
        raise ConfigError("separator: expected a non-blank string")
    small = loaded.get("small_component_max", DEFAULT_SMALL_COMPONENT)
    if isinstance(small, bool) or not isinstance(small, int) or not 1 <= small <= 50:
        raise ConfigError("small_component_max: expected 1-50")
    pairs_raw = loaded.get("protected_pairs") or []
    if not isinstance(pairs_raw, list):
        raise ConfigError("protected_pairs: expected a list of pairs")
    pairs = []
    for pair in pairs_raw:
        names = _str_list(pair, "protected_pairs")
        if len(names) != 2:
            raise ConfigError("protected_pairs: each pair needs exactly two names")
        pairs.append((names[0], names[1]))
    return Config(
        heading=heading.strip(),
        separator=separator,
        small_component_max=small,
        protected=_str_list(loaded.get("protected"), "protected"),
        protected_pairs=tuple(pairs),
    )


# -- parsing one note -------------------------------------------------------------


@dataclass
class _Item:
    line: int  # index into the footer's lines
    text: str
    simple_target: str | None  # the one link's target text, if the item is exactly one link
    targets: list[str]


@dataclass
class _Note:
    rel: str
    data: bytes
    body_targets: list[str]
    items: list[_Item]
    footer_span: tuple[int, int] | None  # character offsets into the decoded text after frontmatter
    keep: tuple[str, ...] | None  # None: an unusable graph-keep, which protects the whole note


def _link_text(text: str) -> str:
    text = _COMMENT.sub("", text)
    text = _FENCE.sub("", text)
    return _INLINE_CODE.sub("", text)


def _targets(text: str) -> list[str]:
    out = []
    for match in links._LINK_RE.finditer(_link_text(text)):  # noqa: SLF001 - same package
        if match.group("bang"):
            continue
        target, _h, _l, _s = links._parse_inner(match.group("inner"))  # noqa: SLF001
        if target.strip() and not graph._ATTACHMENT_RE.search(target):  # noqa: SLF001
            out.append(target)
    return out


def _footer_span(text: str, heading: str) -> tuple[int, int] | None:
    """(start of the heading line, end of the footer) in `text`, or None."""
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    found = None
    for i, line in enumerate(lines):
        if line.rstrip("\r\n").strip() == heading:
            found = i
    if found is None:
        return None
    end = len(text)
    for j in range(found + 1, len(lines)):
        if _HEADING.match(lines[j]):
            end = offsets[j]
            break
    return offsets[found], end


def _split_line(line: str) -> tuple[str, str, str, str]:
    """(label, items text, trailing whitespace, line ending) of one footer line."""
    ending = ""
    if line.endswith("\r\n"):
        line, ending = line[:-2], "\r\n"
    elif line.endswith("\n"):
        line, ending = line[:-1], "\n"
    stripped = line.rstrip(" \t")
    trailing = line[len(stripped) :]
    match = _LABEL.match(stripped)
    label = match.group("label") if match else ""
    return label, stripped[len(label) :], trailing, ending


def _footer_lines(text: str, span: tuple[int, int]) -> list[str]:
    # The heading line itself is line 0 and never edited.
    return text[span[0] : span[1]].splitlines(keepends=True)


def _parse_items(lines: list[str], separator: str) -> list[_Item]:
    items: list[_Item] = []
    for i, line in enumerate(lines):
        if i == 0:
            continue
        _label, rest, _trailing, _ending = _split_line(line)
        if "[[" not in rest:
            continue
        for part in rest.split(separator):
            targets = _targets(part)
            if not targets:
                continue
            simple = targets[0] if _ONE_LINK.match(part) and len(targets) == 1 else None
            items.append(_Item(i, part, simple, targets))
    return items


def _keep_list(data: bytes) -> tuple[str, ...] | None:
    loaded = frontmatter.load(data)
    if not loaded or KEEP_KEY not in loaded:
        return ()
    value = loaded[KEEP_KEY]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    out = []
    for v in value:
        v = v.strip()
        if v.startswith("[[") and v.endswith("]]"):
            v = v[2:-2]
        target, _h, _l, _s = links._parse_inner(v)  # noqa: SLF001
        if target.strip():
            out.append(target)
    return tuple(out)


def parse_note(rel: str, data: bytes, heading: str, separator: str) -> _Note:
    text = frontmatter.body(data).decode("utf-8")
    span = _footer_span(text, heading)
    if span is None:
        body_text, items = text, []
    else:
        body_text = text[: span[0]] + text[span[1] :]
        items = _parse_items(_footer_lines(text, span), separator)
    return _Note(rel, data, _targets(body_text), items, span, _keep_list(data))


# -- the plan -------------------------------------------------------------------


@dataclass(frozen=True)
class Removal:
    a: str
    b: str
    detour: str
    edits: tuple[tuple[str, str], ...]  # (note whose footer changes, the linked note) per direction


@dataclass
class Plan:
    config_error: str | None = None
    notes: int = 0
    edges_before: int = 0
    edges_after: int = 0
    components_before: int = 0
    components_after: int = 0
    removals: list[Removal] = field(default_factory=list)
    unresolved: dict[str, int] = field(default_factory=dict)
    orphans: list[str] = field(default_factory=list)
    small_components: list[list[str]] = field(default_factory=list)
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    file_hashes: dict[str, str] = field(default_factory=dict)  # every note the plan edits, as read
    # For apply_plan only, never reported.
    cfg: Config = field(default_factory=Config, repr=False)
    index: object = field(default=None, repr=False)
    visible: dict[str, bytes] = field(default_factory=dict, repr=False)

    @property
    def files(self) -> list[str]:
        return sorted(self.file_hashes)

    @property
    def plan_hash(self) -> str:
        """What the caller read: the edits and the bytes they apply to."""
        payload = {
            "edits": sorted({e for r in self.removals for e in r.edits}),
            "files": sorted(self.file_hashes.items()),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()[:32]


def title(rel: str) -> str:
    return links.basename(rel)


def _components(nodes, adj) -> list[list[str]]:
    seen: set[str] = set()
    out = []
    for start in sorted(nodes):
        if start in seen:
            continue
        seen.add(start)
        comp, queue = [], deque([start])
        while queue:
            n = queue.popleft()
            comp.append(n)
            for m in adj[n]:
                if m not in seen:
                    seen.add(m)
                    queue.append(m)
        out.append(sorted(comp))
    return out


def _protected_note(rel: str, cfg: Config) -> bool:
    nfc = unicodedata.normalize("NFC", rel)
    stem = nfc[:-3] if nfc.endswith(".md") else nfc
    name = title(rel).casefold()
    for entry in cfg.protected:
        e = entry.rstrip("/")
        if "/" in e or e.endswith(".md"):
            e_stem = e[:-3] if e.endswith(".md") else e
            if stem == e_stem or nfc.startswith(e + "/"):
                return True
        elif e.casefold() == name:
            return True
    return False


def _protected_pair(a: str, b: str, cfg: Config) -> bool:
    pair = {title(a).casefold(), title(b).casefold()}
    return any({x.casefold(), y.casefold()} == pair for x, y in cfg.protected_pairs)


def _visible(root: Path, rules: classes.FolderRules) -> tuple[graph._Index, dict[str, bytes]]:
    index = graph._Index()  # noqa: SLF001 - same package
    visible: dict[str, bytes] = {}
    for rel, data in links.iter_notes(root):
        index.add(rel)
        if unicodedata.normalize("NFC", rel).split("/", 1)[0] == _ANCHOR_TOP:
            continue
        if len(data) > NOTE_MAX_BYTES:
            continue
        mark = frontmatter.note_mark(data)
        if mark == "unknown":
            continue
        if classes.effective_class(rel, mark, rules).note_class not in classes.READABLE_KNOWLEDGE:
            continue
        visible[rel] = data
    if rules.state == "ok":
        index.add(classes.SETTINGS_PATH)
    return index, visible


def build_plan(root: Path) -> Plan:
    """The dry run: everything the report shows, and the removals in order."""
    try:
        cfg = parse_config(paths.read_file(root, CONFIG_PATH))
    except paths.Refused:
        return Plan(config_error="Anchor/graph.md is a symlink")
    except ConfigError as exc:
        return Plan(config_error=str(exc))
    rules = classes.load_rules(root)
    if rules.state == "invalid":
        return Plan(config_error="Anchor/settings.md is unusable")
    index, visible = _visible(root, rules)
    if len(visible) > TREE_MAX_NOTES:
        raise TooLarge
    notes = {rel: parse_note(rel, data, cfg.heading, cfg.separator) for rel, data in visible.items()}

    def resolve(src: str, target: str) -> str | None:
        found = graph._candidates(target, index)  # noqa: SLF001
        if not found:
            return None
        chosen = graph._pick(src, found)  # noqa: SLF001
        return chosen if chosen in visible and chosen != src else ""

    plan = Plan(notes=len(notes))
    body: dict[str, set[str]] = {}
    footer: dict[str, set[str]] = {}
    complex_footer: dict[str, set[str]] = {}
    keep: dict[str, set[str] | None] = {}
    unresolved: dict[str, set[str]] = {}
    for rel, note in notes.items():
        body[rel], footer[rel], complex_footer[rel] = set(), set(), set()
        for t in note.body_targets:
            dst = resolve(rel, t)
            if dst is None:
                unresolved.setdefault(unicodedata.normalize("NFC", t.strip()), set()).add(rel)
            elif dst:
                body[rel].add(dst)
        for item in note.items:
            for t in item.targets:
                dst = resolve(rel, t)
                if dst is None:
                    unresolved.setdefault(unicodedata.normalize("NFC", t.strip()), set()).add(rel)
                elif dst:
                    footer[rel].add(dst)
                    if item.simple_target is None:
                        complex_footer[rel].add(dst)
        if note.keep is None:
            keep[rel] = None
        else:
            keep[rel] = {d for t in note.keep if (d := resolve(rel, t))}
        dup = sorted(title(d) for d in body[rel] & footer[rel])
        if dup:
            plan.duplicates[rel] = dup
    plan.unresolved = {t: len(srcs) for t, srcs in unresolved.items()}

    adj: dict[str, set[str]] = {rel: set() for rel in notes}
    for rel in notes:
        for dst in body[rel] | footer[rel]:
            adj[rel].add(dst)
            adj[dst].add(rel)
    plan.edges_before = sum(len(v) for v in adj.values()) // 2
    comps = _components(notes, adj)
    plan.components_before = len(comps)

    degree = {rel: len(adj[rel]) for rel in notes}
    candidates = []
    for a in sorted(notes):
        for b in sorted(adj[a]):
            if b <= a:
                continue
            if b in body[a] or a in body[b]:
                continue  # rule 1
            if b in complex_footer[a] or a in complex_footer[b]:
                continue
            if keep[a] is None or keep[b] is None or b in keep[a] or a in keep[b]:
                continue  # rule 4: graph-keep
            if _protected_note(a, cfg) or _protected_note(b, cfg) or _protected_pair(a, b, cfg):
                continue  # rule 4
            candidates.append((-(degree[a] + degree[b]), a, b))
    candidates.sort()

    for _neg, a, b in candidates:
        common = (adj[a] & adj[b]) - {a, b}
        if not common:
            continue  # rule 2, against the current graph
        detour = min(common, key=lambda r: (title(r).casefold(), r))
        adj[a].discard(b)
        adj[b].discard(a)
        edits = tuple(sorted((src, dst) for src, dst in ((a, b), (b, a)) if dst in footer[src]))
        plan.removals.append(Removal(a, b, detour, edits))

    plan.edges_after = sum(len(v) for v in adj.values()) // 2
    comps_after = _components(notes, adj)
    plan.components_after = len(comps_after)
    if plan.components_after != plan.components_before:  # rule 3; rule 2 makes this unreachable
        raise AssertionError("pruning would disconnect the graph")
    largest = max((len(c) for c in comps_after), default=0)
    plan.orphans = sorted((c[0] for c in comps_after if len(c) == 1), key=lambda r: title(r).casefold())
    plan.small_components = [
        c for c in comps_after if 1 < len(c) <= cfg.small_component_max and len(c) < largest
    ]
    plan.file_hashes = {src: sha256(visible[src]) for r in plan.removals for src, _dst in r.edits}
    if len(plan.file_hashes) > PRUNE_MAX_FILES:
        raise TooLarge
    plan.cfg, plan.index, plan.visible = cfg, index, visible
    return plan


# -- editing one footer -----------------------------------------------------------


def edit_footer(data: bytes, drop: Callable[[str], bool], heading: str, separator: str) -> bytes:
    """`data` with every footer item whose one link `drop(target)` accepts
    removed (module docstring, "Applying"). Bytes outside the footer's
    item lines, the frontmatter included, are returned unchanged."""
    fm_len = len(data) - len(frontmatter.body(data))
    head, text = data[:fm_len], frontmatter.body(data).decode("utf-8")
    span = _footer_span(text, heading)
    if span is None:
        return data
    lines = _footer_lines(text, span)
    out = [lines[0]]
    for line in lines[1:]:
        label, rest, trailing, ending = _split_line(line)
        if "[[" not in rest:
            out.append(line)
            continue
        parts = rest.split(separator)
        kept = [i for i, part in enumerate(parts) if not _droppable(part, drop)]
        if len(kept) == len(parts):
            out.append(line)
            continue
        if not kept:
            continue  # a line, labelled or not, left with no items goes
        items = [parts[i] for i in kept]
        if kept[0] != 0:
            # A new first item takes the old first item's leading whitespace.
            lead = parts[0][: len(parts[0]) - len(parts[0].lstrip())]
            items[0] = lead + items[0].lstrip()
        out.append(label + separator.join(items) + trailing + ending)
    new_text = text[: span[0]] + "".join(out) + text[span[1] :]
    return head + new_text.encode("utf-8")


def _droppable(part: str, drop: Callable[[str], bool]) -> bool:
    if not _ONE_LINK.match(part):
        return False
    found = _targets(part)
    return len(found) == 1 and drop(found[0])


def _note_edits(plan: Plan) -> dict[str, set[str]]:
    edits: dict[str, set[str]] = {}
    for r in plan.removals:
        for src, dst in r.edits:
            edits.setdefault(src, set()).add(dst)
    return edits


def apply_plan(store: Store, expected_hash: str, *, now: Callable[[], str]) -> tuple[Plan, list[tuple[str, bytes, str]]]:
    """Recompute, check the hash, write. Returns the plan and, per file,
    (rel, pre-image, written sha256). Runs under the caller's lock."""
    plan = build_plan(store.vault_path)
    if plan.config_error is not None or plan.plan_hash != expected_hash:
        raise PlanChanged
    cfg, index, visible = plan.cfg, plan.index, plan.visible
    written: list[tuple[str, bytes, str]] = []
    for src, dsts in sorted(_note_edits(plan).items()):
        current = visible[src]

        def drop(target: str, src: str = src, dsts: set[str] = dsts) -> bool:
            found = graph._candidates(target, index)  # noqa: SLF001
            return bool(found) and graph._pick(src, found) in dsts  # noqa: SLF001

        new = edit_footer(current, drop, cfg.heading, cfg.separator)
        stamped = provenance.apply(new, now(), by="echo")
        try:
            sha = store.put_unchecked(src, stamped, plan.file_hashes[src])
        except (Conflict, Missing, paths.Refused):
            _rollback(store, written)
            raise PlanChanged from None
        written.append((src, current, sha))
    return plan, written


def _rollback(store: Store, written: list[tuple[str, bytes, str]]) -> None:
    for rel, pre_image, sha in reversed(written):
        try:
            store.put_unchecked(rel, pre_image, sha)
        except (Conflict, Missing, paths.Refused):
            pass


# -- the report -------------------------------------------------------------------


def _link(rel: str) -> str:
    return f"[[{title(rel)}]]"


def report(plan: Plan, *, when: str) -> str:
    """The dry run as Markdown (Russian, like the rest of what Echo writes)."""
    out = [f"# Граф ссылок: сухой прогон {when}", ""]
    if plan.config_error is not None:
        out += [f"Настройки `{CONFIG_PATH}` не читаются: {plan.config_error}.", "Ничего не предложено."]
        return "\n".join(out) + "\n"
    files = plan.files
    out += [
        f"- Заметок: {plan.notes}",
        f"- Связей: {plan.edges_before} → {plan.edges_after} (−{len(plan.removals)})",
        f"- Компонент связности: {plan.components_before} → {plan.components_after}",
        f"- Заметок, у которых изменится футер: {len(files)}",
        f"- План: `{plan.plan_hash}`",
        "",
        "## Предлагаемые удаления",
        "",
    ]
    if not plan.removals:
        out.append("Нечего удалять.")
    by_note: dict[str, list[str]] = {}
    for r in plan.removals:
        for src, dst in r.edits:
            by_note.setdefault(src, []).append(f"- {_link(dst)} — обход через {_link(r.detour)}")
    for src in sorted(by_note, key=lambda r: title(r).casefold()):
        out += [f"### {_link(src)}", "", *by_note[src], ""]
    if not plan.removals:
        out.append("")
    out += ["## Ссылки в никуда", ""]
    if plan.unresolved:
        for t, n in sorted(plan.unresolved.items(), key=lambda kv: (-kv[1], kv[0].casefold())):
            out.append(f"- `{t}` — {n}")
    else:
        out.append("Нет.")
    out += ["", "## Сироты и маленькие компоненты", ""]
    if plan.orphans:
        out.append("Сироты: " + ", ".join(_link(r) for r in plan.orphans))
    for comp in plan.small_components:
        out.append("- " + " · ".join(_link(r) for r in comp))
    if not plan.orphans and not plan.small_components:
        out.append("Нет.")
    out += ["", "## Ссылка и в тексте, и в футере (только отчёт)", ""]
    if plan.duplicates:
        for src in sorted(plan.duplicates, key=lambda r: title(r).casefold()):
            out.append(f"- {_link(src)}: " + ", ".join(f"[[{t}]]" for t in plan.duplicates[src]))
    else:
        out.append("Нет.")
    return "\n".join(out) + "\n"
