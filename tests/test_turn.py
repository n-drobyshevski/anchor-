"""core/turn.py tests (plan section 8 / 16).

Every test uses FakeLLMProvider and FakeSession -- no test in this
module ever reaches the network. `LLMRetryableError(retry_after=0)` is
used throughout so turn.py's retry backoff sleeps are effectively
instant and the suite stays fast.
"""

from __future__ import annotations

import datetime
import decimal
import re
from pathlib import Path

from aiogram import Bot
from sqlalchemy import select

from app.config import Settings
from app.core import prompt, turn
from app.core.clock import SystemClock
from app.core.clock import local_date as clock_local_date
from app.core.state import get_state
from app.db.models import Message, SpendLedger, StateChange, TelegramUpdate, UserState
from app.llm.openrouter import OpenRouterProvider
from app.llm.provider import LLMError, LLMMessage, LLMRetryableError, LLMUsage
from conftest import FakeLLMProvider, FakeSession

TEST_CHAT_ID = 4242
TIMEZONE = "Europe/Paris"


async def _seed(
    sessionmaker,
    *,
    chat_id: int = TEST_CHAT_ID,
    update_id: int,
    intensity: int = 3,
    persona_active: bool = True,
) -> None:
    async with sessionmaker() as session:
        session.add(
            UserState(
                id=1,
                chat_id=chat_id,
                timezone=TIMEZONE,
                intensity=intensity,
                persona_active=persona_active,
            )
        )
        await session.commit()
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


def _bot() -> tuple[Bot, FakeSession]:
    fake_session = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake_session), fake_session


async def _assistant_row(sessionmaker, update_id: int) -> Message | None:
    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.reply_to_update == update_id))
        return result.scalar_one_or_none()


async def _user_rows(sessionmaker, update_id: int) -> list[Message]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(Message).where(Message.update_id == update_id, Message.role == "user")
        )
        return list(result.scalars().all())


async def _ledger_rows(sessionmaker) -> list[SpendLedger]:
    async with sessionmaker() as session:
        result = await session.execute(select(SpendLedger))
        return list(result.scalars().all())


async def test_successful_turn_stores_rows_sends_reply_and_writes_ledger(sessionmaker, clock):
    update_id = 1
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    usage = LLMUsage(input_tokens=120, cached_tokens=20, output_tokens=40, cost_usd=None)
    provider = FakeLLMProvider(text="Принято. Дальше.", usage=usage, model="cydonia-fake")

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="привет",
    )

    assert provider.calls == 1
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == "Принято. Дальше."
    assert fake_session.sent[0].chat_id == TEST_CHAT_ID

    user_rows = await _user_rows(sessionmaker, update_id)
    assert len(user_rows) == 1
    assert user_rows[0].content == "привет"

    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant is not None
    assert assistant.content == "Принято. Дальше."
    assert assistant.model == "cydonia-fake"
    assert assistant.tokens_in == 120
    assert assistant.tokens_cached == 20
    assert assistant.tokens_out == 40
    assert assistant.usd_cost is not None
    assert assistant.sent_at is not None

    ledger = await _ledger_rows(sessionmaker)
    assert len(ledger) == 1
    assert ledger[0].usd_cost == assistant.usd_cost
    assert ledger[0].local_date == clock_local_date(SystemClock(), TIMEZONE)
    assert ledger[0].category == "chat"

    await bot.session.close()


async def test_resend_when_sent_at_is_null(sessionmaker, clock):
    """Simulates a crash between generating the reply and sending it:
    the assistant row exists with sent_at NULL. turn.run() must resend
    the stored content without calling the provider again.
    """
    update_id = 2
    await _seed(sessionmaker, update_id=update_id)
    async with sessionmaker() as session:
        session.add(
            Message(
                role="assistant",
                content="Уже сгенерированный ответ.",
                update_id=update_id,
                reply_to_update=update_id,
                sent_at=None,
                usd_cost=decimal.Decimal("0.001000"),
            )
        )
        await session.commit()

    bot, fake_session = _bot()
    provider = FakeLLMProvider()

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="привет ещё раз",
    )

    assert provider.calls == 0
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == "Уже сгенерированный ответ."

    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant.sent_at is not None

    await bot.session.close()


async def test_no_regeneration_when_assistant_row_already_sent(sessionmaker, clock):
    update_id = 3
    await _seed(sessionmaker, update_id=update_id)
    sent_at = datetime.datetime.now(datetime.timezone.utc)
    async with sessionmaker() as session:
        session.add(
            Message(
                role="assistant",
                content="Уже отправлено.",
                update_id=update_id,
                reply_to_update=update_id,
                sent_at=sent_at,
                usd_cost=decimal.Decimal("0.001000"),
            )
        )
        await session.commit()

    bot, fake_session = _bot()
    provider = FakeLLMProvider()

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="привет снова",
    )

    assert provider.calls == 0
    assert fake_session.sent == []

    await bot.session.close()


