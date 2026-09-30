"""Reason codes for `knowledge.Refused` / `undo.CapExceeded` (write-plan section 4).

The client keeps the one bare 403 (or, for `GET /v1/knowledge`, the one
bare 404) with an empty body -- refusals stay indistinguishable to the
caller (W2a). The operator log gets a `reason` code instead, drawn from
a closed set, so a human can tell which rule fired. These tests pin:

1. every code is snake_case, and every raise/construction site names one
   from the closed set (statically, via AST -- new call sites cannot
   silently invent a code or skip one);
2. a representative set of refusals logs the right `reason`;
3. the 403 body stays byte-identical and empty across all of them
   (test_knowledge_logs.py already pins this for one case; this file
   adds more without loosening it);
4. no log record carries the path, folder name or title used to trigger
   the refusal.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from pathlib import Path

import pytest

from vaultd import echo, knowledge, undo
from vaultd.undo import REFUSAL_REASONS

from tests.conftest import AUTH, write

_SOURCE_FILES = [Path(knowledge.__file__), Path(undo.__file__), Path(echo.__file__)]


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def note(mark: str = "knowledge", body: str = "Body.\n") -> str:
    return f"---\nanchor: {mark}\n---\n{body}"


# -- (1) the closed set, and every raise site, checked statically -----------


def test_every_reason_code_is_snake_case():
    for code in REFUSAL_REASONS:
        assert code == code.lower()
        assert code.replace("_", "").isalnum()
        assert not code.startswith("_") and not code.endswith("_")


def test_every_refused_and_capexceeded_call_site_names_a_closed_set_code():
    """Every `Refused(...)`/`CapExceeded(...)` call (raise or otherwise)
    passes exactly one positional argument -- a string literal, checked
    here against `REFUSAL_REASONS`, or a variable (e.g. the result of
    `candidate_path_reason`, whose own literals this same scan covers
    where they are actually returned). A bare `Refused()` cannot appear
    here: it would fail this check (zero args) as well as `TypeError` at
    runtime (see test_bare_refused_is_impossible below), and no keyword
    or multi-argument call can sneak past it either."""
    seen_any = False
    for path in _SOURCE_FILES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name not in ("Refused", "CapExceeded"):
                continue
            seen_any = True
            assert not node.keywords, f"{path}:{node.lineno}: {name}(...) must not use a keyword argument"
            assert len(node.args) == 1, f"{path}:{node.lineno}: {name}(...) must take exactly one argument"
            (arg,) = node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                assert arg.value in REFUSAL_REASONS, f"{path}:{node.lineno}: unknown reason code {arg.value!r}"
            else:
                assert isinstance(arg, ast.Name), f"{path}:{node.lineno}: {name}(...) argument must be a name or string literal"
    assert seen_any


def test_every_string_literal_returned_as_a_reason_is_in_the_closed_set():
    """Catches a typo inside a helper like `candidate_path_reason` that
    the call-site scan above cannot see, since it returns a variable."""
    for path in _SOURCE_FILES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or "reason" not in node.name:
                continue
            for ret in ast.walk(node):
                if not isinstance(ret, ast.Return) or ret.value is None:
                    continue
                if isinstance(ret.value, ast.Constant) and isinstance(ret.value.value, str):
                    assert ret.value.value in REFUSAL_REASONS, (
                        f"{path}:{ret.lineno}: {node.name} returns unknown reason code {ret.value.value!r}"
                    )


def test_bare_refused_is_impossible():
    with pytest.raises(TypeError):
        knowledge.Refused()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        undo.CapExceeded()  # type: ignore[call-arg]


def test_unknown_reason_code_is_rejected():
    with pytest.raises(ValueError):
        knowledge.Refused("not_a_real_code")
    with pytest.raises(ValueError):
        undo.CapExceeded("not_a_real_code")


# -- (2)/(3)/(4): representative refusals, exercised over HTTP --------------

SETTINGS = "---\nanchor: settings\nknowledge_folders: [Library]\n---\n"
TITLE = "Секретный-Узел"


@pytest.fixture(autouse=True)
def _base_settings(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    (vault / "Library").mkdir(exist_ok=True)
    (vault / "Elsewhere").mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def _silence_access_log():
    """`log.setup_logging` disables aiohttp's own access logger in
    production, because it prints the raw request line -- query string,
    and so the path, included. Tests never call `setup_logging`, so this
    re-enables that same disabling here, or the leak checks below would
    be checking a logger production never turns on in the first place."""
    access_logger = logging.getLogger("aiohttp.access")
    was_disabled = access_logger.disabled
    access_logger.disabled = True
    yield
    access_logger.disabled = was_disabled


def _log_records(caplog):
    return [r for r in caplog.records if r.__dict__.get("event") == "knowledge_refused"]


def _assert_clean(caplog, *secrets: str) -> None:
    text_parts = []
    for record in caplog.records:
        text_parts.append(record.getMessage())
        text_parts.append(str(record.__dict__))
    blob = "\n".join(text_parts)
    for secret in secrets:
        assert secret not in blob


async def test_folder_not_knowledge_is_logged_and_403_is_unchanged(client, vault: Path, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = await client.put(
            "/v1/knowledge",
            params={"path": f"Elsewhere/{TITLE}.md"},
            json={"content": note(), "if_sha256": None, "changeset": "cs"},
            headers=AUTH,
        )
    assert resp.status == 403
    assert await resp.read() == b""
    records = _log_records(caplog)
    assert [r.__dict__["reason"] for r in records] == ["folder_not_knowledge"]
    assert records[0].__dict__["route"] == "/v1/knowledge"
    _assert_clean(caplog, TITLE, "Elsewhere")


async def test_folder_missing_is_logged_and_403_is_unchanged(client, vault: Path, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = await client.put(
            "/v1/knowledge",
            params={"path": f"Nonexistent/{TITLE}.md"},
            json={"content": note(), "if_sha256": None, "changeset": "cs"},
            headers=AUTH,
        )
    assert resp.status == 403
    assert await resp.read() == b""
    records = _log_records(caplog)
    assert [r.__dict__["reason"] for r in records] == ["folder_missing"]
    _assert_clean(caplog, TITLE, "Nonexistent")


async def test_name_taken_is_logged_and_403_is_unchanged(client, vault: Path, caplog):
    write(vault, f"Library/{TITLE}.md", note())
    with caplog.at_level(logging.DEBUG):
        resp = await client.put(
            "/v1/knowledge",
            params={"path": f"Library/{TITLE}.md"},
            json={"content": note(), "if_sha256": None, "changeset": "cs"},
            headers=AUTH,
        )
    assert resp.status == 403
    assert await resp.read() == b""
    records = _log_records(caplog)
    assert [r.__dict__["reason"] for r in records] == ["name_taken"]
    _assert_clean(caplog, TITLE)


async def test_settings_invalid_is_logged_and_403_is_unchanged(client, vault: Path, caplog):
    write(vault, "Anchor/settings.md", "---\nanchor: settings\nbogus_key: [1]\n---\n")
    with caplog.at_level(logging.DEBUG):
        resp = await client.put(
            "/v1/knowledge",
            params={"path": f"Library/{TITLE}.md"},
            json={"content": note(), "if_sha256": None, "changeset": "cs"},
            headers=AUTH,
        )
    assert resp.status == 403
    assert await resp.read() == b""
    records = _log_records(caplog)
    assert [r.__dict__["reason"] for r in records] == ["settings_invalid"]
    _assert_clean(caplog, TITLE)


async def test_cap_files_is_logged_and_403_is_unchanged(client, vault: Path, caplog):
    write(vault, f"Library/{TITLE}-0.md", note())
    sha0 = sha(note())
    # Fill the changeset's file cap with one write, then push it over.
    await client.put(
        "/v1/knowledge",
        params={"path": f"Library/{TITLE}-0.md"},
        json={"content": note(body="edited\n"), "if_sha256": sha0, "changeset": "cs"},
        headers=AUTH,
    )
    from vaultd.config import FILES_PER_CHANGESET

    for i in range(1, FILES_PER_CHANGESET):
        await client.put(
            "/v1/knowledge",
            params={"path": f"Library/{TITLE}-{i}.md"},
            json={"content": note(), "if_sha256": None, "changeset": "cs"},
            headers=AUTH,
        )
    with caplog.at_level(logging.DEBUG):
        resp = await client.put(
            "/v1/knowledge",
            params={"path": f"Library/{TITLE}-over.md"},
            json={"content": note(), "if_sha256": None, "changeset": "cs"},
            headers=AUTH,
        )
    assert resp.status == 403
    assert await resp.read() == b""
    records = _log_records(caplog)
    assert [r.__dict__["reason"] for r in records] == ["cap_files"]
    _assert_clean(caplog, TITLE)


async def test_ambiguous_basename_on_rename_is_logged_and_403_is_unchanged(client, vault: Path, caplog):
    write(vault, f"Library/A/{TITLE}.md", note())
    write(vault, f"Library/B/{TITLE}.md", note())
    (vault / "Library" / "Dest").mkdir()
    with caplog.at_level(logging.DEBUG):
        resp = await client.post(
            "/v1/knowledge/rename",
            json={
                "path": f"Library/A/{TITLE}.md",
                "new_path": f"Library/Dest/{TITLE}-renamed.md",
                "if_sha256": sha(note()),
                "changeset": "cs",
            },
            headers=AUTH,
        )
    assert resp.status == 403
    assert await resp.read() == b""
    records = _log_records(caplog)
    assert [r.__dict__["reason"] for r in records] == ["ambiguous_basename"]
    _assert_clean(caplog, TITLE, "Dest")


async def test_get_knowledge_404_logs_a_reason_and_body_is_unchanged(client, vault: Path, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = await client.get("/v1/knowledge", params={"path": f"Library/{TITLE}.md"}, headers=AUTH)
    assert resp.status == 404
    body = await resp.json()
    assert body == {"error": "not_found"}
    records = _log_records(caplog)
    assert [r.__dict__["reason"] for r in records] == ["missing"]
    _assert_clean(caplog, TITLE)


async def test_reason_survives_json_encoding_without_the_secret(client, vault: Path, caplog):
    """`json.dumps(ensure_ascii=False)` on the record's own log.py formatter
    must not surface the folder/title either -- only the reason code."""
    from vaultd.log import JsonFormatter

    with caplog.at_level(logging.DEBUG):
        resp = await client.put(
            "/v1/knowledge",
            params={"path": f"Elsewhere/{TITLE}.md"},
            json={"content": note(), "if_sha256": None, "changeset": "cs"},
            headers=AUTH,
        )
    assert resp.status == 403
    formatter = JsonFormatter()
    formatted = [formatter.format(r) for r in caplog.records]
    for line in formatted:
        if '"event": "knowledge_refused"' in line or '"event":"knowledge_refused"' in line:
            parsed = json.loads(line)
            assert parsed.get("reason") == "folder_not_knowledge"
            assert TITLE not in line
            assert "Elsewhere" not in line
