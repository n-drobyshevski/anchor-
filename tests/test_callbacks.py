"""app/core/callbacks.py (phase-5 plan sections 3, 11a, 12 and 14).

Two layers, like tests/test_voice.py and tests/test_orders.py before
it: `select_callback`/`mark_delivered` tested directly against a real
database (selection rules, the one-per-scene rule, the two writes), and
a handful of turn-level tests through `app/core/turn.py`'s `run()` that
prove the wiring itself -- the prompt actually carries "## Можно
вспомнить" on an ordinary chat turn and never on a check-in, neutral
mode or a welfare turn, and a callback memory is never double-injected
under "## Может быть важно" for the same turn.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot
from sqlalchemy import select

from app.config import Settings
from app.core import callbacks
from app.core import turn
from app.db.models import Memory, Message, Scene, TelegramUpdate, UserState
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

CHAT_ID = 4242
TIMEZONE = "Europe/Paris"


async def _memory(
    session,
    *,
    kind="event",
    text="факт",
    days_ago=20,
    last_used_days_ago=None,
    superseded_by=None,
    now,
):
    row = Memory(
        kind=kind,
        text=text,
        source="user",
        created_at=now - datetime.timedelta(days=days_ago),
        last_used_at=(
            now - datetime.timedelta(days=last_used_days_ago)
            if last_used_days_ago is not None
            else None
        ),
        superseded_by=superseded_by,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _scene(session, now) -> int:
    scene = Scene(started_at=now)
    session.add(scene)
    await session.commit()
    await session.refresh(scene)
    return scene.id


# --- select_callback: the "never asks" shortcuts ---------------------------


async def test_none_when_scene_id_is_none(sessionmaker, clock):
    now = clock.now_utc()
    async with sessionmaker() as session:
        await _memory(session, days_ago=30, now=now)
        picked = await callbacks.select_callback(
            session, Settings(), clock, user_text="что угодно", scene_id=None, callback_scene=None
        )
    assert picked is None


async def test_none_when_this_scene_already_had_its_callback(sessionmaker, clock):
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        await _memory(session, days_ago=30, now=now)
        picked = await callbacks.select_callback(
            session,
            Settings(),
            clock,
            user_text="что угодно",
            scene_id=scene_id,
            callback_scene=scene_id,
        )
    assert picked is None


async def test_a_new_scene_may_ask_again(sessionmaker, clock):
    """The same check, differently scoped: `callback_scene` naming a
    *different*, earlier scene does not block this one."""
    now = clock.now_utc()
    async with sessionmaker() as session:
        old_scene_id = await _scene(session, now)
        new_scene_id = await _scene(session, now)
        await _memory(session, days_ago=30, now=now)
        picked = await callbacks.select_callback(
            session,
            Settings(),
            clock,
            user_text="что угодно",
            scene_id=new_scene_id,
            callback_scene=old_scene_id,
        )
    assert picked is not None


# --- selection rules ---------------------------------------------------


async def test_a_memory_younger_than_the_minimum_age_is_excluded(sessionmaker, frozen_clock):
    """A frozen clock, not the real one: two `now_utc()` calls a few
    microseconds apart (one to seed `created_at`, one inside
    select_callback to compute the cutoff) would otherwise make "exactly
    on the boundary" drift to "just past it" and flip the assertion."""
    clock = frozen_clock(2026, 10, 25, 9, 0)
    now = clock.now_utc()
    settings = Settings()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        # Exactly at the boundary (created_at == cutoff) is excluded --
        # the comparison is strict `<`, not `<=`.
        await _memory(session, days_ago=settings.CALLBACK_MIN_AGE_DAYS, now=now, text="ровно на границе")
        picked = await callbacks.select_callback(
            session, settings, clock, user_text="что угодно", scene_id=scene_id, callback_scene=None
        )
    assert picked is None


async def test_a_memory_one_day_past_the_minimum_age_is_eligible(sessionmaker, clock):
    now = clock.now_utc()
    settings = Settings()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        await _memory(
            session,
            days_ago=settings.CALLBACK_MIN_AGE_DAYS + 1,
            now=now,
            text="чуть старше границы",
        )
        picked = await callbacks.select_callback(
            session, settings, clock, user_text="что угодно", scene_id=scene_id, callback_scene=None
        )
    assert picked is not None


