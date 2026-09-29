"""The lens garden's step 1: deterministic checks over the lens graph
(anchor-lens-plan.md section 8; the L3 spec sections 5 and 7).

Pure and standard-library only: no session, no model, no clock (the
caller passes `now`), and nothing from `app`. app/core/idle/lens_garden.py
hands it app/vault/lens.py's `GardenView` (duck-typed: any object with
the same attributes will do, which is how the tests and the eval build
one by hand) and gets back `Analysis` -- the graph, and `Findings`, the
ids and scores that go to the model and into `lens_garden_run.findings`.

**No networkx** (plan section 8 left the choice open; decisions.md's L3
entry records it). The lens is under a few hundred notes, every check
below is a few lines, and each is deterministic by construction:
nodes are always visited in ascending id order, ties always go to the
smallest id or label, and nothing depends on the order the view
listed its notes or edges in -- so the same lens always gives the same
findings, and a test can pin them.

**Two graphs.**

- `G_all`, directed, over vault file ids: every lens note, and every
  resolved link with at least one lens end. A knowledge note appears
  only as its file id -- anonymous. Its title is never read here except
  by `wanted` and `recheck`, which only ever *remove* something because
  a knowledge note of that name exists; neither returns it.
- `G_lens`, undirected, over lens note ids: the lens-to-lens links.

**The checks** (spec section 5):

| check | definition |
|---|---|
| orphans | no `G_all` edge in or out; an `outside` link counts as an edge, an unresolved one does not |
| dead ends | at least one in-edge, but no resolved or `outside` out-edge |
| wanted | lens-sourced unresolved link text grouped by `norm`, ranked by number of source notes, top 10, minus existing titles and aliases |
| unlinked mentions | no edge A->B, yet B's title or alias (3+ characters) is in A's body once code and links are stripped; top 20 |
| hubs | Brandes betweenness on `G_lens`, normalised; the top 5 above 0 |
| clusters | label propagation over ascending ids, in place, ties to the smallest label, at most 20 rounds; size 2+ |
| holes | cluster pairs with no edge and no shared knowledge neighbour whose TF-IDF cosine is at least 0.15; top 3 |
| people without concepts | only when both kinds exist: person notes linking to no concept, concepts no person links to; 10 each |
| stale | unchanged for over 120 days while a lens neighbour changed within 30 |

**Mentions in Russian.** A title is matched as a whole word with up to
three extra Cyrillic letters, `(?<!\\w)stem[а-яё]{0,3}(?!\\w)`, and a
Cyrillic term's final vowel, ь or й moves into that suffix first:
«кибернетика» matches «кибернетики», while «Ашби» (stem «ашб») does not
match «ашбиевский», whose ending is longer than three letters. Crude on
purpose (the spec's risks: multi-word inflection is missed, short stems
over-match); the model is the filter.

**Dedup and recheck** (spec section 7) live here too, because they are
the same graph questions. A gap's `signature` hashes its kind and the
normalised *titles* it names, not ids: the sync pass keys lens rows by
path, so a moved note gets a new id and would otherwise re-raise a gap
the user dismissed. Its `recheck` payload is titles as well, and
`recheck` answers PASS (the graph now agrees: resolve it), GONE (a note
it names left the lens: resolve it) or FAIL.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

# --- limits (spec section 5) -------------------------------------------------

WANTED_MAX = 10
MENTIONS_MAX = 20
MENTION_MIN_CHARS = 3
# The suffix a matched stem may carry: «кибернетик» + «и».
MENTION_SUFFIX_MAX = 3
HUBS_MAX = 5
CLUSTER_ROUNDS = 20
CLUSTER_MIN = 2
HOLES_MAX = 3
HOLE_MIN_COSINE = 0.15
TERM_MIN_CHARS = 4
TERM_CUT = 6
PEOPLE_MAX = 10
STALE_DAYS = 120
FRESH_DAYS = 30

PERSON = "person"
CONCEPT = "concept"

# Recheck verdicts.
PASS = "pass"
GONE = "gone"
FAIL = "fail"

# The signature's and the recheck payload's version: a change to what
# either covers must bump it, or old live signatures stop matching.
SIGNATURE_VERSION = "v1"
RECHECK_VERSION = 1

# The gap kinds, as app/vault/lens.py's GAP_KINDS (this module may not
# import it); tests pin the two equal.
LINK = "link"
MISSING_NOTE = "missing_note"
TENSION = "tension"
BRIDGE = "bridge"
GAP_KINDS = (LINK, MISSING_NOTE, TENSION, BRIDGE)

# Words of four or more letters that say nothing about a note's topic.
# Checked before the cut to six characters.
STOPWORDS = frozenset(
    """
    этот этого этому этом этой эту эти этих этим этими
    который которая которое которые которого которой котором которых которым
    также тоже только чтобы потому поэтому после перед через между более менее
    очень когда тогда если себя себе своей свой свою своих своим свое своего
    есть были было будет будут быть может могут можно нужно надо даже просто
    всего всех всем весь вся всей всё все всегда никогда здесь там где
    него неё нему ними того тому тех теми какой какая какие каких кого чего
    однако хотя пока уже ещё еще именно например вообще почти сейчас
    that this with from have which their there these those about would could
    should into than then them they what when where while also only other
    such very more most some been being were will your https http html
    """.split()
)


class NoteLike(Protocol):
    """What this module reads of a lens note: app/vault/lens.py's
    `GardenNote`, or anything shaped like it."""

    id: int
    file_id: int
    kind: str
    title: str
    aliases: Sequence[str]
    summary: str | None
    body: str
    updated_at: datetime.datetime


class ViewLike(Protocol):
    """app/vault/lens.py's `GardenView`, or anything shaped like it."""

    notes: Sequence[NoteLike]
    edges: Sequence[tuple[int, int]]
    outside: Mapping[int, int]
    unresolved: Sequence[tuple[int, str]]
    knowledge_titles: Mapping[int, str]


