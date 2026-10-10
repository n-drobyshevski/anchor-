"""Footer link pruning (prune.py): the rule, the footer edits, the routes.

A small fixture vault per test. Names are invented; nothing here is
read from a real vault.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.conftest import AUTH, write
from vaultd import prune

SETTINGS = "---\nanchor: settings\nknowledge_folders: [Library]\nlens_folders: [Lens]\n---\n"
SEP = " · "


@pytest.fixture(autouse=True)
def _settings(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)


def note(body: str, footer: str | None = None, fm: str = "tags: [x]\n") -> str:
    text = f"---\n{fm}---\n{body}"
    if footer is not None:
        text += f"\n## Связи\n\n{footer}\n"
    return text


def links_(*names: str) -> str:
    return SEP.join(f"[[{n}]]" for n in names)


def plan_of(vault: Path) -> prune.Plan:
    return prune.build_plan(vault)


def removed(plan: prune.Plan) -> set[tuple[str, str]]:
    """Each removed pair as a name-sorted tuple."""
    return {tuple(sorted((prune.title(r.a), prune.title(r.b)))) for r in plan.removals}


def body_after_frontmatter(path: Path) -> str:
    text = path.read_text()
    return text.split("\n---\n", 1)[1]


# -- parsing ----------------------------------------------------------------------


def test_links_are_read_from_body_and_footer_separately():
    data = note(
        "See [[Alpha|the first]] and [[Beta#Origins|b]]. ![[Gamma]] `[[Delta]]`\n"
        "```\n[[Epsilon]]\n```\n%% [[Zeta]] %%\n",
        "**Люди:** [[Eta]] · [[Theta|th]]\n**Концепции:** [[Iota#x]]",
        fm="related: '[[Kappa]]'\n",
    ).encode()
    parsed = prune.parse_note("Library/N.md", data, "## Связи", SEP)
    assert parsed.body_targets == ["Alpha", "Beta"]
    assert [i.simple_target for i in parsed.items] == ["Eta", "Theta", "Iota"]


# -- the rule ---------------------------------------------------------------------


def test_a_footer_only_triangle_loses_exactly_one_side(vault: Path):
    write(vault, "Library/A.md", note("A.\n", links_("B", "C")))
    write(vault, "Library/B.md", note("B.\n", links_("A", "C")))
    write(vault, "Library/C.md", note("C.\n", links_("A", "B")))
    plan = plan_of(vault)
    # Ties go by path: A-B first, via C. Then A-C and B-C have no detour left.
    assert removed(plan) == {("A", "B")}
    assert prune.title(plan.removals[0].detour) == "C"
    assert (plan.edges_before, plan.edges_after) == (3, 2)


def test_greedy_takes_the_highest_degree_pair_first_and_rechecks(vault: Path):
    # D links B and C from its body, so B and C have degree 3 and A 2:
    # B-C goes first (via A); after it, A-B and A-C have no shared
    # neighbour any more. Alphabetical order would have removed A-B.
    write(vault, "Library/A.md", note("A.\n", links_("B", "C")))
    write(vault, "Library/B.md", note("B.\n", links_("A", "C")))
    write(vault, "Library/C.md", note("C.\n", links_("A", "B")))
    write(vault, "Library/D.md", note("See [[B]] and [[C]].\n"))
    plan = plan_of(vault)
    assert removed(plan) == {("B", "C")}
    assert prune.title(plan.removals[0].detour) == "A"


def test_a_body_link_in_either_direction_keeps_the_pair(vault: Path):
    write(vault, "Library/Deleuze.md", note("With [[Guattari]].\n", links_("Guattari", "Hub")))
    write(vault, "Library/Guattari.md", note("G.\n", links_("Deleuze", "Hub")))
    write(vault, "Library/Hub.md", note("H.\n", links_("Deleuze", "Guattari")))
    plan = plan_of(vault)
    assert ("Deleuze", "Guattari") not in removed(plan)
    assert plan.duplicates == {"Library/Deleuze.md": ["Guattari"]}


def test_no_shared_neighbour_means_no_removal(vault: Path):
    write(vault, "Library/A.md", note("A.\n", links_("B")))
    write(vault, "Library/B.md", note("B.\n", links_("C")))
    write(vault, "Library/C.md", note("C.\n"))
    assert removed(plan_of(vault)) == set()


@pytest.mark.parametrize(
    "config, keep_fm",
    [
        ("protected_pairs:\n  - [A, B]\n", ""),
        ("protected: [A]\n", ""),
        ("protected: [Library/Keep]\n", ""),
        ("protected: [Library/Keep/A.md]\n", ""),
        ("", "graph-keep: ['[[B]]']\n"),
    ],
)
def test_protection_keeps_the_pair(vault: Path, config: str, keep_fm: str):
    if config:
        write(vault, "Anchor/graph.md", f"---\n{config}---\n")
    write(vault, "Library/Keep/A.md", note("A.\n", links_("B", "C"), fm=keep_fm or "x: 1\n"))
    write(vault, "Library/B.md", note("B.\n", links_("A", "C")))
    write(vault, "Library/C.md", note("C.\n", links_("A", "B")))
    plan = plan_of(vault)
    assert ("A", "B") not in removed(plan)


def test_an_unusable_graph_keep_protects_the_whole_note(vault: Path):
    write(vault, "Library/A.md", note("A.\n", links_("B", "C"), fm="graph-keep: {x: 1}\n"))
    write(vault, "Library/B.md", note("B.\n", links_("A", "C")))
    write(vault, "Library/C.md", note("C.\n", links_("A", "B")))
    assert removed(plan_of(vault)) == {("B", "C")}


def test_a_link_inside_prose_in_the_footer_is_kept(vault: Path):
    write(vault, "Library/A.md", note("A.\n", "[[B]] и [[C]] вместе" + SEP + "[[C]]"))
    write(vault, "Library/B.md", note("B.\n", links_("C")))
    write(vault, "Library/C.md", note("C.\n"))
    assert ("A", "B") not in removed(plan_of(vault))


def test_links_resolve_by_file_name_not_alias(vault: Path):
    write(vault, "Library/Concepts/A.md", note("A.\n", "[[B|Бэ]]" + SEP + "[[C#Part|см.]]"))
    write(vault, "Library/People/B.md", note("B.\n", links_("C")))
    write(vault, "Library/C.md", note("C.\n"))
    assert removed(plan_of(vault)) == {("A", "C")}


def test_lens_notes_are_in_scope(vault: Path):
    write(vault, "Lens/A.md", note("A.\n", links_("B", "C")))
    write(vault, "Library/B.md", note("B.\n", links_("C")))
    write(vault, "Library/C.md", note("C.\n"))
    assert removed(plan_of(vault)) == {("A", "B")}


def test_report_lists_ghosts_orphans_and_small_components(vault: Path):
    write(vault, "Library/A.md", note("A [[Ghost]].\n", links_("B", "C", "Ghost")))
    write(vault, "Library/B.md", note("B.\n", links_("C", "Ghost", "Other ghost")))
    write(vault, "Library/C.md", note("C.\n"))
    write(vault, "Library/Lonely.md", note("Alone.\n"))
    write(vault, "Library/P.md", note("P.\n", links_("Q")))
    write(vault, "Library/Q.md", note("Q.\n"))
    plan = plan_of(vault)
    assert plan.unresolved == {"Ghost": 2, "Other ghost": 1}
    assert [prune.title(r) for r in plan.orphans] == ["Lonely"]
    assert [[prune.title(r) for r in c] for c in plan.small_components] == [["P", "Q"]]
    text = prune.report(plan, when="2026-10-10")
    assert "- `Ghost` — 2" in text
    assert "### [[A]]" in text and "- [[B]] — обход через [[C]]" in text


def test_a_config_typo_proposes_nothing(vault: Path):
    write(vault, "Anchor/graph.md", "---\nprotected_pair: [[A, B]]\n---\n")
    write(vault, "Library/A.md", note("A.\n", links_("B", "C")))
    plan = plan_of(vault)
    assert plan.config_error == "unknown key: protected_pair"
    assert plan.removals == []


# -- editing footers ----------------------------------------------------------------


def _drop(*names: str):
    return lambda target: target in names


@pytest.mark.parametrize(
    "footer, drop, expected",
    [
        ("[[A]] · [[B]] · [[C]]", ("B",), "[[A]] · [[C]]"),
        ("[[A]] · [[B]] · [[C]]", ("A",), "[[B]] · [[C]]"),
        ("[[A]] · [[B]] · [[C]]", ("C",), "[[A]] · [[B]]"),
        ("[[A]] · [[B]] · [[C]]", ("A", "C"), "[[B]]"),
        ("[[A|alias]] · [[B#H|x]]", ("B",), "[[A|alias]]"),
        ("**Люди:** [[A]] · [[B]]\n**Концепции:** [[C]]", ("C",), "**Люди:** [[A]] · [[B]]"),
        ("**Люди:** [[A]] · [[B]]\n**Концепции:** [[C]]", ("A",), "**Люди:** [[B]]\n**Концепции:** [[C]]"),
        ("**Люди:** [[A]]\n**Концепции:** [[C]] · [[D]]", ("A",), "**Концепции:** [[C]] · [[D]]"),
        ("- [[A]]\n- [[B]]", ("A",), "- [[B]]"),
        ("[[A]] · [[B]]  ", ("B",), "[[A]]  "),
        ("[[A]] · ![[B]] · [[C]]", ("B",), "[[A]] · ![[B]] · [[C]]"),
    ],
)
def test_edit_footer(footer: str, drop: tuple[str, ...], expected: str):
    before = note("Body [[B]] stays.\n", footer).encode()
    after = prune.edit_footer(before, _drop(*drop), "## Связи", SEP).decode()
    assert after == note("Body [[B]] stays.\n", expected)


def test_edit_footer_keeps_crlf_and_text_after_the_footer():
    before = "---\nx: 1\n---\r\nBody.\r\n## Связи\r\n[[A]] · [[B]]\r\n## Later\r\n[[B]]\r\n".encode()
    after = prune.edit_footer(before, _drop("B"), "## Связи", SEP)
    assert after == "---\nx: 1\n---\r\nBody.\r\n## Связи\r\n[[A]]\r\n## Later\r\n[[B]]\r\n".encode()


# -- the routes ---------------------------------------------------------------------


def _triangle(vault: Path) -> None:
    write(vault, "Library/A.md", note("A.\n", links_("B", "C")))
    write(vault, "Library/B.md", note("B.\n", "**Люди:** [[A]]\n**Концепции:** [[C]]"))
    write(vault, "Library/C.md", note("C.\n", links_("A", "B")))


async def _dry_run(client) -> dict:
    resp = await client.get("/v1/graph/prune", headers=AUTH)
    assert resp.status == 200
    return await resp.json()


async def _apply(client, plan_hash: str, changeset: str = "prune-1"):
    return await client.post(
        "/v1/graph/prune", json={"plan_hash": plan_hash, "changeset": changeset}, headers=AUTH
    )


async def test_dry_run_writes_nothing(client, vault: Path):
    _triangle(vault)
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    body = await _dry_run(client)
    assert body["removals"] == 1 and body["files"] == 2
    assert body["edges_before"] == 3 and body["edges_after"] == 2
    assert body["report"].startswith("# Граф ссылок: сухой прогон 2026-09-26\n")
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


async def test_apply_edits_footers_only_and_is_idempotent(client, vault: Path):
    _triangle(vault)
    body = await _dry_run(client)
    resp = await _apply(client, body["plan_hash"])
    assert resp.status == 200
    assert await resp.json() == {"files": 2, "removed": 1, "replayed": False}
    a = (vault / "Library/A.md").read_text()
    b = (vault / "Library/B.md").read_text()
    assert a.endswith("\n## Связи\n\n[[C]]\n")
    assert b.endswith("\n## Связи\n\n**Концепции:** [[C]]\n")
    assert "tags: [x]\nanchor_edited_by: echo\n" in a
    assert (vault / "Library/C.md").read_text() == note("C.\n", links_("A", "B"))
    again = await _dry_run(client)
    assert again["removals"] == 0
    replay = await _apply(client, body["plan_hash"])
    assert await replay.json() == {"files": 2, "removed": 1, "replayed": True}


async def test_a_changed_note_refuses_the_apply_and_writes_nothing(client, vault: Path):
    _triangle(vault)
    body = await _dry_run(client)
    write(vault, "Library/B.md", note("B, edited.\n", "**Люди:** [[A]]\n**Концепции:** [[C]]"))
    a_before = (vault / "Library/A.md").read_bytes()
    resp = await _apply(client, body["plan_hash"])
    assert resp.status == 409
    assert (await resp.json())["error"] == "plan_changed"
    assert (vault / "Library/A.md").read_bytes() == a_before


async def test_one_undo_restores_every_file(client, vault: Path):
    _triangle(vault)
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    body = await _dry_run(client)
    assert (await _apply(client, body["plan_hash"])).status == 200
    changes = await (await client.get("/v1/changes", headers=AUTH)).json()
    [entry] = changes["changes"]
    assert entry["writer"] == "echo" and entry["purpose"] == "prune"
    undo = await client.post("/v1/undo", params={"changeset": "prune-1", "writer": "echo"}, headers=AUTH)
    assert await undo.json() == {"restored": 2, "refused": 0}
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


async def test_another_writers_changeset_id_is_refused(client, vault: Path):
    _triangle(vault)
    write(vault, "Library/X.md", "x\n")
    put = await client.put(
        "/v1/knowledge",
        params={"path": "Library/X.md"},
        json={"content": "y\n", "if_sha256": hashlib.sha256(b"x\n").hexdigest(), "changeset": "cs-1"},
        headers=AUTH,
    )
    assert put.status == 200
    body = await _dry_run(client)
    resp = await _apply(client, body["plan_hash"], changeset="cs-1")
    assert resp.status == 403


async def test_a_bad_body_is_400(client):
    resp = await _apply(client, "not-a-hash")
    assert resp.status == 400
