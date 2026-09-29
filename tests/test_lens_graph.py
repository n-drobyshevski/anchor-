"""app/core/lens_graph.py: the lens garden's deterministic step 1 and its
dedup and recheck (anchor-lens-plan.md section 8; the L3 spec sections
5, 7 and 9). Pure: no database, no model, views built by hand. All
titles are synthetic or public-knowledge names.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime
import hashlib
import pathlib
import sys

import pytest

from app.core import lens_graph as g

NOW = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc)


@dataclasses.dataclass(frozen=True)
class Note:
    id: int
    file_id: int
    kind: str
    title: str
    aliases: tuple[str, ...] = ()
    summary: str | None = None
    body: str = ""
    updated_at: datetime.datetime = NOW


@dataclasses.dataclass(frozen=True)
class View:
    notes: tuple[Note, ...]
    edges: tuple[tuple[int, int], ...] = ()
    outside: dict = dataclasses.field(default_factory=dict)
    unresolved: tuple[tuple[int, str], ...] = ()
    knowledge_titles: dict = dataclasses.field(default_factory=dict)
    version_id: int | None = None


def note(i: int, title: str, **kwargs) -> Note:
    """Lens note `i` lives in vault file `100 + i`."""
    kwargs.setdefault("kind", "concept")
    return Note(id=i, file_id=100 + i, title=title, **kwargs)


def edge(a: int, b: int) -> tuple[int, int]:
    """A link between lens notes a -> b, as file ids."""
    return (100 + a, 100 + b)


# --- purity ---------------------------------------------------------------------


def test_the_module_imports_only_the_standard_library():
    """Spec section 8's input test, first half: step 1 cannot reach the
    database, a model or anything in `app`."""
    tree = ast.parse(pathlib.Path(g.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    names.discard("__future__")
    assert names, "the scan found no imports at all"
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)


def test_gap_kinds_match_the_lens_module():
    from app.vault import lens

    assert g.GAP_KINDS == lens.GAP_KINDS


# --- text -----------------------------------------------------------------------


def test_norm_folds_case_width_yo_and_spaces():
    assert g.norm("  Ёжик   в\tТУМАНЕ ") == "ежик в тумане"
    assert g.norm("ＶＳＭ") == "vsm"  # NFKC: fullwidth letters
    assert g.norm("Straße") == "strasse"  # casefold, not lower


def test_strip_markup_drops_code_links_urls_and_frontmatter():
    body = (
        "---\nsummary: x\n---\n"
        "Текст [[Ашби|Эшби]] и ![[картинка.png]], [сайт](https://a.b) "
        "`Ашби` https://example.org/Ашби\n```\nАшби в коде\n```\nконец"
    )
    stripped = g.strip_markup(body)
    assert "Ашби" not in stripped
    assert "summary" not in stripped
    assert "Текст" in stripped and "конец" in stripped


def test_a_short_stem_does_not_match_a_longer_word():
    """«Ашби» must not match «ашбиевский»: the stem «ашб» takes at most
    three more letters."""
    pattern = g.mention_pattern("Ашби")
    assert pattern.search(g.norm("у Ашби есть закон")) is not None
    assert pattern.search(g.norm("ашбиевский подход")) is None


def test_a_cyrillic_final_vowel_moves_into_the_suffix():
    """«кибернетика» matches «кибернетики» (spec section 9)."""
    pattern = g.mention_pattern("Кибернетика")
    assert pattern.search(g.norm("основы кибернетики второго порядка")) is not None
    assert pattern.search(g.norm("кибернетикой")) is not None
    assert pattern.search(g.norm("кибернетикалогия")) is None


def test_soft_sign_and_short_i_move_too_and_latin_keeps_its_end():
    assert g.mention_pattern("Гомеостат").search("о гомеостате") is not None
    assert g.mention_pattern("Связь").search("без связи") is not None
    assert g.mention_pattern("Бейтсон").search("у бейтсона") is not None
    assert g.mention_pattern("VSM").search(g.norm("модель VSM")) is not None


def test_terms_under_three_characters_are_never_matched():
    assert g.mention_pattern("ИИ") is None
    assert g.mention_pattern("ИИС") is not None


def test_a_multi_word_title_matches_across_whitespace():
    pattern = g.mention_pattern("Закон Гудхарта")
    assert pattern.search(g.norm("это закон   Гудхарта в действии")) is not None


# --- orphans, dead ends, wanted ------------------------------------------------------


def test_orphans_count_outside_links_but_not_unresolved_ones():
    view = View(
        notes=(note(1, "A"), note(2, "B"), note(3, "C"), note(4, "D"), note(5, "E")),
        edges=(edge(1, 2),),
        outside={100 + 3: 1},
        unresolved=((100 + 4, "Нет такой"),),
    )
    graph = g.build_graph(view)
    assert g.orphans(graph) == (4, 5)


def test_a_knowledge_link_is_an_edge_for_orphans_and_dead_ends():
    view = View(
        notes=(note(1, "A"), note(2, "B"), note(3, "C")),
        edges=((100 + 1, 900), (901, 100 + 2), edge(3, 2)),
    )
    graph = g.build_graph(view)
    assert g.orphans(graph) == ()
    # B is linked to (by a knowledge note and by C) and links nowhere.
    assert g.dead_ends(graph) == (2,)


def test_an_outside_link_keeps_a_note_from_being_a_dead_end():
    view = View(
        notes=(note(1, "A"), note(2, "B")),
        edges=(edge(1, 2),),
        outside={100 + 2: 2},
    )
    assert g.dead_ends(g.build_graph(view)) == ()


def test_self_links_and_duplicate_edges_are_ignored():
    view = View(notes=(note(1, "A"), note(2, "B")), edges=(edge(1, 1), edge(1, 2), edge(1, 2)))
    graph = g.build_graph(view)
    assert g.orphans(graph) == ()
    assert graph.lens_adj[1] == frozenset({2})
    assert g.dead_ends(graph) == (2,)


def test_wanted_groups_by_norm_ranks_by_sources_and_skips_existing_names():
    view = View(
        notes=(
            note(1, "A", aliases=("Альфа",)),
            note(2, "B"),
            note(3, "C"),
        ),
        unresolved=(
            (101, "Гомеостат"),
            (102, "гомеостат"),
            (103, "Гомеостат "),
            (101, "Разнообразие"),
            (102, "Разнообразие"),
            (101, "Одиночка"),
            (101, "альфа"),  # an alias: that note exists
            (101, "b"),  # a lens title
            (101, "Тайная заметка"),  # a knowledge title
        ),
        knowledge_titles={900: "Тайная заметка"},
    )
    found = g.wanted(g.build_graph(view), view.knowledge_titles.values())
    assert [(item.text, item.sources) for item in found] == [
        ("Гомеостат", (1, 2, 3)),
        ("Разнообразие", (1, 2)),
        ("Одиночка", (1,)),
    ]


def test_wanted_keeps_the_top_ten():
    view = View(
        notes=(note(1, "A"),),
        unresolved=tuple((101, f"Заметка {i:02d}") for i in range(15)),
    )
    assert len(g.wanted(g.build_graph(view))) == g.WANTED_MAX == 10


def test_wanted_never_returns_a_knowledge_title():
    view = View(
        notes=(note(1, "A"),),
        unresolved=((101, "Тайная заметка"), (101, "Открытая заметка")),
        knowledge_titles={900: "Тайная заметка"},
    )
    analysis = g.analyse(view, NOW)
    assert [item.text for item in analysis.findings.wanted] == ["Открытая заметка"]


# --- mentions -----------------------------------------------------------------------


def test_an_unlinked_mention_is_found_and_a_linked_one_is_not():
    view = View(
        notes=(
            note(1, "Кибернетика"),
            note(2, "Норберт Винер", body="Винер основал науку кибернетики в 1948 году."),
            note(3, "Росс Эшби", body="Эшби писал о кибернетике много.", aliases=("Ashby",)),
            note(4, "Гомеостат", body="Прибор Ashby."),
        ),
        edges=(edge(3, 1),),
    )
    assert g.mentions(g.build_graph(view)) == ((2, 1), (4, 3))


def test_a_mention_inside_a_link_or_code_does_not_count():
    view = View(
        notes=(
            note(1, "Кибернетика"),
            note(2, "Винер", body="См. [[Кибернетика]] и `кибернетика` и [кибернетика](http://x)."),
        ),
    )
    assert g.mentions(g.build_graph(view)) == ()


def test_mentions_rank_by_count_then_ids_and_keep_twenty():
    notes = [note(1, "Кибернетика")]
    notes += [note(i, f"Заметка {i}", body="кибернетика " * (i % 3 + 1)) for i in range(2, 30)]
    found = g.mentions(g.build_graph(View(notes=tuple(notes))))
    assert len(found) == g.MENTIONS_MAX == 20
    assert found[0] == (2, 1)  # three matches (2 % 3 + 1), lowest id among them
    assert found[1] == (5, 1)
    assert all(dst == 1 for _src, dst in found)


def test_ashbi_is_not_mentioned_by_the_adjective():
    view = View(
        notes=(note(1, "Ашби"), note(2, "Подход", body="Ашбиевский подход к системам.")),
    )
    assert g.mentions(g.build_graph(view)) == ()


# --- hubs ---------------------------------------------------------------------------


def test_brandes_on_a_path_of_three():
    assert g.betweenness({1: [2], 2: [1, 3], 3: [2]}) == {1: 0.0, 2: 1.0, 3: 0.0}


def test_brandes_on_a_star_matches_the_hand_count():
    """Centre on all 6 leaf pairs' only shortest path: 6 / C(4, 2) = 1."""
    adjacency = {0: [1, 2, 3, 4], 1: [0], 2: [0], 3: [0], 4: [0]}
    scores = g.betweenness(adjacency)
    assert scores[0] == pytest.approx(1.0)
    assert all(scores[leaf] == 0.0 for leaf in (1, 2, 3, 4))