# --- text --------------------------------------------------------------------

_SPACE = re.compile(r"\s+")


def norm(text: str) -> str:
    """NFKC, casefold, ё -> е, whitespace collapsed and trimmed: the one
    comparison key for titles, aliases, wanted text and signatures."""
    folded = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return _SPACE.sub(" ", folded).strip()


_FRONTMATTER = re.compile(r"\A---\n.*?\n---[ \t]*(\n|\Z)", re.DOTALL)
_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.DOTALL | re.MULTILINE)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_WIKILINK = re.compile(r"!?\[\[[^\]\n]*\]\]")
_MD_LINK = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)")
_URL = re.compile(r"\b(?:https?://|www\.)\S+")


def strip_markup(body: str) -> str:
    """The body without frontmatter, code (fenced and inline), links
    (wikilinks, embeds, markdown links) and bare URLs. A title inside a
    link is linked, not merely mentioned; one inside code is code."""
    text = _FRONTMATTER.sub("", body)
    text = _FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _WIKILINK.sub(" ", text)
    text = _MD_LINK.sub(" ", text)
    return _URL.sub(" ", text)


# After `norm`, ё is е, so these are every Cyrillic ending that moves
# into the suffix: the vowels, ь and й.
_MOVABLE_ENDINGS = frozenset("аеиоуыэюяьй")


def mention_pattern(term: str) -> re.Pattern[str] | None:
    """The pattern that finds `term` mentioned in normalised text, or
    None when the term is under `MENTION_MIN_CHARS`. See the module
    docstring for the stem rule."""
    stem = _stem(term)
    if stem is None:
        return None
    body = re.escape(stem).replace("\\ ", r"\s+")
    return re.compile(rf"(?<!\w){body}[а-яё]{{0,{MENTION_SUFFIX_MAX}}}(?!\w)")


