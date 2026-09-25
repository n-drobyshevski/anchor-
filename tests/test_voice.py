"""app/core/voice.py (phase-5 plan section 5).

`load_lines`, `voice_anchors`, `choose_nickname` and `directive` are
pure and get unit tests with no database. `remember_nickname` and the
turn-level rules (no directive outside persona mode, nickname_last
untouched by a welfare or neutral turn) need the real turn machinery,
so they live at the bottom against `sessionmaker`/`clock`.
"""

from __future__ import annotations

import random

import pytest
from aiogram import Bot
from sqlalchemy import select

from app.config import Settings
from app.core.voice import choose_nickname, directive, load_lines, remember_nickname, voice_anchors
from app.db.models import TelegramUpdate, UserState
from conftest import FakeLLMProvider, FakeSession

CHAT_ID = 4242
TIMEZONE = "Europe/Paris"


# --- load_lines ------------------------------------------------------------


def test_load_lines_strips_and_skips_blanks_and_comments(tmp_path):
    path = tmp_path / "lines.txt"
    path.write_text(
        "  первая строка  \n"
        "\n"
        "<!-- заметка для редактора -->\n"
        "# ещё одна заметка\n"
        "вторая строка\n"
        "   \n",
        encoding="utf-8",
    )
    assert load_lines(path) == ["первая строка", "вторая строка"]


def test_load_lines_on_an_effectively_empty_file_is_an_empty_list(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("\n<!-- only comments -->\n# also a comment\n", encoding="utf-8")
    assert load_lines(path) == []


def test_load_lines_on_a_genuinely_empty_file_is_an_empty_list(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    assert load_lines(path) == []


# --- voice_anchors -----------------------------------------------------------

_LINES = [f"строка {i}" for i in range(10)]


def test_voice_anchors_is_deterministic_for_the_same_seed():
    first = voice_anchors(_LINES, seed=42, k=4)
    second = voice_anchors(_LINES, seed=42, k=4)
    assert first == second


def test_voice_anchors_differs_across_scenes():
    seeds_seen = {tuple(voice_anchors(_LINES, seed=s, k=3)) for s in range(20)}
    assert len(seeds_seen) > 1, "20 different scene seeds drew the same 3 lines every time"


def test_voice_anchors_is_capped_at_the_files_length():
    result = voice_anchors(_LINES, seed=1, k=1000)
    assert len(result) == len(_LINES)
    assert set(result) == set(_LINES)


def test_voice_anchors_is_returned_in_file_order():
    result = voice_anchors(_LINES, seed=7, k=5)
    assert result == sorted(result, key=_LINES.index)


def test_voice_anchors_on_an_empty_file_is_empty():
    assert voice_anchors([], seed=1, k=4) == []


def test_voice_anchors_with_k_zero_is_empty():
    assert voice_anchors(_LINES, seed=1, k=0) == []


# --- choose_nickname -----------------------------------------------------------

NICKS = ["напарник", "капитан", "шеф"]


def test_choose_nickname_never_repeats_last():
    rng = random.Random(0)
    for _ in range(500):
        picked = choose_nickname(NICKS, "напарник", rate=1.0, rng=rng)
        assert picked != "напарник"


def test_choose_nickname_is_none_when_the_list_is_empty():
    rng = random.Random(0)
    assert choose_nickname([], None, rate=1.0, rng=rng) is None


def test_choose_nickname_is_none_when_the_only_candidate_equals_last():
    rng = random.Random(0)
    assert choose_nickname(["напарник"], "напарник", rate=1.0, rng=rng) is None


def test_choose_nickname_rate_zero_never_picks():
    rng = random.Random(0)
    for _ in range(200):
        assert choose_nickname(NICKS, None, rate=0.0, rng=rng) is None


def test_choose_nickname_rate_one_always_picks_when_a_candidate_exists():
    rng = random.Random(0)
    for _ in range(200):
        assert choose_nickname(NICKS, None, rate=1.0, rng=rng) is not None


def test_choose_nickname_rate_is_about_half_over_a_seeded_run():
    rng = random.Random(12345)
    draws = 2000
    picked = sum(
        1 for _ in range(draws) if choose_nickname(NICKS, None, rate=0.5, rng=rng) is not None
    )
    share = picked / draws
    assert 0.45 < share < 0.55


# --- directive -----------------------------------------------------------------


def test_directive_with_a_nickname():
    assert directive("напарник") == "Обращение в этом ответе: напарник"


def test_directive_with_none():
    assert directive(None) == "Без обращения в этом ответе."


# --- remember_nickname (DB) -----------------------------------------------------


async def test_remember_nickname_writes_the_column(sessionmaker):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE))
        await session.commit()

    async with sessionmaker() as session:
        await remember_nickname(session, "напарник")
        await session.commit()

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        assert state.nickname_last == "напарник"