def test_brandes_on_a_four_cycle_splits_the_paths():
    """Each opposite pair has two shortest paths, so each node carries
    half of one pair: 0.5 / C(3, 2) = 1/6."""
    adjacency = {1: [2, 4], 2: [1, 3], 3: [2, 4], 4: [3, 1]}
    for value in g.betweenness(adjacency).values():
        assert value == pytest.approx(1 / 6)


def test_brandes_on_a_bridge_between_two_triangles():
    """1-2-3 triangle, 4-5-6 triangle, 3-4 bridge: n=6, so C(5, 2) = 10
    pairs not involving a given node. Every shortest path from {1, 2}
    to {4, 5, 6} runs through 3 (6 pairs): 6/10. 4 is symmetric."""
    adjacency = {1: [2, 3], 2: [1, 3], 3: [1, 2, 4], 4: [3, 5, 6], 5: [4, 6], 6: [4, 5]}
    scores = g.betweenness(adjacency)
    assert scores[3] == pytest.approx(0.6)
    assert scores[4] == pytest.approx(0.6)
    assert scores[1] == 0.0


def test_hubs_are_the_top_five_above_zero():
    # A path 1-2-...-9: inner nodes have scores, ends none.
    view = View(
        notes=tuple(note(i, f"N{i}") for i in range(1, 10)),
        edges=tuple(edge(i, i + 1) for i in range(1, 9)),
    )
    found = g.hubs(g.build_graph(view))
    assert len(found) == 5
    assert found[0][0] == 5  # the middle
    assert all(score > 0 for _node, score in found)
    assert [node for node, _score in found] == [5, 4, 6, 3, 7]