# --- the graph ---------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Graph:
    """Both graphs, built once per run by `build_graph`.

    `succ`/`pred` are `G_all` over file ids (lens and knowledge alike);
    `lens_adj` is `G_lens` over lens note ids; `outside` and
    `unresolved` are keyed by lens note id."""

    notes: Mapping[int, NoteLike]
    file_note: Mapping[int, int]
    succ: Mapping[int, frozenset[int]]
    pred: Mapping[int, frozenset[int]]
    lens_adj: Mapping[int, frozenset[int]]
    outside: Mapping[int, int]
    unresolved: tuple[tuple[int, str], ...]

    def ids(self) -> list[int]:
        return sorted(self.notes)

    def file_of(self, note_id: int) -> int:
        return self.notes[note_id].file_id

    def lens_out(self, note_id: int) -> set[int]:
        """The lens notes this one links to."""
        return {
            self.file_note[dst]
            for dst in self.succ.get(self.file_of(note_id), ())
            if dst in self.file_note
        }

    def lens_in(self, note_id: int) -> set[int]:
        """The lens notes that link to this one."""
        return {
            self.file_note[src]
            for src in self.pred.get(self.file_of(note_id), ())
            if src in self.file_note
        }

    def knowledge_neighbours(self, note_id: int) -> int:
        """How many distinct knowledge-only notes this one links to or is
        linked from: a count, never which."""
        file_id = self.file_of(note_id)
        around = set(self.succ.get(file_id, ())) | set(self.pred.get(file_id, ()))
        return sum(1 for other in around if other not in self.file_note)


def build_graph(view: ViewLike) -> Graph:
    """`G_all` and `G_lens` from a garden view. Self-links and repeated
    edges are dropped; an edge with no lens end is ignored."""
    notes = {note.id: note for note in view.notes}
    file_note = {note.file_id: note.id for note in view.notes}
    succ: dict[int, set[int]] = collections.defaultdict(set)
    pred: dict[int, set[int]] = collections.defaultdict(set)
    lens_adj: dict[int, set[int]] = {note_id: set() for note_id in notes}
    for src, dst in view.edges:
        if src == dst or (src not in file_note and dst not in file_note):
            continue
        succ[src].add(dst)
        pred[dst].add(src)
        if src in file_note and dst in file_note:
            a, b = file_note[src], file_note[dst]
            lens_adj[a].add(b)
            lens_adj[b].add(a)
    outside = {
        file_note[file_id]: count
        for file_id, count in view.outside.items()
        if file_id in file_note and count > 0
    }
    unresolved = tuple(
        sorted(
            {(file_note[file_id], text) for file_id, text in view.unresolved if file_id in file_note}
        )
    )
    return Graph(
        notes=notes,
        file_note=file_note,
        succ={key: frozenset(value) for key, value in succ.items()},
        pred={key: frozenset(value) for key, value in pred.items()},
        lens_adj={key: frozenset(value) for key, value in lens_adj.items()},
        outside=outside,
        unresolved=unresolved,
    )


# --- the checks --------------------------------------------------------------


def orphans(graph: Graph) -> tuple[int, ...]:
    """Lens notes with no `G_all` edge in or out and no outside link."""
    return tuple(
        note_id
        for note_id in graph.ids()
        if not graph.succ.get(graph.file_of(note_id))
        and not graph.pred.get(graph.file_of(note_id))
        and not graph.outside.get(note_id)
    )


def dead_ends(graph: Graph) -> tuple[int, ...]:
    """Lens notes linked to, that link nowhere resolved or outside."""
    return tuple(
        note_id
        for note_id in graph.ids()
        if graph.pred.get(graph.file_of(note_id))
        and not graph.succ.get(graph.file_of(note_id))
        and not graph.outside.get(note_id)
    )


@dataclasses.dataclass(frozen=True)
class Wanted:
    """A note the lens links to that does not exist: the link text (the
    first spelling, in sorted order, of those that normalise alike) and
    the lens notes that link to it."""

    text: str
    sources: tuple[int, ...]


def _existing_keys(graph: Graph, knowledge_titles: Iterable[str]) -> set[str]:
    keys = {norm(term) for note in graph.notes.values() for term in (note.title, *note.aliases)}
    keys.update(norm(title) for title in knowledge_titles)
    keys.discard("")
    return keys