async def test_over_cap_makes_zero_provider_calls_and_writes_no_ledger_row(sessionmaker, clock):
    update_id = 4
    await _seed(sessionmaker, update_id=update_id)
    settings = Settings(DAILY_USD_CAP=0.50)
    today = clock_local_date(SystemClock(), TIMEZONE)
    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("0.50")))
        await session.commit()

    bot, fake_session = _bot()
    provider = FakeLLMProvider()

    await turn.run(
        sessionmaker,
        bot,
        settings,
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="ещё один вопрос",
    )

    assert provider.calls == 0
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == turn.CAP_REPLY_TEXT

    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant is not None
    assert assistant.usd_cost == decimal.Decimal("0")
    assert assistant.sent_at is not None

    # Still only the one ledger row seeded above -- the cap path must
    # never write a second one.
    ledger = await _ledger_rows(sessionmaker)
    assert len(ledger) == 1

    await bot.session.close()


async def test_provider_non_retryable_failure_stores_no_assistant_row(sessionmaker, clock):
    update_id = 5
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    provider = FakeLLMProvider(raises=[LLMError("BadRequestError")])

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="сообщение",
    )

    assert provider.calls == 1
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == turn.FAILURE_REPLY_TEXT

    assert await _assistant_row(sessionmaker, update_id) is None
    assert await _ledger_rows(sessionmaker) == []

    await bot.session.close()


async def test_provider_retries_exhausted_stores_no_assistant_row(sessionmaker, clock):
    update_id = 6
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    provider = FakeLLMProvider(
        raises=[
            LLMRetryableError(retry_after=0),
            LLMRetryableError(retry_after=0),
            LLMRetryableError(retry_after=0),
        ]
    )

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="сообщение",
    )

    # MAX_RETRIES=2 -> 3 total attempts before giving up.
    assert provider.calls == 3
    assert fake_session.sent[0].text == turn.FAILURE_REPLY_TEXT
    assert await _assistant_row(sessionmaker, update_id) is None

    await bot.session.close()


async def test_provider_retries_then_succeeds(sessionmaker, clock):
    update_id = 7
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    provider = FakeLLMProvider(
        text="Всё-таки получилось.",
        raises=[LLMRetryableError(retry_after=0), LLMRetryableError(retry_after=0)],
    )

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="сообщение",
    )

    assert provider.calls == 3
    assert fake_session.sent[0].text == "Всё-таки получилось."
    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant is not None
    assert assistant.sent_at is not None

    await bot.session.close()


async def test_no_double_user_message_across_two_turns(sessionmaker, clock):
    """End-to-end regression: run two real turns and inspect exactly
    what was sent to the provider on the second call. The second
    call's transcript must contain the first turn's user+assistant
    messages exactly once each, and the second user's text must appear
    only as the final message, never duplicated via the transcript.
    """
    provider = FakeLLMProvider(text="Ответ номер один.")
    bot, fake_session = _bot()

    await _seed(sessionmaker, update_id=10)
    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=10,
        user_text="первое сообщение",
    )

    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=11, payload={}))
        await session.commit()

    provider.text = "Ответ номер два."
    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=11,
        user_text="второе сообщение",
    )

    assert provider.calls == 2
    second_call_messages = provider.received_messages[1]
    contents = [m.content for m in second_call_messages]

    assert contents.count("первое сообщение") == 1
    assert contents.count("второе сообщение") == 1
    # The new user text must be the last message, not folded into the
    # transcript a second time.
    assert second_call_messages[-1].role == "user"
    assert second_call_messages[-1].content == "второе сообщение"

    await bot.session.close()


# --- 1d: pause words, neutral mode, /out and /in (plan section 7 / 16) ---


async def test_hard_pause_word_makes_zero_provider_calls_and_no_ledger_row(sessionmaker, clock):
    update_id = 100
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    provider = FakeLLMProvider()

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="Пурпурный!!",
    )

    assert provider.calls == 0
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == turn.PAUSE_REPLY_TEXT

    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant is not None
    assert assistant.usd_cost == decimal.Decimal("0")
    assert assistant.sent_at is not None
    assert await _ledger_rows(sessionmaker) == []

    user_rows = await _user_rows(sessionmaker, update_id)
    assert user_rows[0].ooc is True  # the safeword itself never enters the persona transcript

    await bot.session.close()


