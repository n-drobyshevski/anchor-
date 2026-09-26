"""The sync-pass notice, hold messages, the `v:` callback and /vault's
problem list (phase-8 plan section 8, milestone 8c phase C).

Each behaviour gets its own test, proved by a deliberate breaking edit
to the code under test (see the module's own report, not this file).
"""

from __future__ import annotations

import datetime
import logging

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import TelegramUpdate, UserState, VaultFile, VaultHold
from app.main import build_dispatcher, build_webhook_app
from app.tg import vault as vault_ui
from app.tg.router import build_router
from app.vault import errors as vault_errors
from app.vault import holds
from app.vault.status import ProblemRow, vault_problems
from app.vault.sync import PassResult
from claude_helpers import PUBLIC_URL
from conftest import FakeLLMProvider, FakeSession
from vault_stub import TOKEN, start_stub

CHAT_ID = 555
TIMEZONE = "Europe/Paris"
EPOCH = "abcdef"
NOW = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=datetime.timezone.utc)


def _clock() -> FrozenClock:
    return FrozenClock(NOW)


def _settings(**kwargs) -> Settings:
    return Settings(VAULT_MODE="sync", VAULT_API_TOKEN=TOKEN, **kwargs)


async def _seed(sessionmaker, **state_kwargs) -> None:
    async with sessionmaker() as session:
        session.add(
            UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, vault_epoch=EPOCH, **state_kwargs)
        )
        await session.commit()


# --- notice_text: pure formatting -------------------------------------------


def _result(**kwargs) -> PassResult:
    return PassResult(**kwargs)


def test_no_notice_when_nothing_changed():
    assert vault_ui.notice_text(_result()) is None


def test_all_three_counts_and_a_quarantine():
    text = vault_ui.notice_text(
        _result(created_facts=1, changed_facts=2, forgotten_facts=1, quarantined=1)
    )
    assert text == "Хранилище: новых 1, изменено 2, забыто 1. Не принято: 1 — /vault"


def test_one_count_and_no_quarantine_omits_the_other_parts():
    text = vault_ui.notice_text(_result(changed_facts=2))
    assert text == "Хранилище: изменено 2 — /vault"


def test_created_only():
    assert vault_ui.notice_text(_result(created_facts=1)) == "Хранилище: новых 1 — /vault"


def test_forgotten_only_with_quarantine():
    text = vault_ui.notice_text(_result(forgotten_facts=3, quarantined=2))
    assert text == "Хранилище: забыто 3. Не принято: 2 — /vault"


def test_quarantine_only_with_no_other_part():
    text = vault_ui.notice_text(_result(quarantined=1))
    assert text == "Хранилище: Не принято: 1 — /vault"


# --- send_pass_updates: notice + holds, gated by may_report_now -------------