def wanted(graph: Graph, knowledge_titles: Iterable[str] = ()) -> tuple[Wanted, ...]:
    """Unresolved link text out of lens notes, grouped by `norm`, most
    sources first (then by key), top `WANTED_MAX`. Text that is already
    a lens title or alias, or a knowledge note's title, is left out:
    that note exists, so it is not wanted. Knowledge titles only ever
    remove an entry here; none is returned."""
    existing = _existing_keys(graph, knowledge_titles)
    spellings: dict[str, set[str]] = collections.defaultdict(set)
    sources: dict[str, set[int]] = collections.defaultdict(set)
    for note_id, text in graph.unresolved:
        key = norm(text)
        if not key or key in existing:
            continue
        spellings[key].add(_SPACE.sub(" ", text).strip())
        sources[key].add(note_id)
    ranked = sorted(sources, key=lambda key: (-len(sources[key]), key))[:WANTED_MAX]
    return tuple(
        Wanted(text=min(spellings[key]), sources=tuple(sorted(sources[key]))) for key in ranked
    )


def _stem(term: str) -> str | None:
    """The normalised term with a movable Cyrillic ending cut off, or
    None when the term is under `MENTION_MIN_CHARS`."""
    key = norm(term)
    if len(key) < MENTION_MIN_CHARS:
        return None
    return key[:-1] if key[-1] in _MOVABLE_ENDINGS else key


def mentions(graph: Graph) -> tuple[tuple[int, int], ...]:
    """(A, B) where A's body mentions B's title or an alias without a
    link A -> B, most matches first (then by ids), top `MENTIONS_MAX`."""
    texts = {note_id: norm(strip_markup(note.body)) for note_id, note in graph.notes.items()}
    terms_of: dict[int, list[tuple[str, re.Pattern[str]]]] = {}
    for note_id, note in graph.notes.items():
        found = []
        for term in dict.fromkeys((note.title, *note.aliases)):
            stem, pattern = _stem(term), mention_pattern(term)
            if stem is not None and pattern is not None:
                found.append((stem, pattern))
        terms_of[note_id] = found
    scored: list[tuple[int, int, int]] = []
    for a in graph.ids():
        text = texts[a]
        linked = graph.lens_out(a)
        for b in graph.ids():
            if a == b or b in linked:
                continue
            # The stem as a plain substring first (`norm` collapsed the
            # text's whitespace to single spaces, as it did the stem's),
            # so most pairs never reach the regex.
            count = sum(
                len(pattern.findall(text)) for stem, pattern in terms_of[b] if stem in text
            )
            if count:
                scored.append((-count, a, b))
    scored.sort()
    return tuple((a, b) for _count, a, b in scored[:MENTIONS_MAX])