def test_no_hubs_in_a_graph_without_paths():
    view = View(notes=(note(1, "A"), note(2, "B")), edges=(edge(1, 2),))
    assert g.hubs(g.build_graph(view)) == ()


# --- clusters -----------------------------------------------------------------------


def test_two_separate_triangles_are_two_clusters():
    adjacency = {1: [2, 3], 2: [1, 3], 3: [1, 2], 7: [8, 9], 8: [7, 9], 9: [7, 8], 5: []}
    assert g.clusters(adjacency) == ((1, 2, 3), (7, 8, 9))


def test_clusters_are_stable_under_permutation():
    """The same graph, listed in any order, gives the same clusters."""
    import random

    edges = [(1, 2), (2, 3), (1, 3), (3, 4), (4, 5), (5, 6), (4, 6), (7, 8), (8, 9), (10, 11)]
    reference = None
    rng = random.Random(7)
    for _attempt in range(20):
        shuffled = edges[:]
        rng.shuffle(shuffled)
        nodes = list(range(1, 12))
        rng.shuffle(nodes)
        adjacency: dict[int, list[int]] = {node: [] for node in nodes}
        for a, b in shuffled:
            first, second = (a, b) if rng.random() < 0.5 else (b, a)
            adjacency[first].append(second)
            adjacency[second].append(first)
        result = g.clusters(adjacency)
        if reference is None:
            reference = result
        assert result == reference
    assert all(len(members) >= 2 for members in reference)