async def test_notice_sent_when_allowed(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = _bot()
    await vault_ui.send_pass_updates(
        sessionmaker, bot, _settings(), _clock(), _result(created_facts=1)
    )
    assert fake.sent[-1].text == "Хранилище: новых 1 — /vault"


async def test_no_notice_and_nothing_queued_when_paused(sessionmaker):
    await _seed(sessionmaker, persona_active=False)
    bot, fake = _bot()
    await vault_ui.send_pass_updates(
        sessionmaker, bot, _settings(), _clock(), _result(created_facts=1)
    )
    assert fake.sent == []


async def test_no_notice_during_quiet_hours(sessionmaker):
    # 22:30-08:00 by default; NOW is 10:00 UTC = 12:00 Paris, well
    # outside it, so push the clock into the window instead.
    await _seed(sessionmaker)
    bot, fake = _bot()
    night = FrozenClock(datetime.datetime(2026, 9, 25, 23, 0, tzinfo=datetime.timezone.utc))
    await vault_ui.send_pass_updates(sessionmaker, bot, _settings(), night, _result(created_facts=1))
    assert fake.sent == []


async def test_no_notice_while_quiet_until_is_in_the_future(sessionmaker):
    await _seed(sessionmaker, quiet_until=NOW + datetime.timedelta(hours=1))
    bot, fake = _bot()
    await vault_ui.send_pass_updates(
        sessionmaker, bot, _settings(), _clock(), _result(created_facts=1)
    )
    assert fake.sent == []


async def test_hold_message_has_the_exact_callback_data_and_marks_sent(sessionmaker):
    await _seed(sessionmaker)
    hold_id = await _open_rule_hold(sessionmaker, text="Не звонить после десяти.")
    bot, fake = _bot()
    await vault_ui.send_pass_updates(sessionmaker, bot, _settings(), _clock(), _result())
    [sent] = fake.sent
    assert sent.text == "В хранилище новое правило: «Не звонить после десяти.». Принять?"
    buttons = sent.reply_markup.inline_keyboard[0]
    assert buttons[0].text == "Да" and buttons[0].callback_data == f"v:y:{hold_id}:{EPOCH}"
    assert buttons[1].text == "Нет, вернуть" and buttons[1].callback_data == f"v:n:{hold_id}:{EPOCH}"
    async with sessionmaker() as session:
        hold = await session.get(VaultHold, hold_id)
        assert hold.tg_message_id is not None


async def test_hold_waits_when_not_allowed_then_sends_once_on_a_later_pass(sessionmaker):
    await _seed(sessionmaker, persona_active=False)
    await _open_rule_hold(sessionmaker, text="Правило.")
    bot, fake = _bot()
    await vault_ui.send_pass_updates(sessionmaker, bot, _settings(), _clock(), _result())
    assert fake.sent == []
    # unpause, then the next pass sends it exactly once.
    async with sessionmaker() as session:
        state = (await session.execute(select(UserState))).scalar_one()
        state.persona_active = True
        await session.commit()
    await vault_ui.send_pass_updates(sessionmaker, bot, _settings(), _clock(), _result())
    assert len(fake.sent) == 1
    # a further pass never resends an already-sent hold.
    await vault_ui.send_pass_updates(sessionmaker, bot, _settings(), _clock(), _result())
    assert len(fake.sent) == 1


async def test_mass_delete_hold_text_uses_the_file_count(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await holds.open_mass_delete_hold(session, file_ids=[1, 2, 3], clock=_clock())
        await session.commit()
    bot, fake = _bot()
    await vault_ui.send_pass_updates(sessionmaker, bot, _settings(), _clock(), _result())
    assert fake.sent[-1].text == "Из хранилища пропало 3 факта. Забыть их?"


async def test_rule_edit_hold_text_says_changed_not_new(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/П.md", role="fact")
        session.add(row)
        await session.flush()
        await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Новый текст.", supersedes_id=42, clock=_clock()
        )
        await session.commit()
    bot, fake = _bot()
    await vault_ui.send_pass_updates(sessionmaker, bot, _settings(), _clock(), _result())
    assert fake.sent[-1].text == "В хранилище изменено правило: «Новый текст.». Принять?"


def _bot() -> tuple[Bot, FakeSession]:
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


async def _open_rule_hold(sessionmaker, *, text: str) -> int:
    async with sessionmaker() as session:
        row = VaultFile(path="Anchor/Memory/П.md", role="fact")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text=text, supersedes_id=None, clock=_clock()
        )
        await session.commit()
        return hold.id


# --- Russian plurals ---------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "Из хранилища пропало 1 факт. Забыть его?"),
        (2, "Из хранилища пропало 2 факта. Забыть их?"),
        (5, "Из хранилища пропало 5 фактов. Забыть их?"),
        (11, "Из хранилища пропало 11 фактов. Забыть их?"),
        (21, "Из хранилища пропало 21 факт. Забыть их?"),
    ],
)
def test_mass_delete_plural_forms(n, expected):
    assert vault_ui.mass_delete_text(n) == expected


# --- the v: callback ---------------------------------------------------------


def _callback_update(update_id: int, data: str, *, message_id: int = 900, text: str = "…") -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": text,
            },
        },
    }