async def test_pause_word_through_the_search_path_still_pauses_with_zero_provider_calls(sessionmaker, clock):
    """1f: web_search=True must not bypass step 0 (pause.match()) -- a
    pause word sent via /search still pauses and never reaches the model."""
    update_id = 150
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    provider = FakeLLMProvider()

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="пурпурный",
        web_search=True,
    )

    assert provider.calls == 0
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == turn.PAUSE_REPLY_TEXT

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.persona_active is False

    await bot.session.close()


async def test_hard_pause_word_sets_persona_active_false_with_pause_state_change(sessionmaker, clock):
    update_id = 101
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    provider = FakeLLMProvider()

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="красный",
    )

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.persona_active is False

    async with sessionmaker() as session:
        result = await session.execute(select(StateChange))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].field == "persona_active"
    assert rows[0].source == "pause"
    assert rows[0].new_value == "False"

    await bot.session.close()


async def test_soft_pause_word_decrements_intensity_and_flag_reaches_prompt(sessionmaker, clock):
    update_id = 102
    await _seed(sessionmaker, update_id=update_id, intensity=3)
    bot, fake_session = _bot()
    provider = FakeLLMProvider(text="Помягче отвечаю.")

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="жёлтый",
    )

    assert provider.calls == 1
    contents = [m.content for m in provider.received_messages[0]]
    assert any(turn.YELLOW_FLAG in content for content in contents)

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.intensity == 2

    async with sessionmaker() as session:
        result = await session.execute(select(StateChange).where(StateChange.field == "intensity"))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].source == "pause"
    assert rows[0].old_value == "3"
    assert rows[0].new_value == "2"

    assert fake_session.sent[0].text == "Помягче отвечаю."

    await bot.session.close()


async def test_soft_pause_word_at_intensity_one_still_runs_with_flag(sessionmaker, clock):
    update_id = 103
    await _seed(sessionmaker, update_id=update_id, intensity=1)
    bot, fake_session = _bot()
    provider = FakeLLMProvider(text="Ответ на минимуме.")

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="желтый",
    )

    assert provider.calls == 1
    contents = [m.content for m in provider.received_messages[0]]
    assert any(turn.YELLOW_FLAG in content for content in contents)
    assert fake_session.sent[0].text == "Ответ на минимуме."

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.intensity == 1  # max(1, 1-1) == 1, clamped, turn still ran

    await bot.session.close()


async def test_neutral_mode_uses_neutral_prompt_ooc_context_and_ooc_category(sessionmaker, clock):
    update_id = 104
    await _seed(sessionmaker, update_id=update_id, persona_active=False)

    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=900, payload={}))
        session.add(TelegramUpdate(update_id=901, payload={}))
        await session.commit()
        session.add(Message(role="user", content="в роли история", update_id=900, ooc=False))
        session.add(Message(role="user", content="прошлый ooc", update_id=901, ooc=True))
        await session.commit()

    bot, fake_session = _bot()
    provider = FakeLLMProvider(text="Нейтральный ответ.")

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="как дела",
    )

    assert provider.calls == 1
    messages = provider.received_messages[0]
    assert messages[0].role == "system"
    assert messages[0].content == prompt.NEUTRAL_SYSTEM_PROMPT

    contents = [m.content for m in messages]
    assert "прошлый ooc" in contents
    assert "в роли история" not in contents

    user_rows = await _user_rows(sessionmaker, update_id)
    assert user_rows[0].ooc is True

    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant.ooc is True

    ledger = await _ledger_rows(sessionmaker)
    assert len(ledger) == 1
    assert ledger[0].category == "ooc"

    await bot.session.close()


async def test_paused_bot_stays_paused_across_several_turns(sessionmaker, clock):
    await _seed(sessionmaker, update_id=1000, persona_active=False)
    bot, fake_session = _bot()
    provider = FakeLLMProvider(text="Нейтрально.")

    for uid in (105, 106, 107):
        async with sessionmaker() as session:
            session.add(TelegramUpdate(update_id=uid, payload={}))
            await session.commit()
        await turn.run(
            sessionmaker,
            bot,
            Settings(),
            provider,
            clock=clock,
            chat_id=TEST_CHAT_ID,
            update_id=uid,
            user_text=f"сообщение {uid}",
        )

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.persona_active is False
    assert provider.calls == 3
    assert len(fake_session.sent) == 3

    await bot.session.close()