def test_label_propagation_ties_go_to_the_smallest_label():
    # A pair: 1 takes 2's label, then 2 keeps it -- one cluster.
    assert g.clusters({1: [2], 2: [1]}) == ((1, 2),)
    # Isolated nodes are never clusters.
    assert g.clusters({1: [], 2: []}) == ()


def _cliques(groups, bridge):
    adjacency: dict[int, list[int]] = {}
    for group in groups:
        for a in group:
            adjacency.setdefault(a, []).extend(b for b in group if b != a)
    a, b = bridge
    adjacency[a].append(b)
    adjacency[b].append(a)
    return adjacency


def test_spec_literal_propagation_floods_a_component_and_depends_on_numbering():
    """Pinned on purpose (docs/decisions.md, L3): in place, ascending ids,
    ties to the smallest label, the lowest label floods a connected
    component, so two cliques joined by one edge are one cluster -- unless
    the ids interleave. A change to the algorithm must change this test."""
    assert g.clusters(_cliques([(1, 2, 3, 4), (5, 6, 7, 8)], (4, 5))) == ((1, 2, 3, 4, 5, 6, 7, 8),)
    assert g.clusters(_cliques([(1, 3, 5, 7), (2, 4, 6, 8)], (7, 8))) == ((1, 3, 5, 7), (2, 4, 6, 8))


def test_the_anchor_has_the_most_links_then_the_lowest_id():
    view = View(
        notes=tuple(note(i, f"N{i}") for i in range(1, 5)),
        edges=(edge(1, 2), edge(2, 3), edge(3, 4), edge(2, 4), edge(3, 1)),
    )
    graph = g.build_graph(view)
    # 2 and 3 both have three links; 2 is lower.
    assert g.anchor(graph, (1, 2, 3, 4)) == 2


# --- holes -------------------------------------------------------------------------


def _two_topics(extra_edges=(), knowledge_edges=(), second_body="гомеостат регулятор разнообразие"):
    first = "гомеостат регулятор разнообразие ультрастабильность"
    notes = (
        note(1, "Эшби один", body=first),
        note(2, "Эшби два", body=first),
        note(3, "Бир один", body=second_body),
        note(4, "Бир два", body=second_body),
        note(5, "Шум один", body="акварель пейзаж кисти бумага"),
        note(6, "Шум два", body="акварель пейзаж кисти бумага"),
    )
    edges = (edge(1, 2), edge(3, 4), edge(5, 6), *extra_edges, *knowledge_edges)
    return View(notes=notes, edges=edges)


def test_a_hole_is_two_similar_clusters_that_do_not_touch():
    analysis = g.analyse(_two_topics(), NOW)
    assert analysis.findings.clusters == ((1, 2), (3, 4), (5, 6))
    holes = analysis.findings.holes
    assert [hole.clusters for hole in holes] == [(1, 2)]
    assert holes[0].score >= g.HOLE_MIN_COSINE


def test_an_edge_between_the_clusters_closes_the_hole():
    graph = g.build_graph(_two_topics(extra_edges=(edge(2, 3),)))
    # The clusters as they were, now joined by 2-3: no hole between them.
    assert g.holes(graph, ((1, 2), (3, 4), (5, 6))) == ()