def _dispatcher(sessionmaker, settings, clock) -> tuple[Dispatcher, Bot, FakeSession]:
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, FakeLLMProvider(), clock=clock))
    return dp, bot, fake


async def test_confirm_writes_and_answers_and_removes_the_keyboard(sessionmaker):
    await _seed(sessionmaker)
    hold_id = await _open_rule_hold(sessionmaker, text="Не звонить.")
    dp, bot, fake = _dispatcher(sessionmaker, _settings(), _clock())
    update = Update.model_validate(
        _callback_update(1, f"v:y:{hold_id}:{EPOCH}"), context={"bot": bot}
    )
    await dp.feed_update(bot, update)
    assert fake.answered[-1].text == "Принято."
    assert fake.edits, "keyboard was not removed"
    assert fake.edits[-1].reply_markup is None


async def test_revert_answers_and_removes_the_keyboard(sessionmaker):
    await _seed(sessionmaker)
    hold_id = await _open_rule_hold(sessionmaker, text="Не звонить.")
    dp, bot, fake = _dispatcher(sessionmaker, _settings(), _clock())
    update = Update.model_validate(
        _callback_update(1, f"v:n:{hold_id}:{EPOCH}"), context={"bot": bot}
    )
    await dp.feed_update(bot, update)
    assert fake.answered[-1].text == "Вернул как было."
    assert fake.edits[-1].reply_markup is None


async def test_replayed_press_answers_stale_and_keeps_the_keyboard(sessionmaker):
    await _seed(sessionmaker)
    hold_id = await _open_rule_hold(sessionmaker, text="Не звонить.")
    dp, bot, fake = _dispatcher(sessionmaker, _settings(), _clock())
    first = Update.model_validate(_callback_update(1, f"v:y:{hold_id}:{EPOCH}"), context={"bot": bot})
    await dp.feed_update(bot, first)
    edits_after_confirm = len(fake.edits)
    second = Update.model_validate(_callback_update(2, f"v:y:{hold_id}:{EPOCH}"), context={"bot": bot})
    await dp.feed_update(bot, second)
    assert fake.answered[-1].text == "Устарело"
    assert len(fake.edits) == edits_after_confirm  # no further edit on the stale press


async def test_stale_epoch_answers_stale_and_changes_nothing(sessionmaker):
    await _seed(sessionmaker)
    hold_id = await _open_rule_hold(sessionmaker, text="Не звонить.")
    dp, bot, fake = _dispatcher(sessionmaker, _settings(), _clock())
    update = Update.model_validate(
        _callback_update(1, f"v:y:{hold_id}:zzzzzz"), context={"bot": bot}
    )
    await dp.feed_update(bot, update)
    assert fake.answered[-1].text == "Устарело"
    assert fake.edits == []
    async with sessionmaker() as session:
        hold = await session.get(VaultHold, hold_id)
        assert hold.status == holds.PENDING


async def test_malformed_data_answers_stale(sessionmaker):
    await _seed(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker, _settings(), _clock())
    update = Update.model_validate(_callback_update(1, "v:maybe:1:abcdef"), context={"bot": bot})
    await dp.feed_update(bot, update)
    assert fake.answered[-1].text == "Устарело"
    assert fake.edits == []


async def test_malformed_epoch_shape_answers_stale(sessionmaker):
    """Not an epoch mismatch (holds.decide's own check) but a value that
    does not even look like one -- wrong length, or characters outside
    epoch.EPOCH_RE."""
    await _seed(sessionmaker)
    hold_id = await _open_rule_hold(sessionmaker, text="Не звонить.")
    dp, bot, fake = _dispatcher(sessionmaker, _settings(), _clock())
    update = Update.model_validate(
        _callback_update(1, f"v:y:{hold_id}:ABCDEF"), context={"bot": bot}
    )
    await dp.feed_update(bot, update)
    assert fake.answered[-1].text == "Устарело"
    assert fake.edits == []


