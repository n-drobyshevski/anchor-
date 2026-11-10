"""Echo's stream notes: the body renderer, `/flows notes` and its undo.

No database and no vault: a fake client records what would be sent.
Every title here is synthetic.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from app.core import echo_write, stream_note
from app.core.clock import FrozenClock
from app.tg import planner as planner_ui
from app.vault import errors
from app.vault.client import ChangeEntry, StreamPut, UndoResult, VaultError

NOW = datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.UTC)
TODAY = "2026-11-10"


def stream(key="tag:диплом", **extra):
    base = {
        "key": key,
        "label": key.removeprefix("tag:"),
        "open": 3,
        "done": 1,
        "overdue": 1,
        "needsAttention": 2,
        "nextDue": "2026-11-08",
        "score": 0.9,
        "tasks": [
            {"id": "1", "title": "Глава 2", "dueDate": "2026-11-08", "score": 0.9, "overdue": True},
            {"id": "2", "title": "Список литературы", "dueDate": "2026-11-11", "score": 0.6, "overdue": False},
            {"id": "3", "title": "Когда-нибудь", "score": 0.1, "overdue": False},
        ],
    }
    return {**base, **extra}


def flows(*streams):
    return {"date": TODAY, "groupBy": "tag", "attention": "combined", "streams": list(streams)}


class FakeClient:
    def __init__(self, *, fail=None, changes=None, undo=None):
        self.puts: list[tuple[str, str, str, str]] = []
        self.undone: list[tuple[str, str]] = []
        self.fail = fail or {}
        self.changes = changes or []
        self.undo = undo

    async def put_stream_note(self, name, stream, body, changeset):
        self.puts.append((name, stream, body, changeset))
        outcome = self.fail.get(name, True)
        if isinstance(outcome, str):
            raise VaultError(outcome)
        return StreamPut(sha256="0" * 64, changed=outcome)

    async def list_changes(self):
        if isinstance(self.changes, str):
            raise VaultError(self.changes)
        return self.changes

    async def undo_changeset(self, vault_ref, *, writer="claude"):
        self.undone.append((vault_ref, writer))
        if isinstance(self.undo, str):
            raise VaultError(self.undo)
        return self.undo or UndoResult(restored=1, refused=0)


def change(cid, minute, *, purpose="streams", writer="echo", kind="write", undone=False):
    return ChangeEntry(
        id=cid, kind=kind, time=NOW.replace(minute=minute), undone=undone, files=[], writer=writer, purpose=purpose
    )


# -- the body --------------------------------------------------------------------


def test_the_body_has_counts_absolute_dates_and_only_the_tasks_that_need_attention():
    body = stream_note.render_body(stream(), "tag:диплом")
    assert body.splitlines()[0] == "# диплом"
    assert stream_note.NOTICE in body
    assert "Открыто: 3 · просрочено: 1 · требует внимания: 2 · ближайший срок: 2026-11-08" in body
    assert "- Глава 2 — срок 2026-11-08 · просрочено" in body
    assert "- Список литературы — срок 2026-11-11" in body
    assert "Когда-нибудь" not in body
    assert "завтра" not in body and "сегодня" not in body


def test_task_text_cannot_build_structure_in_the_vault():
    hostile = stream(
        tasks=[{"title": "[[Дневник]] #тег\n## Заголовок <b>x</b> https://evil.example", "score": 0.9}]
    )
    body = stream_note.render_body(hostile, "tag:диплом")
    line = next(line for line in body.splitlines() if line.startswith("- "))
    assert "[[" not in line and "://" not in line and "<b>" not in line
    assert not any(part.startswith("#") for part in line.split())
    assert body.count("\n## ") == 1  # only the note's own heading


def test_a_quiet_stream_has_no_attention_section():
    body = stream_note.render_body(stream(overdue=0, needsAttention=0, nextDue=None, tasks=[]), "tag:дом")
    assert "Открыто: 3" in body and "Требует внимания" not in body and "ближайший" not in body


@pytest.mark.parametrize(
    "bad",
    [None, [], "x", {}, {"streams": "x"}, {"streams": [None, 1, {"key": 5}, {"key": "none"}, {"key": "tag:"}]}],
)
def test_a_malformed_payload_yields_no_notes(bad):
    assert stream_note.notes_for(bad) == []


def test_only_tag_streams_get_a_note_and_names_are_safe_and_unique():
    got = stream_note.notes_for(
        flows(
            stream("tag:диплом"),
            stream("none"),
            stream("category:abc"),
            stream("tag:a/b"),
            stream("tag:A_B"),
            stream("tag:..."),
            stream("tag:zero​width"),
            stream("tag:" + "я" * 200),
        )
    )
    assert [(name, key) for name, key, _ in got] == [("диплом.md", "tag:диплом"), ("a_b.md", "tag:a/b")]
    assert all("/" not in name and not name.startswith(".") for name, _, _ in got)


def test_at_most_twelve_streams():
    got = stream_note.notes_for(flows(*(stream(f"tag:t{i}") for i in range(20))))
    assert len(got) == stream_note.MAX_STREAMS == 12


# -- /flows notes ------------------------------------------------------------------


async def test_write_sends_one_note_per_stream_under_one_changeset():
    client = FakeClient(fail={"дом.md": False})
    result = await echo_write.write_stream_notes(client, flows(stream("tag:диплом"), stream("tag:дом")))
    assert result == echo_write.StreamNotesResult(echo_write.OK, written=1, unchanged=1, skipped=0)
    assert [(name, key) for name, key, _, _ in client.puts] == [("диплом.md", "tag:диплом"), ("дом.md", "tag:дом")]
    refs = {ref for *_, ref in client.puts}
    assert len(refs) == 1 and next(iter(refs)).startswith("streams_")


async def test_a_refused_note_is_skipped_and_the_rest_still_go():
    client = FakeClient(fail={"диплом.md": errors.REFUSED})
    result = await echo_write.write_stream_notes(client, flows(stream("tag:диплом"), stream("tag:дом")))
    assert (result.outcome, result.written, result.skipped) == (echo_write.OK, 1, 1)


async def test_an_unanswering_vault_stops_the_run_and_says_how_far_it_got():
    client = FakeClient(fail={"дом.md": errors.UNAVAILABLE})
    result = await echo_write.write_stream_notes(
        client, flows(stream("tag:диплом"), stream("tag:дом"), stream("tag:работа"))
    )
    assert (result.outcome, result.written) == (echo_write.UNAVAILABLE, 1)
    assert len(client.puts) == 2


async def test_a_title_the_content_screen_refuses_never_reaches_the_vault():
    secret = stream("tag:ключи", tasks=[{"title": "sk-ant-api03-" + "a" * 90, "score": 0.9}])
    client = FakeClient()
    result = await echo_write.write_stream_notes(client, flows(secret, stream("tag:дом")))
    assert (result.written, result.skipped) == (1, 1)
    assert [name for name, *_ in client.puts] == ["дом.md"]


async def test_nothing_to_write_touches_nothing():
    client = FakeClient()
    assert (await echo_write.write_stream_notes(client, flows(stream("none")))).outcome == echo_write.EMPTY
    assert client.puts == []


# -- /flows notes undo ---------------------------------------------------------------


async def test_undo_takes_the_newest_live_stream_changeset_and_no_other_kind():
    client = FakeClient(
        changes=[
            change("streams_old", 1),
            change("streams_new", 5),
            change("streams_done", 9, undone=True),
            change("echo_inbox", 20, purpose=None),
            change("echo_prune", 21, purpose="prune"),
            change("claude_x", 22, writer="claude", purpose=None),
            change("undo-1", 23, kind="undo"),
        ]
    )
    assert await echo_write.undo_stream_notes(client) == echo_write.UNDONE
    assert client.undone == [("streams_new", "echo")]


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (FakeClient(changes=[]), echo_write.NOTHING),
        (FakeClient(changes=errors.UNAVAILABLE), echo_write.UNAVAILABLE),
        (FakeClient(changes=[change("streams_a", 1)], undo=errors.NOT_FOUND), echo_write.EXPIRED),
        (FakeClient(changes=[change("streams_a", 1)], undo=errors.REFUSED), echo_write.REFUSED),
        (FakeClient(changes=[change("streams_a", 1)], undo=UndoResult(restored=0, refused=2)), echo_write.CHANGED),
    ],
)
async def test_undo_outcomes(client, expected):
    assert await echo_write.undo_stream_notes(client) == expected


# -- the command -----------------------------------------------------------------------


def _settings(vault_mode="sync"):
    return SimpleNamespace(VAULT_MODE=vault_mode, PLANNER_SNAPSHOT_MAX_AGE_MIN=60)


def _snapshot(payload=None, fetched_at=NOW):
    payload = {"date": TODAY, "flows": flows(stream())} if payload is None else payload
    return SimpleNamespace(fetched_at=fetched_at, payload=payload)


async def _run(args, *, snapshot=None, settings=None, client=None):
    client = client or FakeClient()
    text = await planner_ui.flows_notes(
        snapshot or _snapshot(), settings or _settings(), FrozenClock(NOW), "UTC", args, client_factory=lambda _s: client
    )
    return text, client


async def test_the_command_writes_and_reports_counts():
    text, client = await _run("notes")
    assert text == "Заметки потоков: записано 1, без изменений 0."
    assert len(client.puts) == 1


async def test_the_command_undoes():
    text, client = await _run("notes undo", client=FakeClient(changes=[change("streams_a", 1)]))
    assert text == planner_ui.NOTES_UNDO_REPLIES[echo_write.UNDONE]
    assert client.puts == []


@pytest.mark.parametrize("args", ["note", "notes now", "notes undo all", "undo"])
async def test_anything_else_is_usage_and_touches_nothing(args):
    text, client = await _run(args)
    assert text == planner_ui.NOTES_USAGE and client.puts == [] and client.undone == []


async def test_a_vault_that_is_off_is_never_called():
    def boom(_settings):
        raise AssertionError("no client when the vault is off")

    text = await planner_ui.flows_notes(
        _snapshot(), _settings("off"), FrozenClock(NOW), "UTC", "notes", client_factory=boom
    )
    assert text == planner_ui.NOTES_VAULT_OFF


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(fetched_at=NOW - datetime.timedelta(hours=5)),
        _snapshot({"date": "2026-11-09", "flows": flows(stream())}),
        _snapshot({"date": TODAY}),
        _snapshot({"date": TODAY, "flows": {**flows(stream()), "date": "2026-11-09"}}),
    ],
)
async def test_stale_or_missing_flows_write_nothing(snapshot):
    text, client = await _run("notes", snapshot=snapshot)
    assert text == planner_ui.FLOWS_STALE and client.puts == []


async def test_a_skipped_note_and_a_lost_vault_are_both_said():
    text, _ = await _run(
        "notes",
        snapshot=_snapshot({"date": TODAY, "flows": flows(stream("tag:диплом"), stream("tag:дом"), stream("tag:x"))}),
        client=FakeClient(fail={"диплом.md": errors.REFUSED, "x.md": errors.UNAVAILABLE}),
    )
    assert "записано 1" in text and "пропущено 1" in text and text.endswith(planner_ui.NOTES_UNAVAILABLE)
