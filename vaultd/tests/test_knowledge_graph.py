"""`GET /v1/knowledge/graph` (lens plan section 4; L1).

Nodes are knowledge and lens notes; edges are parsed by links.py from
those notes only. A link to a note that exists but is not visible is
`{src, outside: true}` and nothing else: no name, no target text, for
every hidden class (personal, never, unclassified, unreadable,
Anchor's own).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from vaultd.config import NOTE_MAX_BYTES, TREE_MAX_NOTES
from vaultd.log import JsonFormatter
from tests.conftest import AUTH, write

SETTINGS = (
    "---\nanchor: settings\n"
    "knowledge_folders: [Library]\npersonal_folders: [Life]\nnever_folders: [Life/Diary]\n"
    "lens_folders: [Lens]\nlens_person_folders: [Lens/People]\n---\n"
)


def note(mark: str | None = None, body: str = "Body.\n", props: str = "") -> str:
    if mark is None and not props:
        return body
    head = f"anchor: {mark}\n" if mark else ""
    return f"---\n{head}{props}---\n{body}"


@pytest.fixture(autouse=True)
def _base_settings(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)


async def _graph(client) -> dict:
    resp = await client.get("/v1/knowledge/graph", headers=AUTH)
    assert resp.status == 200
    return await resp.json()


def _edges_of(graph: dict, src: str) -> list[dict]:
    return [e for e in graph["edges"] if e["src"] == src]


# -- nodes --------------------------------------------------------------------------


async def test_nodes_are_knowledge_and_lens_notes_only(client, vault: Path):
    write(vault, "Library/CCRU.md", note())
    write(vault, "Lens/Cybernetics.md", note())
    write(vault, "Lens/People/Fisher.md", note())
    write(vault, "Lens/Excluded.md", note("knowledge"))
    write(vault, "Life/Партнёр.md", note())
    write(vault, "Life/Diary/day.md", note("lens"))
    write(vault, "Lens/Private.md", note("personal"))
    write(vault, "Elsewhere/plain.md", note())
    write(vault, "Elsewhere/typo.md", note("lense"))
    write(vault, "Anchor/Memory/0001-a.md", note("knowledge"))
    write(vault, "Anchor/Stray.md", note("lens"))

    graph = await _graph(client)
    assert [(n["path"], n["class"], n["lens_kind"]) for n in graph["nodes"]] == [
        ("Lens/Cybernetics.md", "lens", "concept"),
        ("Lens/Excluded.md", "knowledge", None),
        ("Lens/People/Fisher.md", "lens", "person"),
        ("Library/CCRU.md", "knowledge", None),
    ]
    assert graph["truncated"] is False


async def test_a_node_carries_title_chars_and_its_properties(client, vault: Path):
    long = "я" * 400
    text = note(
        "lens",
        "Текст о разнообразии.\n",
        props=f"aliases: [Эшби, Ross Ashby]\ntags: [cybernetics, '#variety']\nsummary: {long}\n",
    )
    write(vault, "Lens/People/Ashby.md", text)
    write(vault, "Library/Bare.md", note(None, "Just text.\n"))
    write(vault, "Library/Odd.md", note("knowledge", props="aliases: 3\ntags: one two\nsummary: [x]\n"))

    nodes = {n["path"]: n for n in (await _graph(client))["nodes"]}
    assert nodes["Lens/People/Ashby.md"] == {
        "path": "Lens/People/Ashby.md",
        "title": "Ashby",
        "class": "lens",
        "lens_kind": "person",
        "aliases": ["Эшби", "Ross Ashby"],
        "tags": ["cybernetics", "variety"],
        "summary": "я" * 300,
        "chars": len(text),
    }
    assert nodes["Library/Bare.md"]["aliases"] == []
    assert nodes["Library/Bare.md"]["tags"] == []
    assert nodes["Library/Bare.md"]["summary"] is None
    assert nodes["Library/Bare.md"]["chars"] == len("Just text.\n")
    assert nodes["Library/Odd.md"]["aliases"] == []
    assert nodes["Library/Odd.md"]["tags"] == ["one", "two"]
    assert nodes["Library/Odd.md"]["summary"] is None


async def test_a_personal_notes_properties_never_reach_the_graph(client, vault: Path):
    props = "aliases: [Тайное имя]\nsummary: Секретно\n"
    write(vault, "Life/Партнёр.md", note("personal", props=props))
    write(vault, "Library/CCRU.md", note())
    raw = await (await client.get("/v1/knowledge/graph", headers=AUTH)).text()
    for secret in ("Тайное имя", "Секретно", "Партнёр"):
        assert secret not in raw
        assert json.dumps(secret)[1:-1] not in raw


async def test_no_body_text_in_the_response(client, vault: Path):
    secret = "совершенно секретный текст"
    write(vault, "Lens/Cybernetics.md", note(None, secret + "\n"))
    raw = await (await client.get("/v1/knowledge/graph", headers=AUTH)).text()
    assert secret not in raw
    assert json.dumps(secret)[1:-1] not in raw


# -- edges ----------------------------------------------------------------------------


async def test_resolved_and_unresolved_edges(client, vault: Path):
    write(
        vault,
        "Lens/Cybernetics.md",
        note(
            None,
            "Ashby: [[Ashby]], again [[Ashby|Эшби]] and [[Ashby#Variety]], embed ![[Ashby]].\n"
            "Library: [[CCRU]]. Wanted: [[Viable system model]], [[Viable system model]].\n"
            "Heading only [[#Top]], self [[Cybernetics]], picture ![[diagram.png]], paper [[beer.PDF]].\n",
        ),
    )
    write(vault, "Lens/People/Ashby.md", note())
    write(vault, "Library/CCRU.md", note(None, "Back to [[Cybernetics]].\n"))

    graph = await _graph(client)
    assert _edges_of(graph, "Lens/Cybernetics.md") == [
        {"src": "Lens/Cybernetics.md", "dst": "Lens/People/Ashby.md"},
        {"src": "Lens/Cybernetics.md", "dst": "Library/CCRU.md"},
        {"src": "Lens/Cybernetics.md", "unresolved": "Viable system model"},
    ]
    assert _edges_of(graph, "Library/CCRU.md") == [{"src": "Library/CCRU.md", "dst": "Lens/Cybernetics.md"}]
    assert _edges_of(graph, "Lens/People/Ashby.md") == []


async def test_resolution_ignores_case_and_honours_a_path_form(client, vault: Path):
    links = "[[ashby]] [[Lens/People/Fisher]] [[Library/Fisher]] [[Nowhere/Fisher]]\n"
    write(vault, "Lens/Cybernetics.md", note(None, links))
    write(vault, "Lens/People/Ashby.md", note())
    write(vault, "Lens/People/Fisher.md", note())
    write(vault, "Library/Fisher.md", note())

    edges = _edges_of(await _graph(client), "Lens/Cybernetics.md")
    assert edges == [
        {"src": "Lens/Cybernetics.md", "dst": "Lens/People/Ashby.md"},
        {"src": "Lens/Cybernetics.md", "dst": "Lens/People/Fisher.md"},
        {"src": "Lens/Cybernetics.md", "dst": "Library/Fisher.md"},
    ]


async def test_a_shared_name_resolves_to_the_same_folder_first(client, vault: Path):
    write(vault, "Library/Topic.md", note())
    write(vault, "Library/Deep/Topic.md", note())
    write(vault, "Library/Deep/Source.md", note(None, "[[Topic]]\n"))
    write(vault, "Library/Other.md", note(None, "[[Topic]]\n"))
    graph = await _graph(client)
    assert _edges_of(graph, "Library/Deep/Source.md") == [
        {"src": "Library/Deep/Source.md", "dst": "Library/Deep/Topic.md"}
    ]
    assert _edges_of(graph, "Library/Other.md") == [{"src": "Library/Other.md", "dst": "Library/Topic.md"}]


HIDDEN = {
    # name: (path, content) -- each one exists, and is not visible.
    "personal": ("Life/Скрытый партнёр.md", note()),
    "personal-marked": ("Library/Личное Имя.md", note("personal")),
    "never": ("Life/Diary/Тайный дневник.md", note("knowledge")),
    "never-marked": ("Lens/Запретное.md", note("never")),
    "unclassified": ("Elsewhere/Неразмеченная.md", note()),
    "unknown-mark": ("Library/Опечатка метки.md", note("knowlege")),
    "anchor-owned": ("Anchor/Memory/0042-факт.md", "fact\n"),
    "anchor-stray": ("Anchor/Отчёт.md", note("knowledge")),
    "over-the-cap": ("Library/Огромная.md", note(None, "x" * (NOTE_MAX_BYTES + 1))),
}


@pytest.mark.parametrize("name", sorted(HIDDEN))
async def test_a_link_to_a_hidden_note_is_outside_and_names_nothing(client, vault: Path, name: str):
    hidden_rel, content = HIDDEN[name]
    hidden_title = hidden_rel.rsplit("/", 1)[-1][: -len(".md")]
    write(vault, hidden_rel, content)
    write(vault, "Lens/Cybernetics.md", note(None, f"See [[{hidden_title}]] and [[{hidden_title}|ярлык]].\n"))

    resp = await client.get("/v1/knowledge/graph", headers=AUTH)
    raw = await resp.text()
    graph = json.loads(raw)
    assert _edges_of(graph, "Lens/Cybernetics.md") == [{"src": "Lens/Cybernetics.md", "outside": True}]
    assert [n["path"] for n in graph["nodes"]] == ["Lens/Cybernetics.md"]
    for secret in (hidden_title, hidden_rel, "ярлык"):
        assert secret not in raw
        assert json.dumps(secret)[1:-1] not in raw


async def test_the_settings_file_is_outside_too(client, vault: Path):
    write(vault, "Library/Note.md", note(None, "[[settings]]\n"))
    graph = await _graph(client)
    assert _edges_of(graph, "Library/Note.md") == [{"src": "Library/Note.md", "outside": True}]


async def test_outside_links_are_counted_once_per_hidden_note(client, vault: Path):
    write(vault, "Life/Один.md", note())
    write(vault, "Life/Два.md", note())
    write(vault, "Lens/Cybernetics.md", note(None, "[[Один]] [[Один]] [[Два]] [[один]]\n"))
    edges = _edges_of(await _graph(client), "Lens/Cybernetics.md")
    assert edges == [{"src": "Lens/Cybernetics.md", "outside": True}] * 2


async def test_hidden_notes_are_never_a_source(client, vault: Path):
    write(vault, "Library/CCRU.md", note())
    write(vault, "Life/Партнёр.md", note(None, "[[CCRU]] [[Wanted from personal]]\n"))
    write(vault, "Elsewhere/plain.md", note(None, "[[CCRU]]\n"))
    write(vault, "Anchor/Memory/0001-a.md", "[[CCRU]] [[Wanted from anchor]]\n")
    raw = await (await client.get("/v1/knowledge/graph", headers=AUTH)).text()
    graph = json.loads(raw)
    assert graph["edges"] == []
    assert "Wanted from" not in raw


async def test_links_inside_an_aside_are_not_edges(client, vault: Path):
    """An aside is private (8e plan section 3); the bot never stores it,
    so none of its links -- to nothing, to a lens note, to a personal
    note -- may become an edge."""
    write(vault, "Lens/People/Maria.md", note())
    write(vault, "Life/Diary/Приём.md", note())
    write(
        vault,
        "Lens/Cybernetics.md",
        note(
            None,
            "Open [[CCRU]].\n"
            "%% private [[Maria diagnosis]] %%\n"
            "%%\nmulti-line [[Maria]] and [[Приём]]\n%%\n",
        ),
    )
    write(vault, "Library/CCRU.md", note())
    raw = await (await client.get("/v1/knowledge/graph", headers=AUTH)).text()
    graph = json.loads(raw)
    assert _edges_of(graph, "Lens/Cybernetics.md") == [{"src": "Lens/Cybernetics.md", "dst": "Library/CCRU.md"}]
    assert "Maria diagnosis" not in raw


async def test_links_inside_code_and_frontmatter_are_not_edges(client, vault: Path):
    write(
        vault,
        "Library/Script.md",
        note(
            "knowledge",
            "Run it:\n```bash\nif [[ -f x ]]; then echo [[Nope]]; fi\n```\nThen [[Wanted]].\n",
            props='related: "[[From properties]]"\n',
        ),
    )
    graph = await _graph(client)
    assert _edges_of(graph, "Library/Script.md") == [{"src": "Library/Script.md", "unresolved": "Wanted"}]


async def test_an_escaped_pipe_link_to_a_hidden_note_is_outside(client, vault: Path):
    """Obsidian's table form `[[Name\\|label]]`: the backslash is the
    separator's, so the link still finds the personal note, and names it not."""
    write(vault, "Life/Diary.md", note("personal"))
    write(vault, "Lens/Table.md", note(None, "| a | [[Diary\\|d]] |\n| b | [[Wanted\\|w]] |\n"))
    raw = await (await client.get("/v1/knowledge/graph", headers=AUTH)).text()
    assert _edges_of(json.loads(raw), "Lens/Table.md") == [
        {"src": "Lens/Table.md", "outside": True},
        {"src": "Lens/Table.md", "unresolved": "Wanted"},
    ]
    assert "Diary" not in raw