async def test_stale_apply_when_the_head_moved(sessionmaker):
    from app.core import memory as memory_core

    await _seed(sessionmaker)
    async with sessionmaker() as session:
        head = await memory_core.write_memory(
            session, kind="rule", text="Старый текст.", source="vault", commit=False
        )
        other = await memory_core.write_memory(
            session, kind="identity", text="Другой факт.", source="vault", commit=False
        )
        row = VaultFile(path="Anchor/Memory/П.md", role="fact", memory_id=other.id)
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Новый текст.", supersedes_id=head.id, clock=_clock()
        )
        row.state, row.hold_id = "held", hold.id
        await session.commit()
        hold_id = hold.id
    dp, bot, fake = _dispatcher(sessionmaker, _settings(), _clock())
    update = Update.model_validate(_callback_update(1, f"v:y:{hold_id}:{EPOCH}"), context={"bot": bot})
    await dp.feed_update(bot, update)
    assert fake.answered[-1].text == "Устарело: факт уже изменился."


async def test_duplicate_fact_text(sessionmaker):
    from app.core import memory as memory_core

    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await memory_core.write_memory(
            session, kind="rule", text="Уже есть.", source="vault", commit=False
        )
        row = VaultFile(path="Anchor/Memory/П.md", role="fact")
        session.add(row)
        await session.flush()
        hold = await holds.open_rule_hold(
            session, file_id=row.id, kind="rule", text="Уже есть.", supersedes_id=None, clock=_clock()
        )
        row.state, row.hold_id = "held", hold.id
        await session.commit()
        hold_id = hold.id
    dp, bot, fake = _dispatcher(sessionmaker, _settings(), _clock())
    update = Update.model_validate(_callback_update(1, f"v:y:{hold_id}:{EPOCH}"), context={"bot": bot})
    await dp.feed_update(bot, update)
    assert fake.answered[-1].text == "Такой факт уже есть — файл отмечен в /vault."


# --- /vault's problem list ---------------------------------------------------


def test_format_problems_none_when_no_rows():
    assert vault_ui.format_problems([], 0) is None


def test_format_problems_shows_five_and_the_remainder():
    rows = [ProblemRow(path=f"Anchor/Memory/{i}.md", state="quarantined", reason=vault_errors.EMPTY) for i in range(7)]
    text = vault_ui.format_problems(rows[:5], 7)
    lines = text.splitlines()
    assert lines[0] == "Требуют внимания:"
    assert len(lines) == 1 + 5 + 1
    assert lines[-1] == "…и ещё 2"


def test_format_problems_no_remainder_line_when_exactly_shown():
    rows = [ProblemRow(path="a.md", state="held", reason=None)]
    text = vault_ui.format_problems(rows, 1)
    assert text.splitlines() == ["Требуют внимания:", "- a.md — ждёт ответа в Telegram"]


@pytest.mark.parametrize("code", sorted(vault_errors.QUARANTINE_CODES))
def test_every_quarantine_code_has_a_label(code):
    label = vault_ui.problem_label("quarantined", code)
    assert label and label != vault_ui.UNKNOWN_REASON_LABEL


def test_unknown_code_fails_closed():
    assert vault_ui.problem_label("quarantined", "some_new_code") == vault_ui.UNKNOWN_REASON_LABEL


def test_held_and_diverged_labels_ignore_reason():
    assert vault_ui.problem_label("held", None) == "ждёт ответа в Telegram"
    assert vault_ui.problem_label("diverged", None) == "изменён вручную, больше не обновляю"


