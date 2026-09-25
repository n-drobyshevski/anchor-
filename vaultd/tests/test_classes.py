"""Effective classes, folder rules and the settings file (8e plan sections 3-4, 11).

vaultd is the enforcement point: whatever the bot asks, a note whose
effective class is unclassified or `never` is not listed and not
served, and an unusable settings file hides every note.
"""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

import pytest

from vaultd import classes, frontmatter
from vaultd.manifest import Manifest
from tests.conftest import AUTH, write

SETTINGS = classes.SETTINGS_PATH


def settings(
    knowledge: str = "[]", personal: str = "[]", never: str = "[]", *, extra: str = ""
) -> str:
    return (
        "---\nanchor: settings\n"
        f"knowledge_folders: {knowledge}\npersonal_folders: {personal}\nnever_folders: {never}\n"
        f"{extra}---\n"
    )


def note(mark: str | None) -> str:
    if mark is None:
        return "Текст заметки.\n"
    return f"---\nanchor: {mark}\n---\nТекст заметки.\n"


RULES = classes.parse_settings(
    settings("[Library, Research/CCRU]", "[Life, People]", "[Life/Diary]").encode()
)

# Every (property x folder) pair: the stricter class wins, and a
# property looser than its folder is a conflict.
# (property, folder of the note, expected class, conflict)
PAIRS = [
    (None, "Elsewhere", None, False),
    (None, "Library", "knowledge", False),
    (None, "Life", "personal", False),
    (None, "Life/Diary", None, False),
    ("knowledge", "Elsewhere", "knowledge", False),
    ("knowledge", "Library", "knowledge", False),
    ("knowledge", "Life", "personal", True),
    ("knowledge", "Life/Diary", None, True),
    ("personal", "Elsewhere", "personal", False),
    ("personal", "Library", "personal", False),
    ("personal", "Life", "personal", False),
    ("personal", "Life/Diary", None, True),
    ("never", "Elsewhere", None, False),
    ("never", "Library", None, False),
    ("never", "Life", None, False),
    ("never", "Life/Diary", None, False),
    ("read", "Elsewhere", "personal", False),
    ("read", "Library", "personal", False),
    ("read", "Life/Diary", None, True),
]


@pytest.mark.parametrize(("prop", "folder", "expected", "conflict"), PAIRS)
def test_the_stricter_class_wins(prop, folder, expected, conflict) -> None:
    mark = frontmatter.note_mark(note(prop).encode())
    resolved = classes.effective_class(f"{folder}/Заметка.md", mark, RULES)
    assert resolved.note_class == expected
    assert resolved.conflict == conflict
    assert resolved.legacy_read == (prop == "read")


def test_rules_cover_nested_folders_by_segment() -> None:
    def cls(rel: str) -> str | None:
        return classes.effective_class(rel, "none", RULES).note_class

    assert cls("Library/Philosophy/Land/CCRU.md") == "knowledge"
    assert cls("Research/CCRU/Hyperstition.md") == "knowledge"
    assert cls("Research/Other/x.md") is None
    assert cls("Research/x.md") is None
    assert cls("Life/Diary/2026/09/day.md") is None
    assert cls("Lifestyle/x.md") is None
    assert cls("People2/x.md") is None
    assert cls("Library.md") is None
    assert cls("x.md") is None


def test_nfc_and_nfd_folder_names_match() -> None:
    nfc = unicodedata.normalize("NFC", "Моё/Мой дневник")
    nfd = unicodedata.normalize("NFD", "Моё/Мой дневник")
    assert nfc != nfd
    for rule, path in ((nfc, nfd), (nfd, nfc)):
        rules = classes.parse_settings(settings(personal=f"[{rule.split('/')[0]}]", never=f"[{rule}]").encode())
        assert rules.state == "ok"
        assert classes.effective_class(f"{path}/день.md", "knowledge", rules).note_class is None
        assert classes.effective_class(f"{path.split('/')[0]}/мысль.md", "knowledge", rules).note_class == "personal"


def test_never_rules_match_in_any_case_and_the_others_exactly() -> None:
    rules = classes.parse_settings(settings("[library]", "[life]", "[diary]").encode())
    assert classes.effective_class("Diary/day.md", "knowledge", rules).note_class is None
    assert classes.effective_class("DIARY/day.md", "personal", rules).note_class is None
    assert classes.effective_class("Library/x.md", "none", rules).note_class is None
    assert classes.effective_class("Life/x.md", "none", rules).note_class is None


def test_unknown_values_are_invisible_whatever_the_folder() -> None:
    resolved = classes.effective_class("Library/x.md", "unknown", RULES)
    assert resolved == classes.Resolution(None, unknown_value=True)


def test_absent_settings_means_no_folder_rules() -> None:
    assert classes.effective_class("Library/x.md", "none", classes.SETTINGS_ABSENT).note_class is None
    assert classes.effective_class("Library/x.md", "knowledge", classes.SETTINGS_ABSENT).note_class == "knowledge"