async def test_a_visible_note_that_shares_a_hidden_notes_name(client, vault: Path):
    """The visible one in the source's folder wins; the hidden one is not mentioned."""
    write(vault, "Library/Topic.md", note())
    write(vault, "Life/Topic.md", note())
    write(vault, "Library/Source.md", note(None, "[[Topic]]\n"))
    graph = await _graph(client)
    assert _edges_of(graph, "Library/Source.md") == [{"src": "Library/Source.md", "dst": "Library/Topic.md"}]


# -- shape, cap, settings, auth, logs ----------------------------------------------------


async def test_empty_on_invalid_settings(client, vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS.replace("[Lens/People]", "[People]"))
    write(vault, "Library/CCRU.md", note())
    write(vault, "Lens/Cybernetics.md", note(None, "[[CCRU]]\n"))
    assert await _graph(client) == {"nodes": [], "edges": [], "truncated": False}


async def test_without_a_settings_file_only_marked_notes_are_nodes(client, vault: Path):
    (vault / "Anchor" / "settings.md").unlink()
    write(vault, "A/Ashby.md", note("lens", "[[CCRU]]\n"))
    write(vault, "B/CCRU.md", note("knowledge"))
    write(vault, "C/plain.md", note())
    graph = await _graph(client)
    assert [(n["path"], n["class"], n["lens_kind"]) for n in graph["nodes"]] == [
        ("A/Ashby.md", "lens", "concept"),
        ("B/CCRU.md", "knowledge", None),
    ]
    assert graph["edges"] == [{"src": "A/Ashby.md", "dst": "B/CCRU.md"}]


