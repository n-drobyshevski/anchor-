"""/lens, /lens code on|off and the digest's lens line (anchor-lens-plan.md section 11).

Status is counts only; the switch is `ALTER ROLE anchor_lens
LOGIN|NOLOGIN`, run for real against the throwaway cluster's role (and
always put back to NOLOGIN), and the refusal path is exercised by
running the command as a role that may not alter it.
"""

from __future__ import annotations

import contextlib
import datetime

import pytest
from sqlalchemy import text

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.grants import record_library_read
from app.core.scheduler import CLAUDE_LIBRARY_DIGEST_TIME, maybe_enqueue_library_digest
from app.db.models import LensNote, LensRead, UserState, VaultFile
from app.tg import claude as claude_ui
from app.tg import lens as lens_ui
from app.tg.router import BOT_COMMANDS
from app.vault import lens
from app.web import ingress

NOW = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=datetime.timezone.utc)
TODAY = datetime.date(2026, 9, 29)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        self.sent.append((chat_id, text))


async def _seed(sessionmaker, *, people: int = 1, concepts: int = 2, reads_today: int = 0) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="UTC"))
        for i, kind in enumerate(["person"] * people + ["concept"] * concepts):
            row = VaultFile(path=f"Lens/N{i}.md", role="note", note_class="knowledge")
            session.add(row)
            await session.flush()
            session.add(
                LensNote(
                    vault_file_id=row.id, kind=kind, title=f"N{i}", body="x", body_hash=f"{i}" * 64,
                    chars=1,
                )
            )
        for _ in range(reads_today):
            session.add(LensRead(fn="notes", rows=3, at=NOW - datetime.timedelta(hours=1)))
        # Yesterday's read is not today's.
        session.add(LensRead(fn="graph", rows=1, at=NOW - datetime.timedelta(days=1)))
        await session.commit()


async def _can_login(sessionmaker) -> bool | None:
    async with sessionmaker() as session:
        return await lens.code_access(session)


@pytest.fixture
async def nologin_after(sessionmaker):
    """The role is cluster-wide: whatever a test did, it ends NOLOGIN."""
    yield
    async with sessionmaker() as session:
        await session.execute(text("ALTER ROLE anchor_lens NOLOGIN"))
        await session.commit()


def _as_role(sessionmaker, role: str):
    """A sessionmaker whose sessions run as `role` (for this transaction)."""

    @contextlib.asynccontextmanager
    async def maker():
        async with sessionmaker() as session:
            await session.execute(text(f"set local role {role}"))
            yield session

    return maker


# --- /lens -----------------------------------------------------------------------


async def test_status_counts_people_concepts_and_todays_reads(sessionmaker):
    await _seed(sessionmaker, people=1, concepts=2, reads_today=4)
    reply = await lens_ui.command(sessionmaker, Settings(LENS_ENABLED=True), FrozenClock(NOW), None)
    assert reply.splitlines() == [
        lens_ui.ON_LINE,
        "Заметок в линзе: 3 — людей 1 · понятий 2.",
        lens_ui.CODE_OFF_LINE,
        "Чтений сегодня: 4.",
    ]


async def test_status_says_when_the_lens_is_off(sessionmaker):
    await _seed(sessionmaker, people=0, concepts=0)
    reply = await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), "")
    assert reply.splitlines()[0] == lens_ui.OFF_LINE
    assert "Чтений сегодня: 0." in reply


async def test_status_warns_over_the_catalog_limit(sessionmaker):
    await _seed(sessionmaker, people=1, concepts=2)
    settings = Settings(LENS_ENABLED=True, LENS_CATALOG_MAX_NOTES=2)
    reply = await lens_ui.command(sessionmaker, settings, FrozenClock(NOW), None)
    assert lens_ui.OVER_LIMIT_LINE.format(limit=2) in reply.splitlines()
    at_limit = await lens_ui.command(
        sessionmaker, Settings(LENS_ENABLED=True, LENS_CATALOG_MAX_NOTES=3), FrozenClock(NOW), None
    )
    assert "LENS_CATALOG_MAX_NOTES" not in at_limit


async def test_status_never_shows_a_title(sessionmaker):
    await _seed(sessionmaker)
    reply = await lens_ui.command(sessionmaker, Settings(LENS_ENABLED=True), FrozenClock(NOW), None)
    assert "N0" not in reply and "N1" not in reply


async def test_unknown_arguments_show_usage(sessionmaker):
    await _seed(sessionmaker)
    for args in ("code", "code maybe", "on", "code on now"):
        assert await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), args) == lens_ui.USAGE


# --- /lens code on|off -------------------------------------------------------------


