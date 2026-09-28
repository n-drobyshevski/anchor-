"""The digest's write line and its [Откатить всё за сутки] button
(W2b, plan section 6.7).
"""

from __future__ import annotations

import datetime

import pytest

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import ClaudeChangeset, OauthConnection, UserState
from app.tg import claude as claude_ui
from app.web import ingress
from tests.claude_write_fake import FakeKnowledgeVault

pytestmark = pytest.mark.asyncio

NOW = datetime.datetime(2026, 9, 26, 21, 5, tzinfo=datetime.timezone.utc)
CHAT_ID = 555


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.edits: list[tuple[int, int, str, object]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        self.sent.append((chat_id, text, reply_markup))

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
        self.edits.append((chat_id, message_id, text, reply_markup))

    async def answer_callback_query(self, callback_id: str, text: str | None = None, **_kw) -> None:
        pass


async def _seed(sessionmaker, *, epoch: str = "aaaaaa") -> OauthConnection:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone="UTC", vault_epoch=epoch))
        connection = OauthConnection(
            client_id="c", created_at=NOW, expires_at=NOW + datetime.timedelta(days=30)
        )
        session.add(connection)
        await session.commit()
        await session.refresh(connection)
        return connection


async def _write_row(
    sessionmaker, connection_id: int, vault_ref: str, *, files=1, created=0, renamed=0, refused=0, when=NOW
) -> ClaudeChangeset:
    async with sessionmaker() as session:
        row = ClaudeChangeset(
            connection_id=connection_id, vault_ref=vault_ref, kind="write",
            files=files, bytes=10, created=created, renamed=renamed, refused=refused,
            created_at=when, last_write_at=when,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def test_write_line_created_marker(sessionmaker):
    connection = await _seed(sessionmaker)
    await _write_row(sessionmaker, connection.id, "v1", created=1)
    vault = FakeKnowledgeVault()
    vault.changesets["v1"] = [_entry("Library/CCRU.md")]
    bot = FakeBot()
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), FrozenClock(NOW), bot, {"local_date": "2026-09-26"},
            client_factory=lambda _s: vault,
        )
    assert bot.sent[0][1] == "Claude за сутки изменил 1 заметку: «CCRU» (создана)."
    assert bot.sent[0][2] is not None  # the undo-all button


async def test_write_line_renamed_marker(sessionmaker):
    connection = await _seed(sessionmaker)
    await _write_row(sessionmaker, connection.id, "v1", files=2, renamed=1)
    vault = FakeKnowledgeVault()
    vault.changesets["v1"] = [_entry("Library/New.md"), _entry_absent("Library/Old.md")]
    bot = FakeBot()
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), FrozenClock(NOW), bot, {"local_date": "2026-09-26"},
            client_factory=lambda _s: vault,
        )
    assert bot.sent[0][1] == "Claude за сутки изменил 1 заметку: «Old» → «New» (переименована)."


async def test_write_line_more_than_ten_titles(sessionmaker):
    connection = await _seed(sessionmaker)
    entries = []
    for i in range(12):
        await _write_row(sessionmaker, connection.id, f"v{i}")
        entries.append((f"v{i}", _entry(f"Library/N{i}.md")))
    vault = FakeKnowledgeVault()
    for ref, entry in entries:
        vault.changesets[ref] = [entry]
    bot = FakeBot()
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), FrozenClock(NOW), bot, {"local_date": "2026-09-26"},
            client_factory=lambda _s: vault,
        )
    text = bot.sent[0][1]
    assert text.startswith("Claude за сутки изменил 12 заметок: ")
    assert "и ещё 2" in text
    assert text.count("«") == 10  # ten titles shown, one opening guillemet each


async def test_write_line_omits_refused_at_zero(sessionmaker):
    connection = await _seed(sessionmaker)
    await _write_row(sessionmaker, connection.id, "v1", created=1, refused=0)
    vault = FakeKnowledgeVault()
    vault.changesets["v1"] = [_entry("Library/CCRU.md")]
    bot = FakeBot()
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), FrozenClock(NOW), bot, {"local_date": "2026-09-26"},
            client_factory=lambda _s: vault,
        )
    assert "Отклонено" not in bot.sent[0][1]