async def test_truncates_at_the_cap(client, vault: Path):
    assert TREE_MAX_NOTES == 2000
    for i in range(TREE_MAX_NOTES + 2):
        write(vault, f"Library/N{i:04d}.md", note(None, f"[[N{(i + 1) % (TREE_MAX_NOTES + 2):04d}]]\n"))
    graph = await _graph(client)
    assert len(graph["nodes"]) == TREE_MAX_NOTES
    assert graph["truncated"] is True
    kept = {n["path"] for n in graph["nodes"]}
    # Edges into a note past the cap go with it; nothing points outside the node set.
    assert all(e["src"] in kept for e in graph["edges"])
    assert all(e["dst"] in kept for e in graph["edges"] if "dst" in e)
    assert len(graph["edges"]) == TREE_MAX_NOTES - 1


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-token-" + "y" * 32}, {"Authorization": AUTH["Authorization"][7:]}],
)
async def test_the_graph_needs_the_token(client, vault: Path, headers):
    write(vault, "Library/CCRU.md", note())
    resp = await client.get("/v1/knowledge/graph", headers=headers)
    assert resp.status == 401
    assert "CCRU" not in await resp.text()


async def test_logs_carry_no_path_title_or_link_text(client, vault: Path, caplog):
    title = "Секретный узел линзы"
    wanted = "Желанная заметка"
    hidden = "Скрытая личная"
    write(vault, f"Lens/{title}.md", note(None, f"[[{wanted}]] [[{hidden}]]\n", props=f"summary: {title}\n"))
    write(vault, f"Life/{hidden}.md", note())
    with caplog.at_level(logging.DEBUG):
        resp = await client.get("/v1/knowledge/graph", headers=AUTH)
    assert resp.status == 200
    formatter = JsonFormatter()
    logged = "\n".join(
        part
        for record in caplog.records
        if record.name.startswith("vaultd")
        for part in (record.getMessage(), str(record.__dict__), formatter.format(record))
    )
    for secret in (title, wanted, hidden, "Lens/"):
        assert secret not in logged
    events = [r for r in caplog.records if r.__dict__.get("event") == "knowledge_graph"]
    assert len(events) == 1
    assert events[0].__dict__["count"] == 1
    routes = {r.__dict__.get("route") for r in caplog.records if r.getMessage() == "request"}
    assert "/v1/knowledge/graph" in routes