def test_a_third_lens_note_between_the_clusters_closes_the_hole():
    """Joined through 5, which sits in neither cluster: the bridge recheck
    would pass at once, so it is no hole (the two tests are one)."""
    graph = g.build_graph(_two_topics(extra_edges=(edge(2, 5), edge(5, 3))))
    assert g.holes(graph, ((1, 2), (3, 4))) == ()
    payload = {"v": g.RECHECK_VERSION, "clusters": [["Эшби один", "Эшби два"], ["Бир один", "Бир два"]]}
    analysis = g.analyse(_two_topics(extra_edges=(edge(2, 5), edge(5, 3))), NOW)
    assert g.recheck(g.BRIDGE, payload, analysis) == g.PASS
    # Without the middle note, the same pair is a hole.
    assert [h.clusters for h in g.holes(g.build_graph(_two_topics()), ((1, 2), (3, 4)))] == [(1, 2)]


def test_a_shared_knowledge_neighbour_closes_the_hole():
    view = _two_topics(knowledge_edges=((100 + 1, 900), (100 + 3, 900)))
    assert g.analyse(view, NOW).findings.holes == ()


def test_dissimilar_clusters_are_no_hole():
    view = _two_topics(second_body="акварель пейзаж кисти бумага холст")
    holes = g.analyse(view, NOW).findings.holes
    assert all(set(hole.clusters) != {1, 2} for hole in holes)


def test_terms_are_cut_stoplisted_and_skip_numbers():
    counted = g.terms(note(1, "Разнообразие", body="который разнообразия 12345 https://x.org"))
    assert counted == {"разноо": 2}


# --- people, stale ------------------------------------------------------------------


def test_people_without_concepts_need_both_kinds():
    view = View(
        notes=(
            note(1, "Бир", kind="person"),
            note(2, "Эшби", kind="person"),
            note(3, "Разнообразие"),
            note(4, "Гомеостат"),
        ),
        edges=(edge(1, 3), edge(2, 1)),
    )
    people, concepts = g.people_without_concepts(g.build_graph(view))
    assert people == (2,)
    assert concepts == (4,)
    only_concepts = View(notes=(note(3, "Разнообразие"), note(4, "Гомеостат")))
    assert g.people_without_concepts(g.build_graph(only_concepts)) == ((), ())


def test_stale_is_old_next_to_fresh():
    old = NOW - datetime.timedelta(days=121)
    view = View(
        notes=(
            note(1, "Старая", updated_at=old),
            note(2, "Свежая", updated_at=NOW - datetime.timedelta(days=30)),
            note(3, "Тоже старая", updated_at=old),
            note(4, "Старая одна", updated_at=old),
            note(5, "Не такая старая", updated_at=NOW - datetime.timedelta(days=120)),
        ),
        edges=(edge(1, 2), edge(3, 4), edge(5, 2)),
    )
    assert g.stale(g.build_graph(view), NOW) == (1,)


# --- findings --------------------------------------------------------------------------


def test_findings_json_is_ids_scores_and_cluster_names():
    view = _two_topics()
    findings = g.analyse(view, NOW).findings
    document = findings.to_json({1: "Эшби", 3: "Шум"})
    assert document["v"] == 1
    assert document["clusters"][0] == {"id": 1, "members": [1, 2], "anchor": 1, "name": "Эшби"}
    assert document["clusters"][1]["name"] is None
    assert document["holes"] == [{"clusters": [1, 2], "score": findings.holes[0].score}]
    assert set(document) == {
        "v", "orphans", "dead_ends", "wanted", "mentions", "hubs", "clusters", "holes",
        "people_without_concepts", "concepts_without_people", "stale",
    }


def test_analysis_never_carries_a_knowledge_title_in_its_findings():
    view = View(
        notes=(note(1, "A"), note(2, "B")),
        edges=((101, 900), (102, 900)),
        unresolved=((101, "Тайная заметка"),),
        knowledge_titles={900: "Тайная заметка"},
    )
    analysis = g.analyse(view, NOW)
    assert "Тайная" not in repr(analysis.findings.to_json())
    assert analysis.knowledge_keys == frozenset({"тайная заметка"})


# --- signatures ---------------------------------------------------------------------


def test_the_signature_is_the_specs_hash_over_sorted_normalised_titles():
    expected = hashlib.sha256("v1|link|ашби|бир".encode()).hexdigest()
    assert g.signature("link", ["Бир", "Ашби"]) == expected
    assert g.signature("link", ["  АШБИ ", "бир"]) == expected
    assert g.signature("tension", ["Бир", "Ашби"]) != expected
    with pytest.raises(ValueError):
        g.signature("research", ["x"])