INVALID_SETTINGS = {
    "bad yaml": "---\nanchor: settings\nknowledge_folders: [Library\n---\n",
    "alias": "---\nanchor: settings\nknowledge_folders: &a [Library]\npersonal_folders: *a\n---\n",
    "duplicate key": "---\nanchor: settings\nnever_folders: [Life]\nnever_folders: []\n---\n",
    "unknown key": settings(extra="never_folder: [Life/Diary]\n"),
    "missing anchor: settings": "---\nknowledge_folders: [Library]\n---\n",
    "wrong anchor value": "---\nanchor: Settings\n---\n",
    "anchor not a string": "---\nanchor: [settings]\n---\n",
    "list is a string": settings(knowledge="Library"),
    "list is null": settings(never=""),
    "entry not a string": settings(knowledge="[1]"),
    "entry nested list": settings(knowledge="[[Library]]"),
    "empty entry": settings(knowledge='[""]'),
    "leading slash": settings(knowledge="[/Library]"),
    "trailing slash": settings(knowledge="[Library/]"),
    "dot segment": settings(knowledge="[./Library]"),
    "dotdot segment": settings(knowledge="[Library/../Life]"),
    "dot folder": settings(knowledge="[.obsidian]"),
    "empty segment": settings(knowledge="[Library//x]"),
    "backslash": settings(knowledge='["Library\\\\x"]'),
    "nul": settings(knowledge='["Library\\0"]'),
    "top level is a list": "---\n- anchor: settings\n---\n",
    "no frontmatter": "anchor: settings\n",
    "unclosed fence": "---\nanchor: settings\n",
    "not utf-8": b"---\nanchor: settings\nknowledge_folders: [\xff]\n---\n",
    "python tag": "---\nanchor: !!python/name:os.system settings\n---\n",
}


@pytest.mark.parametrize("name", sorted(INVALID_SETTINGS))
def test_invalid_settings(name: str) -> None:
    raw = INVALID_SETTINGS[name]
    data = raw if isinstance(raw, bytes) else raw.encode()
    assert classes.parse_settings(data) is classes.SETTINGS_INVALID


def test_a_minimal_settings_file_is_valid() -> None:
    assert classes.parse_settings(b"---\nanchor: settings\n---\n").state == "ok"


@pytest.mark.parametrize("name", sorted(INVALID_SETTINGS))
def test_invalid_settings_lists_no_notes(vault: Path, name: str) -> None:
    write(vault, SETTINGS, INVALID_SETTINGS[name])
    write(vault, "Library/CCRU.md", note("knowledge"))
    write(vault, "Anchor/Memory/0001-abcdef.md", "---\nanchor: fact\n---\n")
    scan = Manifest(vault).scan()
    assert [(e.path, e.scope) for e in scan.entries] == [("Anchor/Memory/0001-abcdef.md", "anchor")]
    assert scan.summary.as_json() == {"conflict": 0, "legacy_read": 0, "unknown_value": 0, "settings": "invalid"}


def test_a_symlinked_settings_file_is_invalid(vault: Path, tmp_path: Path) -> None:
    target = write(tmp_path, "elsewhere.md", settings("[Library]"))
    (vault / "Anchor").mkdir()
    os.symlink(target, vault / SETTINGS)
    write(vault, "Library/CCRU.md", note("knowledge"))
    scan = Manifest(vault).scan()
    assert scan.entries == []
    assert scan.summary.settings == "invalid"


def test_a_settings_folder_is_invalid(vault: Path) -> None:
    (vault / SETTINGS).mkdir(parents=True)
    write(vault, "x.md", note("knowledge"))
    assert Manifest(vault).scan().summary.settings == "invalid"


def test_acceptance_library_diary_and_counts(vault: Path) -> None:
    """8e plan section 11, items 1-3."""
    write(vault, SETTINGS, settings("[Library]", "[Life]", "[Life/Diary]"))
    write(vault, "Library/CCRU.md", note(None))
    write(vault, "Life/Diary/2026-09-25.md", note("knowledge"))
    write(vault, "Life/Diary/plain.md", note(None))
    write(vault, "Life/Партнёр.md", note("knowledge"))
    write(vault, "Old/Бег.md", note("read"))
    write(vault, "Old/Опечатка.md", note("knowlege"))
    write(vault, "Unclassified.md", note(None))
    scan = Manifest(vault).scan()
    assert [(e.path, e.note_class) for e in scan.entries] == [
        ("Library/CCRU.md", "knowledge"),
        ("Life/Партнёр.md", "personal"),
        ("Old/Бег.md", "personal"),
    ]
    # The diary note marked knowledge and the partner note are both conflicts.
    assert scan.summary.as_json() == {"conflict": 2, "legacy_read": 1, "unknown_value": 1, "settings": "ok"}