async def test_vault_problems_orders_held_then_quarantined_then_diverged(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        base = NOW
        h1 = VaultFile(path="h1.md", role="fact", updated_at=base - datetime.timedelta(minutes=5))
        h2 = VaultFile(path="h2.md", role="fact", updated_at=base)
        session.add_all([h1, h2])
        await session.flush()
        hold1 = await holds.open_rule_hold(
            session, file_id=h1.id, kind="rule", text="t1", supersedes_id=None, clock=_clock()
        )
        hold2 = await holds.open_rule_hold(
            session, file_id=h2.id, kind="rule", text="t2", supersedes_id=None, clock=_clock()
        )
        h1.state, h1.hold_id = "held", hold1.id
        h2.state, h2.hold_id = "held", hold2.id
        session.add(
            VaultFile(path="q1.md", role="fact", state="quarantined", reason=vault_errors.EMPTY, updated_at=base)
        )
        session.add(
            VaultFile(
                path="d1.md", role="journal", state="diverged",
                local_date=datetime.date(2026, 9, 20), updated_at=base,
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        problems, total = await vault_problems(session)
    assert total == 4
    assert [p.path for p in problems] == ["h2.md", "h1.md", "q1.md", "d1.md"]


async def test_vault_command_shows_more_than_five_and_the_remainder(sessionmaker):
    stub, server = await start_stub()
    try:
        await _seed(sessionmaker)
        async with sessionmaker() as session:
            for i in range(6):
                session.add(
                    VaultFile(
                        path=f"Anchor/Memory/{i}.md",
                        role="fact",
                        state="quarantined",
                        reason=vault_errors.EMPTY,
                        updated_at=NOW - datetime.timedelta(minutes=i),
                    )
                )
            await session.commit()
        reply = await _vault_reply(sessionmaker, _settings_stub("sync", stub.url))
        lines = reply.splitlines()
        assert "Требуют внимания:" in lines
        idx = lines.index("Требуют внимания:")
        assert lines[idx + 1 : idx + 6] == [
            f"- Anchor/Memory/{i}.md — пустой факт" for i in range(5)
        ]
        assert lines[-1] == "…и ещё 1"
    finally:
        await server.close()


async def test_status_mode_shows_no_problem_list(sessionmaker):
    stub, server = await start_stub()
    try:
        await _seed(sessionmaker)
        async with sessionmaker() as session:
            session.add(
                VaultFile(path="Anchor/Memory/x.md", role="fact", state="quarantined", reason=vault_errors.EMPTY)
            )
            await session.commit()
        reply = await _vault_reply(sessionmaker, _settings_stub("status", stub.url))
        assert "Требуют внимания:" not in reply
    finally:
        await server.close()


def _settings_stub(mode: str, url: str) -> Settings:
    return Settings(VAULT_MODE=mode, VAULT_API_TOKEN=TOKEN, VAULT_URL=url)


async def _vault_reply(sessionmaker, settings: Settings) -> str:
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, FakeLLMProvider(), clock=_clock()))
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": "/vault",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        },
    }
    await dp.feed_update(bot, Update.model_validate(update, context={"bot": bot}))
    return fake.sent[-1].text


# --- privacy: no path/name/text in any log record ---------------------------


LOGGERS = ("app.tg.vault", "app.tg.router", "app.vault.holds")


@pytest.fixture
def live_loggers(monkeypatch):
    for name in LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


def _record_text(record: logging.LogRecord) -> str:
    parts = [record.getMessage()]
    for key, value in vars(record).items():
        if key in ("msg", "args", "message") or key.startswith("_"):
            continue
        parts.append(f"{key}={value!r}")
    return " ".join(parts)


async def test_no_path_or_text_in_logs_during_vault_reply_and_callback(sessionmaker, caplog, live_loggers):
    caplog.set_level(logging.DEBUG)
    stub, server = await start_stub()
    try:
        await _seed(sessionmaker)
        secret_path = "Anchor/Memory/Тайное.md"
        secret_text = "Секретный факт про меня."
        async with sessionmaker() as session:
            session.add(
                VaultFile(path=secret_path, role="fact", state="quarantined", reason=vault_errors.EMPTY)
            )
            row = VaultFile(path="Anchor/Memory/П2.md", role="fact")
            session.add(row)
            await session.flush()
            hold = await holds.open_rule_hold(
                session, file_id=row.id, kind="rule", text=secret_text, supersedes_id=None, clock=_clock()
            )
            row.state, row.hold_id = "held", hold.id
            await session.commit()
            hold_id = hold.id

        settings = _settings_stub("sync", stub.url)
        await _vault_reply(sessionmaker, settings)

        # Exercises the notice/hold-send log lines too, not just /vault
        # and the callback -- the hold is still pending at this point.
        send_bot, _ = _bot()
        await vault_ui.send_pass_updates(
            sessionmaker, send_bot, settings, _clock(), _result(created_facts=1)
        )

        dp, bot, fake = _dispatcher(sessionmaker, settings, _clock())
        update = Update.model_validate(
            _callback_update(1, f"v:y:{hold_id}:{EPOCH}"), context={"bot": bot}
        )
        await dp.feed_update(bot, update)

        messages = {r.getMessage() for r in caplog.records}
        assert {"vault hold decided", "vault hold sent", "vault notice sent"} <= messages

        for record in caplog.records:
            rendered = _record_text(record)
            assert secret_path not in rendered
            assert secret_text not in rendered
    finally:
        await server.close()