async def test_remember_nickname_overwrites_a_previous_value(sessionmaker):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, nickname_last="шеф"))
        await session.commit()

    async with sessionmaker() as session:
        await remember_nickname(session, "капитан")
        await session.commit()

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        assert state.nickname_last == "капитан"


# --- turn-level: which modes see a directive, and who writes nickname_last -----


class _AlwaysPickFirst(random.Random):
    """A rigged rng: always "yes, address them", always the first candidate."""

    def random(self) -> float:  # noqa: D102
        return 0.0

    def choice(self, seq):  # noqa: D102
        return seq[0]


async def _seed(sessionmaker, *update_ids: int, **state_overrides):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state_overrides))
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


async def test_neutral_mode_turn_carries_no_directive(sessionmaker, clock, monkeypatch):
    from app.core import turn

    monkeypatch.setattr(turn, "NICKNAME_RNG", _AlwaysPickFirst())
    bot, fake = await _seed(sessionmaker, 1, persona_active=False)
    main = FakeLLMProvider(text="Ок.")

    await turn.run(
        sessionmaker, bot, Settings(DAILY_USD_CAP=10.0), main,
        clock=clock, chat_id=CHAT_ID, update_id=1, user_text="привет",
    )

    prompt_text = "\n".join(m.content for m in main.received_messages[0])
    assert "Обращение в этом ответе" not in prompt_text
    assert "Без обращения в этом ответе" not in prompt_text

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.nickname_last is None


async def test_a_welfare_turn_leaves_nickname_last_untouched(sessionmaker, clock, monkeypatch):
    from app.core import turn

    monkeypatch.setattr(turn, "NICKNAME_RNG", _AlwaysPickFirst())
    bot, fake = await _seed(sessionmaker, 1, nickname_last="шеф")

    class _RealVerdict(FakeLLMProvider):
        async def complete(self, messages, *, conversation_id, json_schema=None):
            response = await super().complete(
                messages, conversation_id=conversation_id, json_schema=json_schema
            )
            if messages[0].content.startswith("Определи"):
                return type(response)(
                    text='{"level": "real", "confidence": 0.9}',
                    usage=response.usage,
                    model=response.model,
                )
            return type(response)(text="Я рядом.", usage=response.usage, model=response.model)

    safety = _RealVerdict()
    main = FakeLLMProvider(text="Не оправдание.")

    await turn.run(
        sessionmaker, bot, Settings(DAILY_USD_CAP=10.0), main,
        clock=clock, chat_id=CHAT_ID, update_id=1,
        user_text="стоп, мне реально плохо", safety_provider=safety,
    )

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.nickname_last == "шеф", "welfare must never write a nickname"


async def test_a_normal_persona_turn_updates_nickname_last_when_one_is_chosen(
    sessionmaker, clock, monkeypatch
):
    from app.core import turn

    monkeypatch.setattr(turn, "NICKNAME_RNG", _AlwaysPickFirst())
    bot, fake = await _seed(sessionmaker, 1)
    main = FakeLLMProvider(text="Добей и напиши.")

    await turn.run(
        sessionmaker, bot, Settings(DAILY_USD_CAP=10.0), main,
        clock=clock, chat_id=CHAT_ID, update_id=1, user_text="осталось два абзаца",
    )

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.nickname_last is not None

    prompt_text = "\n".join(m.content for m in main.received_messages[0])
    assert "Обращение в этом ответе" in prompt_text