async def test_code_on_and_off_flip_the_role(sessionmaker, nologin_after):
    await _seed(sessionmaker)
    assert await _can_login(sessionmaker) is False

    reply = await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), "code on")
    assert reply == lens_ui.CODE_SET_ON
    # L2: the consent reply names every door the role opens, lens.rounds()
    # included, and says the review's rationale is not one of them.
    assert "lens.rounds()" in reply and "какие заметки выбрал еженедельный разбор" in reply
    assert "но не объяснение почему" in reply and "видно только тебе" in reply
    assert await _can_login(sessionmaker) is True
    status = await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), None)
    assert lens_ui.CODE_ON_LINE in status.splitlines()

    reply = await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), "code off")
    assert reply == lens_ui.CODE_SET_OFF
    assert await _can_login(sessionmaker) is False


async def test_code_on_without_privilege_says_so_and_changes_nothing(sessionmaker, nologin_after):
    await _seed(sessionmaker)
    unprivileged = _as_role(sessionmaker, "anchor_debug")
    reply = await lens_ui.command(unprivileged, Settings(), FrozenClock(NOW), "code on")
    assert reply == lens_ui.CODE_DENIED.format(word="LOGIN")
    assert "docs/claude-access.md" in reply
    assert await _can_login(sessionmaker) is False

    reply = await lens_ui.command(unprivileged, Settings(), FrozenClock(NOW), "code off")
    assert reply == lens_ui.CODE_DENIED.format(word="NOLOGIN")


async def test_code_with_the_role_missing_points_to_the_docs(sessionmaker, monkeypatch):
    await _seed(sessionmaker)
    monkeypatch.setattr(lens, "LENS_ROLE", "anchor_lens_never_created")
    reply = await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), "code on")
    assert reply == lens_ui.CODE_MISSING
    assert "docs/claude-access.md" in reply
    status = await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), None)
    assert lens_ui.CODE_MISSING_LINE in status.splitlines()


async def test_code_off_reports_a_failed_terminate(sessionmaker, monkeypatch):
    """NOLOGIN went through but ending open sessions was refused: the
    reply says the door is shut and the sessions may linger."""
    await _seed(sessionmaker)

    async def refused_terminate(session, on):
        return lens.CodeAccess("ok", terminate_failed=True)

    monkeypatch.setattr(lens, "set_code_access", refused_terminate)
    reply = await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), "code off")
    assert reply == lens_ui.CODE_SET_OFF_TERMINATE_FAILED


async def test_code_off_names_the_sessions_it_ended(sessionmaker, monkeypatch):
    await _seed(sessionmaker)

    async def ended_two(session, on):
        return lens.CodeAccess("ok", terminated=2)

    monkeypatch.setattr(lens, "set_code_access", ended_two)
    reply = await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), "code off")
    assert reply == lens_ui.CODE_SET_OFF_TERMINATED.format(n=2)


async def test_code_reports_any_other_database_error_as_retryable(monkeypatch):
    """Only 42501 is "no privilege"; a lock timeout or a dropped
    connection must not send the user off to fix privileges by hand."""
    from sqlalchemy.exc import OperationalError

    class Orig(Exception):
        sqlstate = "55P03"  # lock_not_available

    class Session:
        async def execute(self, *args, **kwargs):
            raise OperationalError("ALTER ROLE", {}, Orig())

        async def rollback(self):
            pass

    async def exists(session):
        return False

    monkeypatch.setattr(lens, "code_access", exists)
    assert (await lens.set_code_access(Session(), False)).state == "error"

    @contextlib.asynccontextmanager
    async def maker():
        yield Session()

    reply = await lens_ui.command(maker, Settings(), FrozenClock(NOW), "code off")
    assert reply == lens_ui.CODE_ERROR.format(word="NOLOGIN")
    assert "Не хватает прав" not in reply


async def test_status_reports_a_read_whose_record_was_rolled_back(sessionmaker):
    await _seed(sessionmaker, people=0, concepts=0)
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_lens"))
        await session.execute(text("select * from lens.notes()"))
        await session.rollback()
    reply = await lens_ui.command(sessionmaker, Settings(), FrozenClock(NOW), None)
    assert lens_ui.UNRECORDED_LINE.format(n=1) in reply.splitlines()
    assert "Чтений сегодня: 0." in reply


def test_lens_is_registered_and_telegram_only():
    assert "lens" in {command.command for command in BOT_COMMANDS}
    assert ingress.is_blocked_command("/lens")
    assert ingress.is_blocked_command("/LENS@anchor_bot code on")


# --- the digest's line ---------------------------------------------------------------


async def _digest(sessionmaker, settings=None) -> list[tuple[int, str]]:
    bot = FakeBot()
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, settings or Settings(), FrozenClock(NOW), bot, {"local_date": TODAY.isoformat()}
        )
    return bot.sent


@pytest.mark.parametrize("n, word", [(1, "раз"), (2, "раза"), (5, "раз"), (12, "раз"), (22, "раза")])
async def test_digest_reports_lens_reads(sessionmaker, n, word):
    await _seed(sessionmaker, people=0, concepts=0, reads_today=n)
    assert await _digest(sessionmaker) == [(555, f"Claude Code прочитал линзу: {n} {word}.")]