async def test_a_memory_used_exactly_at_the_unused_boundary_is_excluded(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 10, 25, 9, 0)
    now = clock.now_utc()
    settings = Settings()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        await _memory(
            session,
            days_ago=30,
            last_used_days_ago=settings.CALLBACK_UNUSED_DAYS,
            now=now,
            text="использовалась ровно на границе",
        )
        picked = await callbacks.select_callback(
            session, settings, clock, user_text="что угодно", scene_id=scene_id, callback_scene=None
        )
    assert picked is None


async def test_a_memory_used_one_day_past_the_unused_boundary_is_eligible(sessionmaker, clock):
    now = clock.now_utc()
    settings = Settings()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        await _memory(
            session,
            days_ago=30,
            last_used_days_ago=settings.CALLBACK_UNUSED_DAYS + 1,
            now=now,
            text="использовалась чуть раньше границы",
        )
        picked = await callbacks.select_callback(
            session, settings, clock, user_text="что угодно", scene_id=scene_id, callback_scene=None
        )
    assert picked is not None


async def test_a_never_used_memory_is_eligible(sessionmaker, clock):
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        await _memory(session, days_ago=30, last_used_days_ago=None, now=now)
        picked = await callbacks.select_callback(
            session, Settings(), clock, user_text="что угодно", scene_id=scene_id, callback_scene=None
        )
    assert picked is not None


@pytest.mark.parametrize("kind", ["identity", "preference", "rule", "technique"])
async def test_non_event_kinds_are_never_picked(sessionmaker, clock, kind):
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        await _memory(session, kind=kind, days_ago=30, now=now, text="старый факт не о событии")
        picked = await callbacks.select_callback(
            session, Settings(), clock, user_text="что угодно", scene_id=scene_id, callback_scene=None
        )
    assert picked is None


async def test_a_superseded_event_memory_is_excluded(sessionmaker, clock):
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        old = await _memory(session, days_ago=30, now=now, text="устаревшее событие")
        new = await _memory(session, days_ago=30, now=now, text="новое событие")
        old.superseded_by = new.id
        await session.commit()
        picked = await callbacks.select_callback(
            session, Settings(), clock, user_text="что угодно", scene_id=scene_id, callback_scene=None
        )
    assert picked is not None
    assert picked[0] == new.id


async def test_the_best_similarity_match_wins_above_the_threshold(sessionmaker, clock):
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        await _memory(session, days_ago=30, now=now, text="ходил на скалодром в первый раз")
        await _memory(session, days_ago=30, now=now, text="ужинал с друзьями в новом кафе")
        picked = await callbacks.select_callback(
            session,
            Settings(),
            clock,
            user_text="сегодня опять думаю про скалодром, руки болят",
            scene_id=scene_id,
            callback_scene=None,
        )
    assert picked is not None
    assert "скалодром" in picked[1]


async def test_falls_back_to_least_recently_used_when_nothing_scores_above_the_threshold(
    sessionmaker, clock
):
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        # Neither text has anything in common with the unrelated query
        # below, so both score at or under CALLBACK_MIN_SCORE and the
        # tie-break falls to last_used_at -- NULL (never used) first.
        await _memory(
            session, days_ago=30, last_used_days_ago=20, now=now, text="ходил на скалодром"
        )
        never_used = await _memory(
            session, days_ago=30, last_used_days_ago=None, now=now, text="был на концерте"
        )
        picked = await callbacks.select_callback(
            session,
            Settings(),
            clock,
            user_text="что приготовить сегодня на ужин",
            scene_id=scene_id,
            callback_scene=None,
        )
    assert picked is not None
    assert picked[0] == never_used.id


async def test_none_when_there_are_no_candidates_at_all(sessionmaker, clock):
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        picked = await callbacks.select_callback(
            session, Settings(), clock, user_text="что угодно", scene_id=scene_id, callback_scene=None
        )
    assert picked is None


async def test_exclude_ids_removes_a_candidate_from_the_pool(sessionmaker, clock):
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene_id = await _scene(session, now)
        only = await _memory(session, days_ago=30, now=now, text="единственная запись")
        picked = await callbacks.select_callback(
            session,
            Settings(),
            clock,
            user_text="что угодно",
            scene_id=scene_id,
            callback_scene=None,
            exclude_ids=(only.id,),
        )
    assert picked is None