def test_absent_settings_is_reported(vault: Path) -> None:
    write(vault, "x.md", note("knowledge"))
    scan = Manifest(vault).scan()
    assert [e.path for e in scan.entries] == ["x.md"]
    assert scan.summary.settings == "absent"


def test_editing_settings_reclassifies_without_rereading_notes(vault: Path) -> None:
    write(vault, SETTINGS, settings("[Library]"))
    write(vault, "Library/CCRU.md", note(None))
    write(vault, "Library/Diary.md", note(None))
    write(vault, "Life/day.md", note(None))
    manifest = Manifest(vault)
    first = manifest.scan()
    assert [(e.path, e.note_class) for e in first.entries] == [
        ("Library/CCRU.md", "knowledge"),
        ("Library/Diary.md", "knowledge"),
    ]
    assert manifest.last_reads == 3

    write(vault, SETTINGS, settings("[Library]", "[Life]"))
    second = manifest.scan()
    assert manifest.last_reads == 0
    assert [(e.path, e.note_class) for e in second.entries] == [
        ("Library/CCRU.md", "knowledge"),
        ("Library/Diary.md", "knowledge"),
        ("Life/day.md", "personal"),
    ]

    write(vault, SETTINGS, "---\nanchor: settings\nknowledge_folders: [\n---\n")
    assert manifest.scan().entries == []
    assert manifest.last_reads == 0

    write(vault, SETTINGS, settings(never="[Library]"))
    assert manifest.scan().entries == []
    assert manifest.last_reads == 0

    (vault / SETTINGS).unlink()
    assert manifest.scan().entries == []
    assert manifest.last_reads == 0


async def test_the_settings_file_is_never_listed_or_served(client, vault: Path) -> None:
    write(vault, SETTINGS, settings("[Anchor]", "[]", "[]"))
    manifest = await (await client.get("/v1/manifest", headers=AUTH)).json()
    assert manifest["files"] == []
    got = await client.get("/v1/file", params={"path": SETTINGS}, headers=AUTH)
    missing = await client.get("/v1/file", params={"path": "nope.md"}, headers=AUTH)
    assert got.status == 404
    assert await got.text() == await missing.text()
    put = await client.put(
        "/v1/file", params={"path": SETTINGS}, json={"content": "x", "if_sha256": None}, headers=AUTH
    )
    assert put.status == 403


async def test_never_unclassified_and_invalid_are_the_same_404_as_missing(client, vault: Path) -> None:
    write(vault, SETTINGS, settings("[Library]", "[]", "[Library/Diary]"))
    write(vault, "Library/Diary/day.md", note("knowledge"))
    write(vault, "never.md", note("never"))
    write(vault, "plain.md", note(None))
    write(vault, "Library/CCRU.md", note(None))
    names = ("Library/Diary/day.md", "never.md", "plain.md", "missing.md")
    first = [await client.get("/v1/file", params={"path": p}, headers=AUTH) for p in names]
    ok = await client.get("/v1/file", params={"path": "Library/CCRU.md"}, headers=AUTH)
    assert ok.status == 200
    assert (await ok.json())["class"] == "knowledge"

    write(vault, SETTINGS, "---\nanchor: settings\nbogus: 1\n---\n")
    invalid = await client.get("/v1/file", params={"path": "Library/CCRU.md"}, headers=AUTH)
    responses = [*first, invalid]
    assert {r.status for r in responses} == {404}
    assert len({await r.text() for r in responses}) == 1


async def test_the_manifest_summary_never_carries_a_path(client, vault: Path) -> None:
    hidden = ("Секретная папка", "Дневник-2026", "Опечатка")
    write(vault, SETTINGS, settings("[Библиотека]", "[Жизнь]", f"[{hidden[0]}]"))
    write(vault, f"{hidden[0]}/{hidden[1]}.md", note("knowledge"))
    write(vault, f"Жизнь/{hidden[2]}.md", note("knowlege"))
    write(vault, f"{hidden[0]}/старое.md", note("read"))
    write(vault, "Жизнь/Партнёр.md", note("knowledge"))
    resp = await client.get("/v1/manifest", headers=AUTH)
    raw = await resp.text()
    body = json.loads(raw)
    assert body["summary"] == {"conflict": 3, "legacy_read": 1, "unknown_value": 1, "settings": "ok"}
    assert [f["path"] for f in body["files"]] == ["Жизнь/Партнёр.md"]
    summary_json = json.dumps(body["summary"], ensure_ascii=False) + json.dumps(body["summary"])
    serialised = raw + json.dumps(body, ensure_ascii=False)
    for secret in (*hidden, "старое", ".md"):
        assert secret not in summary_json
        if secret != ".md":
            assert secret not in serialised
            assert json.dumps(secret)[1:-1] not in raw