def betweenness(adjacency: Mapping[int, Iterable[int]]) -> dict[int, float]:
    """Brandes' betweenness centrality on an undirected, unweighted
    graph, normalised to [0, 1] the way networkx does: each pair's
    shortest paths are counted once, over (n-1)(n-2)/2 pairs."""
    nodes = sorted(adjacency)
    neighbours = {node: sorted(set(adjacency[node]) - {node}) for node in nodes}
    score = {node: 0.0 for node in nodes}
    for source in nodes:
        stack: list[int] = []
        preds: dict[int, list[int]] = {node: [] for node in nodes}
        sigma = dict.fromkeys(nodes, 0)
        sigma[source] = 1
        dist = dict.fromkeys(nodes, -1)
        dist[source] = 0
        queue = collections.deque([source])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in neighbours[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            w = stack.pop()
            for v in preds[w]:
                delta[v] += sigma[v] / sigma[w] * (1.0 + delta[w])
            if w != source:
                score[w] += delta[w]
    n = len(nodes)
    if n < 3:
        return {node: 0.0 for node in nodes}
    # Every pair was counted from both ends (undirected), hence the 2.
    scale = 1.0 / ((n - 1) * (n - 2))
    return {node: value * scale for node, value in score.items()}


def hubs(graph: Graph) -> tuple[tuple[int, float], ...]:
    """The top `HUBS_MAX` lens notes by betweenness on `G_lens`, score
    above 0, highest first (then by id), rounded to four places."""
    scores = betweenness(graph.lens_adj)
    ranked = sorted(
        ((node, value) for node, value in scores.items() if value > 1e-12),
        key=lambda item: (-item[1], item[0]),
    )
    return tuple((node, round(value, 4)) for node, value in ranked[:HUBS_MAX])


def clusters(adjacency: Mapping[int, Iterable[int]]) -> tuple[tuple[int, ...], ...]:
    """Label propagation: every node starts with its own id as label;
    in each round, in ascending id order, a node takes the label most of
    its neighbours carry (ties to the smallest label), updated in place;
    at most `CLUSTER_ROUNDS` rounds, stopping early when nothing moves.
    Groups of `CLUSTER_MIN` or more, each sorted, largest first (then by
    smallest member). Nothing depends on input order."""
    nodes = sorted(adjacency)
    neighbours = {node: sorted(set(adjacency[node]) - {node}) for node in nodes}
    labels = {node: node for node in nodes}
    for _round in range(CLUSTER_ROUNDS):
        moved = False
        for node in nodes:
            if not neighbours[node]:
                continue
            counts = collections.Counter(labels[other] for other in neighbours[node])
            best = max(counts.values())
            label = min(value for value, count in counts.items() if count == best)
            if label != labels[node]:
                labels[node] = label
                moved = True
        if not moved:
            break
    groups: dict[int, list[int]] = collections.defaultdict(list)
    for node in nodes:
        groups[labels[node]].append(node)
    found = [tuple(sorted(members)) for members in groups.values() if len(members) >= CLUSTER_MIN]
    found.sort(key=lambda members: (-len(members), members[0]))
    return tuple(found)


def anchor(graph: Graph, members: Iterable[int]) -> int:
    """A cluster's representative: the member with the most `G_lens`
    links, then the lowest id. A bridge gap names the two anchors."""
    return min(members, key=lambda node: (-len(graph.lens_adj.get(node, ())), node))


_TOKEN = re.compile(r"\w{%d,}" % TERM_MIN_CHARS)


def terms(note: NoteLike) -> collections.Counter[str]:
    """The note's terms for TF-IDF: `\\w{4,}` tokens of its title,
    aliases, summary and body (code, links and URLs stripped), stop
    words and numbers out, each cut to `TERM_CUT` characters -- a crude
    stemmer that folds most Russian endings together."""
    text = norm(" ".join([note.title, *note.aliases, note.summary or "", strip_markup(note.body)]))
    counter: collections.Counter[str] = collections.Counter()
    for token in _TOKEN.findall(text):
        if token in STOPWORDS or token.isdigit() or "_" in token:
            continue
        counter[token[:TERM_CUT]] += 1
    return counter


def _cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    dot = sum(value * right.get(term, 0.0) for term, value in left.items())
    if dot <= 0:
        return 0.0
    norm_left = math.sqrt(sum(value * value for value in left.values()))
    norm_right = math.sqrt(sum(value * value for value in right.values()))
    return dot / (norm_left * norm_right)


@dataclasses.dataclass(frozen=True)
class Hole:
    """Two clusters (1-based indexes into `Findings.clusters`) that do not
    touch although their notes share terms, and their cosine."""

    clusters: tuple[int, int]
    score: float


def holes(graph: Graph, groups: Sequence[Sequence[int]]) -> tuple[Hole, ...]:
    """Pairs of clusters not already joined, whose TF-IDF cosine is at
    least `HOLE_MIN_COSINE`; the top `HOLES_MAX`. "Joined" is exactly the
    bridge recheck's test (`_joined`): an undirected path of length 2 or
    less in `G_all`, through a knowledge note or through a third lens
    note of another cluster (or of none). With a narrower test (a direct
    edge or a shared knowledge note) a pair joined through such a third
    note would be sent to the model as a hole, and every bridge it
    proposed there dropped by `validate()`. IDF is over lens notes,
    `ln((1+N)/(1+df))`, so a term every note has weighs nothing; a
    cluster's vector is the sum of its members' weighted counts."""
    if len(groups) < 2:
        return ()
    counts = {note_id: terms(note) for note_id, note in graph.notes.items()}
    total = len(counts)
    df: collections.Counter[str] = collections.Counter()
    for counter in counts.values():
        df.update(set(counter))
    idf = {term: math.log((1 + total) / (1 + seen)) for term, seen in df.items()}
    vectors = []
    for members in groups:
        vector: dict[str, float] = collections.defaultdict(float)
        for note_id in members:
            for term, count in counts[note_id].items():
                vector[term] += count * idf[term]
        vectors.append(vector)

    files = [{graph.file_of(note_id) for note_id in members} for members in groups]
    found: list[Hole] = []
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if _joined(graph, files[i], files[j]):
                continue
            score = _cosine(vectors[i], vectors[j])
            if score >= HOLE_MIN_COSINE:
                found.append(Hole(clusters=(i + 1, j + 1), score=round(score, 4)))
    found.sort(key=lambda hole: (-hole.score, hole.clusters))
    return tuple(found[:HOLES_MAX])


def people_without_concepts(graph: Graph) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """(person notes linking to no concept note, concept notes no person
    note links to), at most `PEOPLE_MAX` each, by id. Both empty unless
    the lens has both kinds (`lens_person_folders` in use)."""
    people = [note_id for note_id in graph.ids() if graph.notes[note_id].kind == PERSON]
    concepts = [note_id for note_id in graph.ids() if graph.notes[note_id].kind == CONCEPT]
    if not people or not concepts:
        return (), ()
    concept_set = set(concepts)
    lonely_people = [p for p in people if not graph.lens_out(p) & concept_set]
    reached = {c for p in people for c in graph.lens_out(p) if c in concept_set}
    lonely_concepts = [c for c in concepts if c not in reached]
    return tuple(lonely_people[:PEOPLE_MAX]), tuple(lonely_concepts[:PEOPLE_MAX])


def stale(graph: Graph, now: datetime.datetime) -> tuple[int, ...]:
    """Lens notes unchanged for over `STALE_DAYS` while a `G_lens`
    neighbour changed within `FRESH_DAYS`. `updated_at` is when the
    sync pass saw a change, so a move counts as one."""
    old = datetime.timedelta(days=STALE_DAYS)
    fresh = datetime.timedelta(days=FRESH_DAYS)
    return tuple(
        note_id
        for note_id in graph.ids()
        if now - graph.notes[note_id].updated_at > old
        and any(
            now - graph.notes[other].updated_at <= fresh
            for other in graph.lens_adj.get(note_id, ())
        )
    )


# --- all of step 1 -----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Findings:
    """Step 1's answer, ids and scores only (plus `wanted`'s link text,
    which is lens-sourced). `clusters` is ordered, and `Hole.clusters`
    and a bridge's cluster ids are 1-based indexes into it."""

    orphans: tuple[int, ...]
    dead_ends: tuple[int, ...]
    wanted: tuple[Wanted, ...]
    mentions: tuple[tuple[int, int], ...]
    hubs: tuple[tuple[int, float], ...]
    clusters: tuple[tuple[int, ...], ...]
    holes: tuple[Hole, ...]
    people_without_concepts: tuple[int, ...]
    concepts_without_people: tuple[int, ...]
    stale: tuple[int, ...]
    # Each cluster's anchor (`anchor`), in `clusters` order.
    anchors: tuple[int, ...] = ()

    def to_json(self, cluster_names: Mapping[int, str] | None = None) -> dict:
        """The `lens_garden_run.findings` document (and the report's
        «Структура»): ids, scores, the clusters with the names the model
        gave them (None when it gave none), and wanted link text.

        `{"v": 1, "orphans": [id], "dead_ends": [id],
        "wanted": [{"text", "sources": [id]}], "mentions": [[a, b]],
        "hubs": [{"id", "score"}], "clusters": [{"id", "members": [id],
        "anchor": id, "name"}], "holes": [{"clusters": [i, j], "score"}],
        "people_without_concepts": [id], "concepts_without_people": [id],
        "stale": [id]}` -- every id a `lens_note` id as of the run."""
        names = cluster_names or {}
        return {
            "v": RECHECK_VERSION,
            "orphans": list(self.orphans),
            "dead_ends": list(self.dead_ends),
            "wanted": [{"text": item.text, "sources": list(item.sources)} for item in self.wanted],
            "mentions": [list(pair) for pair in self.mentions],
            "hubs": [{"id": node, "score": score} for node, score in self.hubs],
            "clusters": [
                {
                    "id": index,
                    "members": list(members),
                    "anchor": self.anchors[index - 1] if self.anchors else members[0],
                    "name": names.get(index),
                }
                for index, members in enumerate(self.clusters, start=1)
            ],
            "holes": [{"clusters": list(hole.clusters), "score": hole.score} for hole in self.holes],
            "people_without_concepts": list(self.people_without_concepts),
            "concepts_without_people": list(self.concepts_without_people),
            "stale": list(self.stale),
        }


@dataclasses.dataclass(frozen=True)
class Analysis:
    """The graph and its findings, for step 2 and the recheck."""

    graph: Graph
    findings: Findings
    knowledge_keys: frozenset[str]

    def cluster_anchor(self, index: int) -> int:
        """The anchor note of cluster `index` (1-based)."""
        return self.findings.anchors[index - 1]


def analyse(view: ViewLike, now: datetime.datetime) -> Analysis:
    """Every check over one garden view. `knowledge_keys` keeps the
    knowledge notes' normalised titles for local checks (a missing note
    that already exists, a recheck) and never leaves this process."""
    graph = build_graph(view)
    knowledge_titles = list(view.knowledge_titles.values())
    groups = clusters(graph.lens_adj)
    people, concepts = people_without_concepts(graph)
    findings = Findings(
        orphans=orphans(graph),
        dead_ends=dead_ends(graph),
        wanted=wanted(graph, knowledge_titles),
        mentions=mentions(graph),
        hubs=hubs(graph),
        clusters=groups,
        holes=holes(graph, groups),
        people_without_concepts=people,
        concepts_without_people=concepts,
        stale=stale(graph, now),
        anchors=tuple(anchor(graph, members) for members in groups),
    )
    keys = frozenset(key for key in (norm(title) for title in knowledge_titles) if key)
    return Analysis(graph=graph, findings=findings, knowledge_keys=keys)


# --- dedup and recheck (spec section 7) --------------------------------------


def signature(kind: str, titles: Iterable[str]) -> str:
    """`sha256("v1|<kind>|<t1>|<t2>...")` over the sorted normalised
    titles: the note pair (link, tension), the proposed title
    (missing_note) or the two anchors (bridge)."""
    if kind not in GAP_KINDS:
        raise ValueError(f"unknown gap kind: {kind!r}")
    parts = (SIGNATURE_VERSION, kind, *sorted(norm(title) for title in titles))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def recheck_payload(kind: str, *, titles: Sequence[str] = (), title: str | None = None,
                    clusters: Sequence[Sequence[str]] = ()) -> dict:
    """What the next run tests, by title: the pair (link, tension), the
    proposed title and the notes that raised it (missing_note), or both
    clusters' member titles (bridge)."""
    if kind in (LINK, TENSION):
        return {"v": RECHECK_VERSION, "titles": list(titles)}
    if kind == MISSING_NOTE:
        return {"v": RECHECK_VERSION, "title": title, "sources": list(titles)}
    if kind == BRIDGE:
        return {"v": RECHECK_VERSION, "clusters": [list(group) for group in clusters]}
    raise ValueError(f"unknown gap kind: {kind!r}")


def _title_index(graph: Graph) -> dict[str, list[int]]:
    index: dict[str, list[int]] = collections.defaultdict(list)
    for note_id in graph.ids():
        index[norm(graph.notes[note_id].title)].append(note_id)
    return index


def _files(graph: Graph, index: Mapping[str, list[int]], titles: Iterable) -> set[int]:
    return {
        graph.file_of(note_id)
        for title in titles
        if isinstance(title, str)
        for note_id in index.get(norm(title), ())
    }


def _around(graph: Graph, file_id: int) -> set[int]:
    return set(graph.succ.get(file_id, ())) | set(graph.pred.get(file_id, ()))


def _joined(graph: Graph, left: set[int], right: set[int]) -> bool:
    """Whether an undirected path of length 2 or less in `G_all` joins
    two sets of files (a shared file counts). The bridge recheck, and
    `holes()`'s test for a pair that is no hole."""
    if left & right:
        return True
    right_around = set().union(*(_around(graph, b) for b in right))
    for a in left:
        near = _around(graph, a)
        if near & right or near & right_around:
            return True
    return False


def recheck(kind: str, payload: Mapping, analysis: Analysis) -> str:
    """PASS, GONE or FAIL for one gap against the graph now (spec
    section 7), by title:

    - link: an edge either way between the two notes;
    - missing_note: a lens title or alias, or a knowledge title, now
      matches the proposed title (GONE when every note that raised it
      has left the lens);
    - tension: some third note links to both;
    - bridge: an undirected path of length 2 or less in `G_all` joins a
      note of one cluster to a note of the other (a knowledge note in
      the middle counts, which is how L4's inbox note closes it).

    A pair or cluster whose note left the lens is GONE. A payload this
    version cannot read is GONE too: resolving it is safer than
    re-raising it forever."""
    graph = analysis.graph
    index = _title_index(graph)
    if not isinstance(payload, Mapping) or payload.get("v") != RECHECK_VERSION:
        return GONE
    if kind in (LINK, TENSION):
        titles = payload.get("titles")
        if not isinstance(titles, list) or len(titles) != 2:
            return GONE
        left, right = _files(graph, index, titles[:1]), _files(graph, index, titles[1:])
        if not left or not right:
            return GONE
        if kind == LINK:
            joined = any(b in graph.succ.get(a, ()) or a in graph.succ.get(b, ())
                         for a in left for b in right)
            return PASS if joined else FAIL
        for a in left:
            for b in right:
                third = (set(graph.pred.get(a, ())) & set(graph.pred.get(b, ()))) - {a, b}
                if third:
                    return PASS
        return FAIL
    if kind == MISSING_NOTE:
        title = payload.get("title")
        if not isinstance(title, str) or not norm(title):
            return GONE
        existing = _existing_keys(graph, ()) | set(analysis.knowledge_keys)
        if norm(title) in existing:
            return PASS
        sources = payload.get("sources")
        if isinstance(sources, list) and sources and not _files(graph, index, sources):
            return GONE
        return FAIL
    if kind == BRIDGE:
        groups = payload.get("clusters")
        if not isinstance(groups, list) or len(groups) != 2:
            return GONE
        if not all(isinstance(group, list) for group in groups):
            return GONE
        left, right = _files(graph, index, groups[0]), _files(graph, index, groups[1])
        if not left or not right:
            return GONE
        return PASS if _joined(graph, left, right) else FAIL
    return GONE


__all__ = [
    "BRIDGE",
    "CLUSTER_MIN",
    "CLUSTER_ROUNDS",
    "CONCEPT",
    "FAIL",
    "GAP_KINDS",
    "GONE",
    "HOLES_MAX",
    "HOLE_MIN_COSINE",
    "HUBS_MAX",
    "LINK",
    "MENTIONS_MAX",
    "MISSING_NOTE",
    "PASS",
    "PEOPLE_MAX",
    "PERSON",
    "SIGNATURE_VERSION",
    "TENSION",
    "WANTED_MAX",
    "Analysis",
    "Findings",
    "Graph",
    "Hole",
    "Wanted",
    "analyse",
    "anchor",
    "betweenness",
    "build_graph",
    "clusters",
    "dead_ends",
    "holes",
    "hubs",
    "mention_pattern",
    "mentions",
    "norm",
    "orphans",
    "people_without_concepts",
    "recheck",
    "recheck_payload",
    "signature",
    "stale",
    "strip_markup",
    "terms",
    "wanted",
]