# --- wiring: the `v:` callback through app/main.py's own builders ----------
#
# The LESSON from 8c phase B (see app/main.py's own history): objects the
# webhook app and the dispatcher share were once tested against each
# other separately and still broke in production. This drives a real
# callback_query Update through the Dispatcher build_dispatcher() itself
# returns, after also building the webhook app from the same settings and
# bot the way main() does -- not a bespoke Dispatcher assembled by hand.


def _wiring_settings() -> Settings:
    return Settings(
        MODE="webhook",
        TELEGRAM_BOT_TOKEN="123456:TEST",
        PUBLIC_URL=PUBLIC_URL,
        ALLOWED_CHAT_ID=CHAT_ID,
        VAULT_MODE="sync",
        VAULT_API_TOKEN=TOKEN,
    )


async def test_the_v_callback_reaches_its_handler_through_main_s_own_builders(sessionmaker):
    settings = _wiring_settings()
    clock = _clock()
    dp = build_dispatcher(sessionmaker, settings, FakeLLMProvider(), FakeLLMProvider(), clock)
    fake = FakeSession()
    web_bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, session=fake)
    app = build_webhook_app(
        settings, web_bot, dp, sessionmaker,
        engine=None, provider=None, cheap_provider=None, safety_provider=None,
        llm_client=None, clock=clock,
    )
    app.on_startup.clear()
    app.on_cleanup.clear()
    assert app["dp"] is dp  # the same Dispatcher, not a second one

    await _seed(sessionmaker)
    hold_id = await _open_rule_hold(sessionmaker, text="Через main.py.")
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=9, payload={}))
        await session.commit()

    update = Update.model_validate(
        _callback_update(9, f"v:y:{hold_id}:{EPOCH}"), context={"bot": web_bot}
    )
    await dp.feed_update(web_bot, update)

    assert fake.answered[-1].text == "Принято."
    async with sessionmaker() as session:
        hold = await session.get(VaultHold, hold_id)
        assert hold.status == holds.CONFIRMED_RESULT
    await web_bot.session.close()


# --- worker plumbing: VAULT_SYNC -> outcome.vault_pass_result -> the send --


async def test_a_vault_sync_job_sends_its_own_notice_through_the_worker(sessionmaker, monkeypatch):
    from app.db.models import Job
    from app.vault.sync import run_vault_sync as real_run_vault_sync
    from app import worker as worker_module
    from vault_fake import FakeVault

    vault = FakeVault()
    vault.files["Anchor/Memory/Новый.md"] = (
        "---\nanchor: fact\nkind: identity\npinned: false\nfact: Живёт в Париже.\n---\n"
    )

    async def _fake_run_vault_sync(session, settings, clock, client_factory=None):
        return await real_run_vault_sync(session, settings, clock, vault)

    monkeypatch.setattr(worker_module, "run_vault_sync", _fake_run_vault_sync)

    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(Job(kind="vault_sync", payload={}, dedup_key="vault_sync:test"))
        await session.commit()

    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    claimed = await worker_module.process_one_job(
        sessionmaker, _settings(), FakeLLMProvider(), _clock(), bot
    )
    assert claimed
    assert fake.sent[-1].text == "Хранилище: новых 1 — /vault"
