"""Echo's inbox writer (lens L4): `echo_inbox`, `PUT /v1/echo/inbox`, the
`echo` provenance and the writer-scoped undo store.

anchor-lens-plan.md sections 9 and 14.5, the L4 spec sections 5 and 8.
Pinned here:

- the settings key: an explicit value is validated (never under a lens,
  personal or never rule or `Anchor/`, and no lens folder inside it);
  the default `Echo/Inbox` applies only in a valid file with no
  conflicting rule, and a conflict never invalidates the file; no
  settings file, no inbox;
- the route: vaultd builds the path from a bare basename, so a write
  outside the inbox, into a subfolder or through `../` is refused; a
  taken name becomes `name 2`..`name 9`; the frontmatter keys are
  exactly Echo's and `anchor: lens` is refused; the note is stamped
  `echo`; missing inbox folders are created and recorded; a replayed
  changeset writes nothing;
- the undo store: another writer's changeset is refused both ways (a
  write and an undo), Claude's caps -- even at 0 -- never block Echo,
  Echo's writes never spend Claude's, Echo has its own daily and
  hourly caps, and every undo outcome;
- the log names the refusal's reason and never the note's name.

Every note here is synthetic.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from vaultd import classes, echo, frontmatter, provenance
from vaultd.config import ECHO_CHANGESETS_PER_DAY, ECHO_UNDOS_PER_HOUR, FRONTMATTER_MAX_BYTES
from vaultd.log import JsonFormatter
from tests.conftest import AUTH, write

SETTINGS = "---\nanchor: settings\nknowledge_folders: [Library]\n---\n"
NAME = "Необходимое разнообразие.md"


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def settings(extra: str = "") -> str:
    return f"---\nanchor: settings\nknowledge_folders: [Library]\n{extra}---\n"


def note(
    body: str = "Исследование Echo; это не линза.\n",
    *,
    mark: str = "knowledge",
    gap: str = "12",
    urls: str = '["https://plato.stanford.edu/entries/x/"]',
    extra: str = "",
) -> str:
    return f"---\nanchor: {mark}\nsource_urls: {urls}\ngap: {gap}\n{extra}---\n{body}"


async def _put(client, name: str = NAME, content: str | None = None, changeset: str = "echo_a"):
    return await client.put(
        "/v1/echo/inbox",
        json={"name": name, "content": note() if content is None else content, "changeset": changeset},
        headers=AUTH,
    )


async def _undo(client, changeset: str, writer: str | None = "echo"):
    params = {"changeset": changeset}
    if writer is not None:
        params["writer"] = writer
    return await client.post("/v1/undo", params=params, headers=AUTH)


async def _set_limits(client, **values) -> None:
    resp = await client.put("/v1/limits", json=values, headers=AUTH)
    assert resp.status == 200


@pytest.fixture
def base(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    (vault / "Library").mkdir()


# -- the settings key -----------------------------------------------------------


def test_the_default_inbox_applies_in_a_valid_file():
    rules = classes.parse_settings(settings().encode())
    assert rules.state == "ok"
    assert rules.echo_inbox == ("Echo", "Inbox")
    assert echo.inbox_rel(rules) == "Echo/Inbox"
    # The inbox is a knowledge folder: an unmarked note there is knowledge.
    assert classes.effective_class("Echo/Inbox/x.md", "none", rules).note_class == "knowledge"
    # ...and it stays out of the user's own list.
    assert rules.knowledge == (("Library",),)


def test_no_settings_file_and_an_invalid_one_have_no_inbox():
    assert echo.inbox_rel(classes.SETTINGS_ABSENT) is None
    assert echo.inbox_rel(classes.SETTINGS_INVALID) is None
    assert classes.effective_class("Echo/Inbox/x.md", "none", classes.SETTINGS_ABSENT).note_class is None


@pytest.mark.parametrize(
    "extra",
    [
        "personal_folders: [Echo]\n",
        "never_folders: [echo]\n",
        "never_folders: [Echo/Inbox]\n",
        "lens_folders: [Echo]\n",
        "lens_folders: [Echo/Inbox/Lens]\n",
    ],
)
def test_a_conflicting_default_leaves_the_file_valid_and_no_inbox(extra):
    rules = classes.parse_settings(settings(extra).encode())
    assert rules.state == "ok"
    assert rules.echo_inbox is None
    assert classes._folder_class("Echo/Inbox/x.md", rules) != "knowledge"  # noqa: SLF001


def test_an_explicit_value_is_the_inbox():
    rules = classes.parse_settings(settings("echo_inbox: Library/Echo\n").encode())
    assert rules.state == "ok"
    assert rules.echo_inbox == ("Library", "Echo")
    assert echo.inbox_rel(rules) == "Library/Echo"
    rules = classes.parse_settings(settings("echo_inbox: Research\n").encode())
    assert rules.echo_inbox == ("Research",)
    assert classes.effective_class("Research/x.md", "none", rules).note_class == "knowledge"


@pytest.mark.parametrize(
    "extra",
    [
        "echo_inbox: Anchor/Inbox\n",
        "echo_inbox: anchor/Inbox\n",
        "echo_inbox: Life/Inbox\npersonal_folders: [Life]\n",
        "echo_inbox: Diary/Inbox\nnever_folders: [diary]\n",
        "echo_inbox: Lens/Inbox\nlens_folders: [Lens]\n",
        # A lens folder inside the inbox: it joins the knowledge rules
        # before the nesting checks.
        "echo_inbox: Echo\nlens_folders: [Echo/Lens]\n",
        "echo_inbox: [Echo]\n",
        "echo_inbox: 3\n",
        "echo_inbox:\n",
        "echo_inbox: /Echo\n",
        "echo_inbox: Echo/\n",
        "echo_inbox: Echo/../x\n",
        "echo_inbox: .hidden\n",
    ],
)
def test_an_explicit_value_is_validated(extra):
    assert classes.parse_settings(settings(extra).encode()) is classes.SETTINGS_INVALID


# -- the route: happy path ------------------------------------------------------------


async def test_create_in_the_default_inbox_creates_and_records_its_folders(client, vault: Path, base):
    resp = await _put(client)
    assert resp.status == 200
    body = await resp.json()
    assert body == {"name": NAME, "sha256": body["sha256"], "replayed": False}
    data = (vault / "Echo" / "Inbox" / NAME).read_bytes()
    assert body["sha256"] == sha(data)
    text = data.decode()
    assert "anchor_edited_by: echo\n" in text
    assert 'anchor_edited_at: "2026-09-26T12:00:00Z"' in text
    assert "anchor: knowledge\n" in text
    changes = (await (await client.get("/v1/changes", headers=AUTH)).json())["changes"]
    assert changes == [
        {
            "id": "echo_a",
            "kind": "write",
            "time": changes[0]["time"],
            "undone": False,
            "files": [{"path": f"Echo/Inbox/{NAME}", "sha256": body["sha256"]}],
            "writer": "echo",
        }
    ]


async def test_a_taken_name_gets_a_suffix_and_nine_taken_is_refused(client, vault: Path, base):
    (vault / "Echo" / "Inbox").mkdir(parents=True)
    write(vault, f"Echo/Inbox/{NAME}", "моя заметка\n")
    resp = await _put(client, changeset="echo_1")
    assert resp.status == 200
    assert (await resp.json())["name"] == "Необходимое разнообразие 2.md"
    assert (vault / "Echo" / "Inbox" / NAME).read_text() == "моя заметка\n"
    for n in range(3, 10):
        write(vault, f"Echo/Inbox/Необходимое разнообразие {n}.md", "x\n")
    resp = await _put(client, changeset="echo_2")
    assert resp.status == 403
    assert await resp.read() == b""


async def test_a_replayed_changeset_writes_nothing(client, vault: Path, base):
    first = await (await _put(client)).json()
    resp = await _put(client, content=note(body="другой текст\n"))
    assert resp.status == 200
    assert await resp.json() == {"name": NAME, "sha256": first["sha256"], "replayed": True}
    assert sorted(p.name for p in (vault / "Echo" / "Inbox").iterdir()) == [NAME]


async def test_an_explicit_inbox_is_used(client, vault: Path):
    write(vault, "Anchor/settings.md", settings("echo_inbox: Library/Echo\n"))
    (vault / "Library").mkdir()
    resp = await _put(client)
    assert resp.status == 200
    assert (vault / "Library" / "Echo" / NAME).exists()
    assert not (vault / "Echo").exists()


# -- the route: refusals --------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Library/x.md",
        "../x.md",
        "..",
        "Sub/x.md",
        ".x.md",
        "x.txt",
        "x",
        ".md",
        " .md",
        "a\\b.md",
        "a\x07b.md",
        "\u0415\u0308x.md",  # not NFC: NFC composes the two into one letter
        "x" * 118 + ".md",
    ],
)
async def test_a_name_that_is_not_a_bare_safe_basename_is_refused(client, vault: Path, base, name):
    resp = await _put(client, name=name)
    assert resp.status == 403
    assert await resp.read() == b""
    assert not (vault / "Echo").exists()
    assert not (vault / "x.md").exists()


@pytest.mark.parametrize(
    "content",
    [
        note(mark="lens"),
        note(mark="personal"),
        note(extra="aliases: [x]\n"),
        note(extra="anchor_edited_by: claude\n"),
        "---\nanchor: knowledge\ngap: 1\n---\nbody\n",
        "---\nanchor: knowledge\nsource_urls: []\n---\nbody\n",
        "no frontmatter at all\n",
        note(urls="https://x.org"),
        note(urls="[1, 2]"),
        note(gap="twelve"),
        note(gap="0"),
        note(gap="true"),
        "---\nanchor: knowledge\nsource_urls: [\n---\nbody\n",
        note(body="x" * (32 * 1024)),
    ],
)
async def test_content_that_is_not_an_echo_note_is_refused(client, vault: Path, base, content):
    resp = await _put(client, content=content)
    assert resp.status == 403
    assert not (vault / "Echo").exists()


async def test_content_the_stamp_would_push_past_the_frontmatter_cap_is_refused(
    client, vault: Path, base
):
    # One long source URL puts the closing fence just under
    # FRONTMATTER_MAX_BYTES: the bot's bytes pass every check, but the
    # `anchor_edited_*` stamp would push the fence past the cap and leave
    # a note vaultd could not read back as knowledge.
    url = "https://plato.stanford.edu/entries/" + "a" * 3960
    content = note(urls=f'["{url}"]')
    block = content.encode().index(b"\n---\n", 4) + len(b"\n---\n")
    assert 4030 <= block < FRONTMATTER_MAX_BYTES
    echo.check_content(content.encode())
    stamped = provenance.apply(content.encode(), "2026-09-26T12:00:00Z", by="echo")
    assert frontmatter.load(stamped) is None
    resp = await _put(client, content=content)
    assert resp.status == 403
    assert await resp.read() == b""
    assert not (vault / "Echo").exists()
    changes = (await (await client.get("/v1/changes", headers=AUTH)).json())["changes"]
    assert changes == []


async def test_no_settings_file_means_no_inbox(client, vault: Path):
    resp = await _put(client)
    assert resp.status == 403
    assert not (vault / "Echo").exists()


async def test_a_conflicting_default_refuses_but_keeps_the_settings_valid(client, vault: Path):
    write(vault, "Anchor/settings.md", settings("personal_folders: [Echo]\n"))
    (vault / "Library").mkdir()
    write(vault, "Library/Keep.md", "---\nanchor: knowledge\n---\nx\n")
    resp = await _put(client)
    assert resp.status == 403
    assert not (vault / "Echo").exists()
    # The file itself still works: the library is still readable.
    got = await client.get("/v1/knowledge", params={"path": "Library/Keep.md"}, headers=AUTH)
    assert got.status == 200


async def test_invalid_settings_refuse(client, vault: Path):
    write(vault, "Anchor/settings.md", "---\nanchor: settings\nknowledge_folder: [x]\n---\n")
    assert (await _put(client)).status == 403


async def test_a_symlinked_inbox_is_refused(client, vault: Path, base, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "Echo").symlink_to(outside, target_is_directory=True)
    resp = await _put(client)
    assert resp.status == 403
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"name": NAME, "content": "x"},
        {"name": NAME, "content": "x", "changeset": "echo_a", "path": "Library/x.md"},
        {"name": 3, "content": "x", "changeset": "echo_a"},
        {"name": NAME, "content": "x", "changeset": "../x"},
    ],
)
async def test_a_malformed_body_is_a_400(client, base, payload):
    resp = await client.put("/v1/echo/inbox", json=payload, headers=AUTH)
    assert resp.status == 400


async def test_the_route_needs_the_token(client):
    assert (await client.put("/v1/echo/inbox", json={})).status == 401


# -- the writer split -------------------------------------------------------------------


async def test_echo_cannot_join_a_claude_changeset_and_claude_cannot_join_echos(client, vault: Path, base):
    resp = await client.put(
        "/v1/knowledge",
        params={"path": "Library/a.md"},
        json={"content": "a\n", "if_sha256": None, "changeset": "shared"},
        headers=AUTH,
    )
    assert resp.status == 200
    assert (await _put(client, changeset="shared")).status == 403
    assert not (vault / "Echo").exists()

    assert (await _put(client, changeset="echo_b")).status == 200
    resp = await client.put(
        "/v1/knowledge",
        params={"path": "Library/b.md"},
        json={"content": "b\n", "if_sha256": None, "changeset": "echo_b"},
        headers=AUTH,
    )
    assert resp.status == 403
    assert not (vault / "Library" / "b.md").exists()


async def test_claudes_caps_at_zero_never_block_echo(client, vault: Path, base):
    await _set_limits(client, changesets_per_hour=0, undos_per_hour=0, folders_per_day=0, folders_per_changeset=0)
    resp = await _put(client)
    assert resp.status == 200
    resp = await _undo(client, "echo_a")
    assert resp.status == 200
    assert await resp.json() == {"restored": 1, "refused": 0}


async def test_echos_writes_never_spend_claudes_budget(client, vault: Path, base):
    await _set_limits(client, changesets_per_hour=1)
    for i in range(3):
        assert (await _put(client, name=f"n{i}.md", changeset=f"echo_{i}")).status == 200
    resp = await client.put(
        "/v1/knowledge",
        params={"path": "Library/a.md"},
        json={"content": "a\n", "if_sha256": None, "changeset": "claude_1"},
        headers=AUTH,
    )
    assert resp.status == 200


async def test_echo_has_its_own_daily_cap(client, vault: Path, base, clock):
    for i in range(ECHO_CHANGESETS_PER_DAY):
        assert (await _put(client, name=f"n{i}.md", changeset=f"echo_{i}")).status == 200
    assert (await _put(client, name="late.md", changeset="echo_late")).status == 403
    # A counter reset is Claude's; it does not give Echo a new budget.
    await _set_limits(client, counters_reset_at=int(clock().timestamp()))
    assert (await _put(client, name="late.md", changeset="echo_late")).status == 403
    clock.advance(hours=24, seconds=1)
    assert (await _put(client, name="late.md", changeset="echo_late")).status == 200


# -- undo ------------------------------------------------------------------------------


async def test_undo_removes_the_note_and_the_folders_it_created(client, vault: Path, base):
    assert (await _put(client)).status == 200
    resp = await _undo(client, "echo_a")
    assert resp.status == 200
    assert await resp.json() == {"restored": 1, "refused": 0}
    assert not (vault / "Echo").exists()
    changes = (await (await client.get("/v1/changes", headers=AUTH)).json())["changes"]
    assert [(c["kind"], c["writer"], c["undone"]) for c in changes] == [
        ("write", "echo", True),
        ("undo", "echo", False),
    ]


async def test_undo_keeps_a_folder_that_holds_something_else(client, vault: Path, base):
    assert (await _put(client)).status == 200
    write(vault, "Echo/Inbox/mine.md", "моё\n")
    assert (await (await _undo(client, "echo_a")).json()) == {"restored": 1, "refused": 0}
    assert (vault / "Echo" / "Inbox" / "mine.md").exists()
    assert not (vault / "Echo" / "Inbox" / NAME).exists()


async def test_undo_of_an_edited_note_is_refused_by_compare_and_swap(client, vault: Path, base):
    assert (await _put(client)).status == 200
    path = vault / "Echo" / "Inbox" / NAME
    path.write_text(path.read_text() + "моя правка\n")
    resp = await _undo(client, "echo_a")
    assert resp.status == 200
    assert await resp.json() == {"restored": 0, "refused": 1}
    assert path.exists()


async def test_undo_writer_mismatch_is_refused_both_ways(client, vault: Path, base):
    assert (await _put(client)).status == 200
    assert (await _undo(client, "echo_a", writer=None)).status == 403
    assert (await _undo(client, "echo_a", writer="claude")).status == 403
    assert (vault / "Echo" / "Inbox" / NAME).exists()
    resp = await client.put(
        "/v1/knowledge",
        params={"path": "Library/a.md"},
        json={"content": "a\n", "if_sha256": None, "changeset": "claude_1"},
        headers=AUTH,
    )
    assert resp.status == 200
    assert (await _undo(client, "claude_1", writer="echo")).status == 403
    assert (vault / "Library" / "a.md").exists()
    assert (await _undo(client, "claude_1", writer="nobody")).status == 400


async def test_undo_of_an_unknown_or_expired_changeset_is_a_404(client, base, clock):
    assert (await _undo(client, "echo_missing")).status == 404
    assert (await _put(client)).status == 200
    clock.advance(days=14, seconds=1)
    assert (await _undo(client, "echo_a")).status == 404


async def test_echo_undos_have_their_own_hourly_cap(client, vault: Path, base, clock):
    for i in range(ECHO_UNDOS_PER_HOUR + 1):
        assert (await _put(client, name=f"n{i}.md", changeset=f"echo_{i}")).status == 200
    for i in range(ECHO_UNDOS_PER_HOUR):
        assert (await _undo(client, f"echo_{i}")).status == 200
    assert (await _undo(client, f"echo_{ECHO_UNDOS_PER_HOUR}")).status == 403
    # Echo's undos never spend Claude's.
    resp = await client.put(
        "/v1/knowledge",
        params={"path": "Library/a.md"},
        json={"content": "a\n", "if_sha256": None, "changeset": "claude_1"},
        headers=AUTH,
    )
    assert resp.status == 200
    assert (await _undo(client, "claude_1", writer="claude")).status == 200
    clock.advance(hours=1, seconds=1)
    assert (await _undo(client, f"echo_{ECHO_UNDOS_PER_HOUR}")).status == 200


# -- provenance and logs -----------------------------------------------------------------


def test_provenance_names_the_writer_and_refuses_any_other():
    stamped = provenance.apply(b"---\nanchor: knowledge\n---\nx\n", "2026-09-26T12:00:00Z", by="echo")
    assert b"anchor_edited_by: echo\n" in stamped
    stamped = provenance.apply(b"x\n", "2026-09-26T12:00:00Z", by="claude")
    assert stamped.startswith(b"---\nanchor_edited_by: claude\n")
    with pytest.raises(ValueError):
        provenance.apply(b"x\n", "2026-09-26T12:00:00Z", by="grok")
    with pytest.raises(TypeError):
        provenance.apply(b"x\n", "2026-09-26T12:00:00Z")  # type: ignore[call-arg]


async def test_logs_carry_the_reason_and_never_the_name(client, vault: Path, base, caplog):
    secret = "Тайная заметка о гомеостате.md"
    with caplog.at_level(logging.DEBUG):
        await _put(client, name=secret, content=note(mark="lens"), changeset="echo_x")
        await _put(client, name=secret, changeset="echo_y")
        await _undo(client, "echo_y")
    formatter = JsonFormatter()
    dumped = "\n".join(
        part for r in caplog.records for part in (r.getMessage(), str(r.__dict__), formatter.format(r))
    )
    assert "content_not_knowledge" in dumped
    assert "Тайная" not in dumped
    assert "Echo/Inbox" not in dumped
    assert "plato" not in dumped


def test_the_echo_caps_are_the_specs_constants():
    assert ECHO_CHANGESETS_PER_DAY == 20
    assert ECHO_UNDOS_PER_HOUR == 4