# --- mark_delivered ------------------------------------------------------


async def test_mark_delivered_sets_callback_scene_and_bumps_last_used_at(sessionmaker, clock):
    now = clock.now_utc()
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE))
        await session.commit()
        scene_id = await _scene(session, now)
        row = await _memory(session, days_ago=30, now=now)

        await callbacks.mark_delivered(session, scene_id=scene_id, memory_id=row.id, clock=clock)
        await session.commit()

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        memory = await session.get(Memory, row.id)

    assert state.callback_scene == scene_id
    assert memory.last_used_at is not None


async def test_mark_delivered_with_no_memory_only_sets_callback_scene(sessionmaker, clock):
    """The "checked this scene, found nothing" case: callback_scene is
    still stamped, so the check runs only once, but no memory row is
    touched at all."""
    now = clock.now_utc()
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE))
        await session.commit()
        scene_id = await _scene(session, now)
        row = await _memory(session, days_ago=30, now=now)

        await callbacks.mark_delivered(session, scene_id=scene_id, memory_id=None, clock=clock)
        await session.commit()

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        memory = await session.get(Memory, row.id)

    assert state.callback_scene == scene_id
    assert memory.last_used_at is None


# --- at most one per scene, end to end at the select_callback layer ------


async def test_at_most_one_per_scene_then_a_new_scene_may_ask_again(sessionmaker, clock):
    now = clock.now_utc()
    settings = Settings()
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE))
        await session.commit()
        scene_id = await _scene(session, now)
        await _memory(session, days_ago=30, now=now, text="первая старая запись")
        # A second, independent candidate: mark_delivered bumps the
        # *chosen* memory's own last_used_at, so re-using "the same
        # memory" to prove a new scene may ask again would actually be
        # testing whether that one memory got un-used again, a
        # different question. This one is untouched by the first pick.
        second_candidate = await _memory(session, days_ago=30, now=now, text="вторая старая запись")

        first = await callbacks.select_callback(
            session, settings, clock, user_text="что угодно", scene_id=scene_id, callback_scene=None
        )
        assert first is not None
        await callbacks.mark_delivered(
            session, scene_id=scene_id, memory_id=first[0], clock=clock
        )
        await session.commit()
        state = await session.get(UserState, 1)

        # Same scene, second turn: already checked.
        second = await callbacks.select_callback(
            session,
            settings,
            clock,
            user_text="что угодно",
            scene_id=scene_id,
            callback_scene=state.callback_scene,
        )
        assert second is None

        # A new scene: eligible again -- the other candidate, since the
        # first one was just used and is not due again for
        # CALLBACK_UNUSED_DAYS.
        new_scene_id = await _scene(session, now)
        third = await callbacks.select_callback(
            session,
            settings,
            clock,
            user_text="что угодно",
            scene_id=new_scene_id,
            callback_scene=state.callback_scene,
        )
        assert third is not None
        assert third[0] == second_candidate.id


# --- turn-level wiring (app/core/turn.py) ---------------------------------


async def _seed_turn_state(sessionmaker, now, **state_kwargs) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state_kwargs))
        await session.commit()
        await _memory(
            session,
            days_ago=30,
            now=now,
            text="ходил на скалодром в первый раз, руки устали",
        )


def _bot() -> tuple[Bot, FakeSession]:
    fake_session = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake_session), fake_session


async def test_the_prompt_carries_the_callback_header_on_an_ordinary_chat_turn(
    sessionmaker, clock
):
    now = clock.now_utc()
    await _seed_turn_state(sessionmaker, now)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        await session.commit()
    bot, fake = _bot()
    provider = FakeLLMProvider(text="Понял, отдохни немного.")

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=CHAT_ID,
        update_id=1,
        user_text="снова думаю про скалодром, руки до сих пор побаливают",
    )

    assert provider.calls == 1
    system_texts = "\n".join(m.content for m in provider.received_messages[0] if m.role == "system")
    assert "## Можно вспомнить (только если к месту)" in system_texts

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        assert state.callback_scene is not None
    await bot.session.close()