async def test_write_line_includes_refused_when_nonzero(sessionmaker):
    connection = await _seed(sessionmaker)
    await _write_row(sessionmaker, connection.id, "v1", created=1, refused=2)
    vault = FakeKnowledgeVault()
    vault.changesets["v1"] = [_entry("Library/CCRU.md")]
    bot = FakeBot()
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), FrozenClock(NOW), bot, {"local_date": "2026-09-26"},
            client_factory=lambda _s: vault,
        )
    assert bot.sent[0][1].endswith("Отклонено: 2.")


async def test_no_write_activity_no_button(sessionmaker):
    await _seed(sessionmaker)
    from app.core.grants import record_library_read

    async with sessionmaker() as session:
        await record_library_read(session, datetime.date(2026, 9, 26))
    bot = FakeBot()
    vault = FakeKnowledgeVault()
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), FrozenClock(NOW), bot, {"local_date": "2026-09-26"},
            client_factory=lambda _s: vault,
        )
    assert len(bot.sent) == 1
    assert bot.sent[0][2] is None


def _entry(path: str):
    from tests.claude_write_fake import _Entry

    return _Entry(path, None, "a" * 64)


def _entry_absent(path: str):
    from tests.claude_write_fake import _Entry

    return _Entry(path, "old content", None)


# --- the callback: undo that day, stale date, stale epoch, replay -------


async def test_undo_all_callback_undoes_that_days_changesets(sessionmaker):
    from tests.claude_write_fake import _Entry, sha

    connection = await _seed(sessionmaker)
    vault = FakeKnowledgeVault()
    vault.files["Library/New.md"] = "content"
    await _write_row(sessionmaker, connection.id, "v1")
    vault.changesets["v1"] = [_Entry("Library/New.md", None, sha("content"))]
    bot = FakeBot()
    await claude_ui.handle_undo_callback(
            sessionmaker, Settings(), bot, FrozenClock(NOW),
            callback_id="cb1", chat_id=CHAT_ID, message_id=1, data="cu:2026-09-26:aaaaaa",
            client_factory=lambda _s: vault,
        )
    assert bot.edits[-1][2] == "Откатил за 2026-09-26: 1 файл."
    assert "Library/New.md" not in vault.files


async def test_undo_all_callback_stale_date(sessionmaker):
    await _seed(sessionmaker)
    bot = FakeBot()
    await claude_ui.handle_undo_callback(
        sessionmaker, Settings(), bot, FrozenClock(NOW),
        callback_id="cb1", chat_id=CHAT_ID, message_id=1, data="cu:not-a-date:aaaaaa",
    )
    assert bot.edits[-1][2] == claude_ui.UNDO_STALE


async def test_undo_all_callback_stale_epoch(sessionmaker):
    await _seed(sessionmaker, epoch="aaaaaa")
    bot = FakeBot()
    await claude_ui.handle_undo_callback(
        sessionmaker, Settings(), bot, FrozenClock(NOW),
        callback_id="cb1", chat_id=CHAT_ID, message_id=1, data="cu:2026-09-26:zzzzzz",
    )
    assert bot.edits[-1][2] == claude_ui.UNDO_STALE


async def test_undo_all_callback_replay_is_stale(sessionmaker):
    connection = await _seed(sessionmaker)
    vault = FakeKnowledgeVault()
    vault.files["Library/New.md"] = "content"
    await _write_row(sessionmaker, connection.id, "v1")
    from tests.claude_write_fake import _Entry, sha

    vault.changesets["v1"] = [_Entry("Library/New.md", None, sha("content"))]
    bot = FakeBot()
    kwargs = dict(
        callback_id="cb1", chat_id=CHAT_ID, message_id=1, data="cu:2026-09-26:aaaaaa",
        client_factory=lambda _s: vault,
    )
    await claude_ui.handle_undo_callback(sessionmaker, Settings(), bot, FrozenClock(NOW), **kwargs)
    await claude_ui.handle_undo_callback(sessionmaker, Settings(), bot, FrozenClock(NOW), **kwargs)
    assert bot.edits[-1][2] == claude_ui.UNDO_STALE


async def test_web_sink_refused_at_ingress_layer():
    assert "cu:2026-09-26:aaaaaa".startswith(ingress.BLOCKED_CALLBACK_PREFIX)


async def test_web_sink_refused_at_router_layer():
    """Mirrors the `v:`/`g:`/`cl:` router guard -- `cu:` gets the same
    `is_web_sink` check before dispatch (app/tg/router.py)."""
    import inspect

    from app.tg import router as router_module

    source = inspect.getsource(router_module)
    idx = source.index('F.data.startswith("cu:")')
    handler_src = source[idx : idx + 800]
    assert "is_web_sink" in handler_src