async def test_digest_joins_the_library_line(sessionmaker):
    await _seed(sessionmaker, people=0, concepts=0, reads_today=3)
    async with sessionmaker() as session:
        await record_library_read(session, TODAY)
    assert await _digest(sessionmaker) == [
        (555, "Claude за сутки: библиотека — 1 запрос. Claude Code прочитал линзу: 3 раза.")
    ]


async def test_no_lens_reads_no_lens_line(sessionmaker):
    await _seed(sessionmaker, people=0, concepts=0, reads_today=0)
    assert await _digest(sessionmaker) == []
    async with sessionmaker() as session:
        await record_library_read(session, TODAY)
    [(chat, text_sent)] = await _digest(sessionmaker)
    assert "линзу" not in text_sent


async def test_an_evening_read_lands_in_the_next_digest(sessionmaker):
    """The digest goes out at CLAUDE_LIBRARY_DIGEST_TIME; a read after it
    that evening belongs to the next day's digest, not to none."""
    await _seed(sessionmaker, people=0, concepts=0)
    yesterday_evening = datetime.datetime.combine(
        TODAY - datetime.timedelta(days=1), datetime.time(22, 0), tzinfo=datetime.timezone.utc
    )
    tonight = datetime.datetime.combine(TODAY, datetime.time(22, 0), tzinfo=datetime.timezone.utc)
    async with sessionmaker() as session:
        session.add(LensRead(fn="notes", rows=3, at=yesterday_evening))
        session.add(LensRead(fn="notes", rows=3, at=tonight))
        await session.commit()
    assert await _digest(sessionmaker) == [(555, "Claude Code прочитал линзу: 1 раз.")]


async def test_digest_reports_a_rolled_back_read_before_a_recorded_one(sessionmaker):
    await _seed(sessionmaker, people=0, concepts=0)  # id 1: yesterday, outside the window
    async with sessionmaker() as session:
        # id 2 was used by a read whose row was rolled back.
        session.add(LensRead(id=3, fn="notes", rows=3, at=NOW - datetime.timedelta(hours=1)))
        await session.commit()
    assert await _digest(sessionmaker) == [
        (
            555,
            "Claude Code прочитал линзу: 1 раз. "
            + lens_ui.DIGEST_UNRECORDED_LINE.format(n=1),
        )
    ]


async def test_an_open_door_schedules_the_digest_with_both_flags_off(sessionmaker, nologin_after):
    clock = FrozenClock(
        datetime.datetime.combine(TODAY, CLAUDE_LIBRARY_DIGEST_TIME).replace(tzinfo=datetime.timezone.utc)
    )
    await _seed(sessionmaker, people=0, concepts=0)
    async with sessionmaker() as session:
        await session.execute(text("ALTER ROLE anchor_lens LOGIN"))
        await session.commit()
    async with sessionmaker() as session:
        assert await maybe_enqueue_library_digest(session, Settings(), clock, "UTC") is True


async def test_a_read_in_the_window_schedules_the_digest_with_the_door_shut(sessionmaker):
    clock = FrozenClock(
        datetime.datetime.combine(TODAY, CLAUDE_LIBRARY_DIGEST_TIME).replace(tzinfo=datetime.timezone.utc)
    )
    await _seed(sessionmaker, people=0, concepts=0, reads_today=1)
    async with sessionmaker() as session:
        assert await maybe_enqueue_library_digest(session, Settings(), clock, "UTC") is True


async def test_the_lens_alone_schedules_the_digest(sessionmaker):
    clock = FrozenClock(
        datetime.datetime.combine(TODAY, CLAUDE_LIBRARY_DIGEST_TIME).replace(tzinfo=datetime.timezone.utc)
    )
    async with sessionmaker() as session:
        assert await maybe_enqueue_library_digest(session, Settings(), clock, "UTC") is False
    async with sessionmaker() as session:
        assert await maybe_enqueue_library_digest(session, Settings(LENS_ENABLED=True), clock, "UTC") is True


# --- through the router ------------------------------------------------------------


async def test_lens_through_the_router(sessionmaker):
    from aiogram import Bot, Dispatcher
    from aiogram.types import Update

    from app.db.models import TelegramUpdate
    from app.tg.router import build_router
    from conftest import FakeLLMProvider, FakeSession

    await _seed(sessionmaker, people=2, concepts=1, reads_today=1)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        await session.commit()
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, Settings(LENS_ENABLED=True), FakeLLMProvider(), clock=FrozenClock(NOW))
    )
    message = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 555, "is_bot": False, "first_name": "Test"},
            "text": "/lens",
            "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
        },
    }
    await dp.feed_update(bot, Update.model_validate(message, context={"bot": bot}))
    assert fake.sent[-1].text.splitlines()[:2] == [
        lens_ui.ON_LINE,
        "Заметок в линзе: 3 — людей 2 · понятий 1.",
    ]