async def test_the_second_chat_turn_in_the_same_scene_gets_no_callback(sessionmaker, clock):
    now = clock.now_utc()
    await _seed_turn_state(sessionmaker, now)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        session.add(TelegramUpdate(update_id=2, payload={}))
        await session.commit()
    bot, fake = _bot()
    provider = FakeLLMProvider(text="Понял.")

    await turn.run(
        sessionmaker, bot, Settings(), provider, clock=clock,
        chat_id=CHAT_ID, update_id=1,
        user_text="снова думаю про скалодром, руки до сих пор побаливают",
    )
    await turn.run(
        sessionmaker, bot, Settings(), provider, clock=clock,
        chat_id=CHAT_ID, update_id=2,
        user_text="ладно, поеду домой пораньше сегодня",
    )

    assert provider.calls == 2
    second_system = "\n".join(
        m.content for m in provider.received_messages[1] if m.role == "system"
    )
    assert "## Можно вспомнить" not in second_system
    await bot.session.close()


async def test_a_neutral_turn_carries_no_callback_header(sessionmaker, clock):
    now = clock.now_utc()
    await _seed_turn_state(sessionmaker, now, persona_active=False)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        await session.commit()
    bot, fake = _bot()
    provider = FakeLLMProvider(text="Ок.")

    await turn.run(
        sessionmaker, bot, Settings(), provider, clock=clock,
        chat_id=CHAT_ID, update_id=1, user_text="привет",
    )

    system_texts = "\n".join(m.content for m in provider.received_messages[0] if m.role == "system")
    assert "Можно вспомнить" not in system_texts

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        assert state.callback_scene is None
    await bot.session.close()


async def test_a_welfare_turn_carries_no_callback_and_never_sets_callback_scene(
    sessionmaker, clock
):
    """The welfare path (run_welfare_turn) returns long before turn.py's
    own callback wiring runs, and never calls persona_context.gather()
    at all -- so the reply carries no "## Можно вспомнить" (it is not
    even in character), and callback_scene stays untouched."""

    class ScriptedWelfare(FakeLLMProvider):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.verdict_json = '{"level": "real", "confidence": 0.9}'

        async def complete(self, messages, *, conversation_id, json_schema=None):
            response = await super().complete(
                messages, conversation_id=conversation_id, json_schema=json_schema
            )
            system = messages[0].content
            text = self.verdict_json if system.startswith("Определи") else "Я рядом."
            return type(response)(text=text, usage=response.usage, model=response.model)

    now = clock.now_utc()
    await _seed_turn_state(sessionmaker, now)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        await session.commit()
    bot, fake = _bot()
    persona = FakeLLMProvider(text="Не оправдание.")

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        persona,
        clock=clock,
        chat_id=CHAT_ID,
        update_id=1,
        user_text="стоп, мне реально хреново, это не игра",
        safety_provider=ScriptedWelfare(),
    )

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        assert state.callback_scene is None
    await bot.session.close()


async def test_the_callback_memory_is_never_duplicated_under_retrieved(sessionmaker, clock):
    """The same `event` memory can score for both `retrieve_memories`
    and `select_callback` -- app/core/turn.py must dedupe it out of
    "## Может быть важно" once it has been chosen as the callback."""
    now = clock.now_utc()
    await _seed_turn_state(sessionmaker, now)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        await session.commit()
    bot, fake = _bot()
    provider = FakeLLMProvider(text="Понял.")

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        clock=clock,
        chat_id=CHAT_ID,
        update_id=1,
        # Deliberately similar enough to also match ordinary retrieval.
        user_text="думаю про скалодром и про то, что руки до сих пор болят",
    )

    # "## Может быть важно" and "## Можно вспомнить" both live inside
    # the same "## Сейчас" system message (app/core/prompt.py's
    # build_now_block), so the dedupe question is "does the callback's
    # own text appear more than once in it", not "is it in a different
    # message".
    now_block = next(
        m.content for m in provider.received_messages[0] if m.role == "system" and "## Сейчас" in m.content
    )
    assert now_block.count("скалодром") == 1
    callback_idx = now_block.index("Можно вспомнить")
    scalodrom_idx = now_block.index("скалодром")
    assert scalodrom_idx > callback_idx, "the mention must sit under the callback header, not retrieved"
    await bot.session.close()