async def test_run_resume_sets_persona_active_true_and_does_not_restore_intensity(sessionmaker, clock):
    update_id = 108
    await _seed(sessionmaker, update_id=update_id, persona_active=False, intensity=2)
    bot, fake_session = _bot()

    await turn.run_resume(sessionmaker, bot, clock=clock, chat_id=TEST_CHAT_ID, update_id=update_id)

    assert fake_session.sent[0].text == turn.RESUME_REPLY_TEXT

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.persona_active is True
    assert state.intensity == 2  # not restored

    async with sessionmaker() as session:
        result = await session.execute(
            select(StateChange).where(StateChange.field == "persona_active")
        )
        rows = result.scalars().all()
    assert len(rows) == 1
    # /in is a typed command, never a pause word -- the audit log has to
    # say so, or it cannot answer "how did the persona come back on".
    assert rows[0].source == "command"
    assert rows[0].new_value == "True"

    await bot.session.close()


async def test_state_change_source_distinguishes_command_from_pause_word(sessionmaker, clock):
    """/out and a HARD pause word both switch the persona off, but the
    audit log must record which one did it (plan section 5's
    command|pause|system enum). Collapsing them onto one value throws
    away the only evidence that tells them apart.
    """
    await _seed(sessionmaker, update_id=1)
    bot, _ = _bot()

    # /out goes through the router's call site: an explicit command.
    await turn.run_hard_pause(
        sessionmaker, bot, clock=clock, chat_id=TEST_CHAT_ID, update_id=1, source="command"
    )

    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=2, payload={}))
        await session.commit()

    # A HARD pause word takes run()'s step-3 branch, which defaults to "pause".
    provider = FakeLLMProvider()
    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=TEST_CHAT_ID,
        update_id=2,
        user_text="пурпурный",
    )

    assert provider.calls == 0

    async with sessionmaker() as session:
        result = await session.execute(
            select(StateChange)
            .where(StateChange.field == "persona_active")
            .order_by(StateChange.id)
        )
        rows = result.scalars().all()

    assert [r.source for r in rows] == ["command", "pause"]

    await bot.session.close()


async def test_run_hard_pause_is_zero_call_and_reusable_outside_run(sessionmaker, clock):
    """run_hard_pause is the function /out calls directly, with no
    prior turn.run() involvement -- exercise it exactly that way."""
    update_id = 109
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()

    await turn.run_hard_pause(sessionmaker, bot, clock=clock, chat_id=TEST_CHAT_ID, update_id=update_id)

    assert fake_session.sent[0].text == turn.PAUSE_REPLY_TEXT
    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.persona_active is False

    await bot.session.close()


async def test_ordinary_chat_turns_never_send_tools_to_the_model():
    """Safety invariant 4: the OpenRouter request builder must never pass
    a `tools`/`tool_choice`/`functions` argument. There is no
    tools support anywhere in this codebase (LLMMessage/LLMProvider
    carry no such concept). This is a behavioural test rather than a
    source-grep: it stubs OpenRouterProvider's internal client, drives
    a real `complete()` call, and asserts on the kwargs the client
    actually received -- so it survives a rename and can't be fooled by
    a comment that merely mentions the word "tools"."""
    captured_kwargs: dict = {}

    class _FakeMessage:
        content = "ok"

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeUsage:
        prompt_tokens = 10
        completion_tokens = 5
        prompt_tokens_details = None
        cost = None

    class _FakeResponse:
        choices = [_FakeChoice()]
        usage = _FakeUsage()

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    provider = OpenRouterProvider(
        api_key="test-key",
        model="thedrummer/cydonia-24b-v4.1",
        max_tokens=700,
        temperature=0.9,
        data_collection="deny",
        web_search_max_results=5,
    )
    provider._client = _FakeClient()

    response = await provider.complete(
        [LLMMessage(role="user", content="hi")], conversation_id="anchor-main"
    )

    assert response.text == "ok"
    assert "tools" not in captured_kwargs
    assert "tool_choice" not in captured_kwargs
    assert "functions" not in captured_kwargs
    # 1f: an ordinary (non-/search) turn must never send the `web` plugin.
    assert "plugins" not in captured_kwargs["extra_body"]


async def test_only_run_resume_sets_persona_active_true():
    """Non-behavioural enforcement of safety invariant 2: run_resume
    must be the only place in the whole repo that flips persona_active
    back on. Grepping the source, not just testing behaviour, catches
    a future call site added anywhere else in app/."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    pattern = re.compile(r'"persona_active"\s*,\s*True')

    hits = []
    for path in app_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            if pattern.search(line):
                hits.append((path, lineno))

    assert len(hits) == 1, f"expected exactly one persona_active=True call site, found: {hits}"
    path, _ = hits[0]
    assert path.name == "turn.py"

    source = path.read_text(encoding="utf-8")
    run_resume_start = source.index("async def run_resume")
    run_start = source.index("\nasync def run(")
    (hit_path, hit_line) = hits[0]
    hit_offset = sum(len(line) + 1 for line in source.splitlines(keepends=False)[: hit_line - 1])
    assert run_resume_start < hit_offset < run_start