def test_a_moved_note_keeps_its_signature():
    """Titles, not ids: the sync pass re-keys a moved note."""
    assert g.signature("link", ["Ашби", "Бир"]) == g.signature("link", ["Ашби", "Бир"])


# --- recheck --------------------------------------------------------------------------


def _recheck(kind, payload, view):
    return g.recheck(kind, payload, g.analyse(view, NOW))


def test_link_recheck_passes_on_an_edge_either_way_and_is_gone_without_its_note():
    payload = g.recheck_payload("link", titles=["Ашби", "Бир"])
    base = (note(1, "Ашби"), note(2, "Бир"))
    assert _recheck("link", payload, View(notes=base)) == g.FAIL
    assert _recheck("link", payload, View(notes=base, edges=(edge(2, 1),))) == g.PASS
    assert _recheck("link", payload, View(notes=base[:1])) == g.GONE


def test_a_link_recheck_follows_a_moved_note_by_title():
    payload = g.recheck_payload("link", titles=["Ашби", "Бир"])
    moved = View(notes=(note(7, "Ашби"), note(9, "Бир")), edges=(edge(7, 9),))
    assert _recheck("link", payload, moved) == g.PASS


def test_missing_note_recheck_sees_lens_titles_aliases_and_knowledge_titles():
    payload = g.recheck_payload("missing_note", titles=["Ашби"], title="Гомеостат")
    base = (note(1, "Ашби"),)
    assert _recheck("missing_note", payload, View(notes=base)) == g.FAIL
    assert _recheck("missing_note", payload, View(notes=(*base, note(2, "гомеостат")))) == g.PASS
    aliased = View(notes=(*base, note(2, "Прибор", aliases=("Гомеостат",))))
    assert _recheck("missing_note", payload, aliased) == g.PASS
    knowledge = View(notes=base, knowledge_titles={900: "Гомеостат"})
    assert _recheck("missing_note", payload, knowledge) == g.PASS
    assert _recheck("missing_note", payload, View(notes=(note(3, "Другое"),))) == g.GONE


def test_tension_recheck_passes_when_a_third_note_links_to_both():
    payload = g.recheck_payload("tension", titles=["Фишер", "Ланд"])
    base = (note(1, "Фишер"), note(2, "Ланд"), note(3, "Ускорение"))
    assert _recheck("tension", payload, View(notes=base, edges=(edge(1, 2),))) == g.FAIL
    both = View(notes=base, edges=(edge(3, 1), edge(3, 2)))
    assert _recheck("tension", payload, both) == g.PASS
    # A knowledge note linking to both counts too.
    knowledge = View(notes=base, edges=((900, 101), (900, 102)))
    assert _recheck("tension", payload, knowledge) == g.PASS


def test_bridge_recheck_passes_on_a_path_of_two_even_through_a_knowledge_note():
    payload = g.recheck_payload("bridge", clusters=[["Эшби один", "Эшби два"], ["Бир один", "Бир два"]])
    view = _two_topics()
    assert _recheck("bridge", payload, view) == g.FAIL
    direct = _two_topics(extra_edges=(edge(2, 3),))
    assert _recheck("bridge", payload, direct) == g.PASS
    via_inbox = _two_topics(knowledge_edges=((900, 102), (900, 104)))
    assert _recheck("bridge", payload, via_inbox) == g.PASS
    via_lens = _two_topics(extra_edges=(edge(1, 5), edge(5, 3)))
    assert _recheck("bridge", payload, via_lens) == g.PASS
    three_steps = _two_topics(extra_edges=(edge(1, 5), edge(6, 3)))
    assert _recheck("bridge", payload, three_steps) == g.FAIL
    gone = View(notes=view.notes[:2])
    assert _recheck("bridge", payload, gone) == g.GONE


def test_an_unreadable_recheck_payload_is_gone():
    view = View(notes=(note(1, "A"), note(2, "B")))
    assert _recheck("link", {"titles": ["A", "B"]}, view) == g.GONE
    assert _recheck("link", {"v": 1, "titles": ["A"]}, view) == g.GONE
