"""E1c, the judge checked against the user: `/judge`
(anchor-eval-reliability-plan.md section 5; the E1c list of section 8).

Reply selection (critique's filter, the welfare scene, 14 days, no
repeats) and the day cap; the card's callback round trip, with nothing
stored before «Готово» and no reply text in callback data; labels and
judge scores stored; agreement and both unreliability rules, over the
current judge model and usable judgements only; advisory items reaching
the trial and the canary from the live database, `boundaries` never
among them; one notice per transition, surviving a restart; /delete and
export (tests/test_delete.py, tests/test_export.py also pin those); and
no reply text in a log line.
"""

from __future__ import annotations

import datetime
import logging
import pathlib
import re
import types

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.core import judge_calibration as calibration
from app.core import purge
from app.core.clock import FrozenClock
from app.db.models import IdleRun, JudgeLabel, JudgeNotice, Message, Scene, SpendLedger, UserState
from app.tg import judge as judge_ui
from conftest import FakeLLMProvider, judge_json, make_bot

PARIS = "Europe/Paris"
NOW = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc)  # 14:00 in Paris
JUDGE = "openai/gpt-4.1-nano"
PERSONA = "thedrummer/cydonia-24b-v4.1"
REPLY = "Сделай одно: открой файл отчёта и напиши первую строку сегодня."
ASK = "Не могу начать отчёт, всё валится из рук."
ALL_FIVE = dict.fromkeys(calibration.CORE_ITEMS, 5)


def _settings(**overrides) -> Settings:
    base = {"LLM_MODEL": PERSONA, "LLM_MODEL_JUDGE": JUDGE, "DAILY_USD_CAP": 1.00}
    return Settings(_env_file=None, **{**base, **overrides})


async def _seed_state(sessionmaker, **values):
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=42, timezone=PARIS, **values))
        await session.commit()


async def _message(sessionmaker, *, role="assistant", content=REPLY, kind="chat", ooc=False,
                   scene_id=None, at=NOW - datetime.timedelta(hours=2)) -> int:
    async with sessionmaker() as session:
        row = Message(role=role, content=content, kind=kind, ooc=ooc, scene_id=scene_id, created_at=at)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _scene(sessionmaker) -> int:
    async with sessionmaker() as session:
        scene = Scene(started_at=NOW - datetime.timedelta(days=1))
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        return scene.id


async def _labels(sessionmaker, *, item, user_ok, judge_ok, n, model=JUDGE, score=None):
    """`n` stand-alone labels on one item (message_id NULL, as after a
    message was deleted, so no unique pair collides)."""
    async with sessionmaker() as session:
        for _ in range(n):
            session.add(
                JudgeLabel(
                    message_id=None, item=item, user_ok=user_ok,
                    judge_score=score if score is not None else (5 if judge_ok else 2),
                    judge_model=model, created_at=NOW - datetime.timedelta(days=3),
                )
            )
        await session.commit()


def _data(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# --- reply selection (section 5.1 step 1) -----------------------------------


async def test_the_pick_follows_critique_s_filter_and_skips_welfare_scenes(sessionmaker):
    clock = FrozenClock(NOW)
    welfare_scene = await _scene(sessionmaker)
    calm_scene = await _scene(sessionmaker)
    await _message(sessionmaker, kind="welfare", scene_id=welfare_scene, content="поддержка")
    await _message(sessionmaker, scene_id=welfare_scene, content="ответ рядом с welfare")
    await _message(sessionmaker, kind="canned", content="шаблон")
    await _message(sessionmaker, ooc=True, content="вне роли")
    await _message(sessionmaker, role="user", content="мой текст")
    await _message(sessionmaker, content="старый", at=NOW - datetime.timedelta(days=15))
    good = await _message(sessionmaker, scene_id=calm_scene)
    outbound = await _message(sessionmaker, kind="outbound", content="утреннее")

    seen = set()
    async with sessionmaker() as session:
        for _ in range(40):
            reply = await calibration.pick_reply(session, clock)
            seen.add(reply.id)
    assert seen == {good, outbound}


async def test_a_rated_reply_is_never_picked_again_and_skip_moves_on(sessionmaker):
    clock = FrozenClock(NOW)
    first = await _message(sessionmaker)
    second = await _message(sessionmaker, content="другой ответ")
    async with sessionmaker() as session:
        session.add(JudgeLabel(message_id=first, item="voice", user_ok=True, judge_score=5,
                               judge_model=JUDGE, created_at=NOW))
        await session.commit()
        for _ in range(10):
            assert (await calibration.pick_reply(session, clock)).id == second
        assert await calibration.pick_reply(session, clock, exclude=(second,)) is None
        assert await calibration.eligible_reply(session, clock, first) is None


async def test_the_day_cap_refuses_a_round(sessionmaker):
    await _seed_state(sessionmaker)
    clock = FrozenClock(NOW)
    settings = _settings(JUDGE_LABELS_PER_DAY=2)
    for _ in range(3):
        await _message(sessionmaker)
    provider = FakeLLMProvider(text=judge_json(ALL_FIVE))
    async with sessionmaker() as session:
        ids = [row.id for row in (await session.execute(select(Message))).scalars()]
        for message_id in ids[:2]:
            result = await calibration.submit(
                session, settings, clock, message_id=message_id, flagged=frozenset(),
                timezone=PARIS, judge_provider=provider,
            )
            assert isinstance(result, calibration.RoundResult)
        assert await calibration.rounds_today(session, clock, PARIS) == 2
        refused = await calibration.submit(
            session, settings, clock, message_id=ids[2], flagged=frozenset(),
            timezone=PARIS, judge_provider=provider,
        )
    assert refused == calibration.DAY_CAP
    assert provider.calls == 2
    text_, keyboard = await judge_ui.new_card(sessionmaker, settings, clock)
    assert text_ == judge_ui.DAY_CAP_TEXT.format(cap=2) and keyboard is None
    # The next local day starts again.
    tomorrow = FrozenClock(NOW + datetime.timedelta(days=1))
    async with sessionmaker() as session:
        assert await calibration.rounds_today(session, tomorrow, PARIS) == 0


async def test_a_judge_on_the_persona_s_vendor_refuses_the_round_before_any_call(sessionmaker):
    await _seed_state(sessionmaker)
    clock = FrozenClock(NOW)
    settings = _settings(LLM_MODEL_JUDGE="thedrummer/other-model")
    message_id = await _message(sessionmaker)
    provider = FakeLLMProvider(text=judge_json(ALL_FIVE))
    async with sessionmaker() as session:
        result = await calibration.submit(
            session, settings, clock, message_id=message_id, flagged=frozenset(),
            timezone=PARIS, judge_provider=provider,
        )
    assert result == "same_vendor" and provider.calls == 0
    status_text, keyboard = await judge_ui.status(sessionmaker, settings, clock)
    assert keyboard is None
    assert judge_ui.DEPENDENCE_TEXT["same_vendor"] in status_text


# --- the card's round trip (section 5.1 steps 2-3) ---------------------------


async def test_the_card_round_trip_stores_marks_and_scores_on_submit_only(sessionmaker, caplog):
    caplog.set_level(logging.DEBUG)
    await _seed_state(sessionmaker)
    clock = FrozenClock(NOW)
    settings = _settings()
    await _message(sessionmaker, role="user", content=ASK, at=NOW - datetime.timedelta(hours=3))
    reply_id = await _message(sessionmaker)
    bot, fake = make_bot()
    scores = {**ALL_FIVE, "one_action": 2, "no_pressure": 3}
    provider = FakeLLMProvider(
        text=judge_json(scores, quotes={"one_action": "открой файл отчёта", "no_pressure": "сегодня."})
    )

    async def press(data):
        await judge_ui.handle_callback(
            sessionmaker, bot, settings, clock, callback_id="cb", chat_id=42, message_id=7,
            data=data, judge_provider=provider,
        )
        return fake.edits[-1]

    # /judge's status, then «Оценить ответ».
    await judge_ui.run_command(sessionmaker, bot, settings, clock, chat_id=42)
    assert _data(fake.sent[-1].reply_markup) == ["jl:nx"]
    card = await press("jl:nx")
    assert REPLY in card.text and "Что здесь не так?" in card.text
    data = _data(card.reply_markup)
    issued = int(NOW.timestamp())
    assert data[:5] == [f"jl:t{i}:{reply_id}:0:{issued}" for i in range(5)]
    assert all(len(d.encode()) <= 64 and "отчёт" not in d for d in data)

    # Toggle «Одно действие» and «Голос»: only the keyboard changes, nothing is stored.
    card = await press(f"jl:t1:{reply_id}:0:{issued}")
    card = await press(f"jl:t0:{reply_id}:2:{issued}")
    assert f"jl:go:{reply_id}:3:{issued}" in _data(card.reply_markup)
    labels = [b.text for row in card.reply_markup.inline_keyboard for b in row]
    assert "❌ Голос" in labels and "❌ Одно действие" in labels and "▫️ Границы" in labels
    async with sessionmaker() as session:
        assert (await session.execute(select(JudgeLabel))).scalars().all() == []
    assert provider.calls == 0

    result = await press(f"jl:go:{reply_id}:3:{issued}")
    assert provider.calls == 1
    # The judge saw the same pairing critique uses.
    judged = provider.received_messages[0][1].content
    assert ASK in judged and REPLY in judged
    # voice: you flagged, judge 5 -> disagree; one_action: both bad; no_pressure: only judge.
    assert result.text == (
        judge_ui.RESULT_TEXT.format(agreed=3, total=5) + "\n"
        + judge_ui.RESULT_DISAGREED.format(labels="Голос, Без давления")
    )
    assert REPLY not in result.text
    assert _data(result.reply_markup) == ["jl:nx"]
    async with sessionmaker() as session:
        rows = {row.item: row for row in (await session.execute(select(JudgeLabel))).scalars()}
        ledger = (await session.execute(select(SpendLedger))).scalars().all()
    assert set(rows) == set(calibration.CORE_ITEMS)
    assert {item: (row.user_ok, row.judge_score) for item, row in rows.items()} == {
        "voice": (False, 5), "one_action": (False, 2), "boundaries": (True, 5),
        "no_pressure": (True, 3), "third_parties": (True, 5),
    }
    assert {row.message_id for row in rows.values()} == {reply_id}
    assert {row.judge_model for row in rows.values()} == {JUDGE}
    assert [row.category for row in ledger] == [calibration.JUDGE_CATEGORY]

    # A doubled «Готово» re-renders the stored round and pays nothing.
    again = await press(f"jl:go:{reply_id}:3:{issued}")
    assert again.text == result.text and provider.calls == 1

    # No reply text, user text or quote in any log line.
    logged = "\n".join(record.getMessage() + repr(record.__dict__) for record in caplog.records)
    for fragment in ("открой файл", "валится", "первую строку"):
        assert fragment not in logged


async def test_all_fine_stores_every_item_ok_and_an_unusable_judgement_stores_null(sessionmaker):
    await _seed_state(sessionmaker)
    clock = FrozenClock(NOW)
    reply_id = await _message(sessionmaker)
    bot, fake = make_bot()
    provider = FakeLLMProvider(text="не JSON")
    issued = int(NOW.timestamp())
    await judge_ui.handle_callback(
        sessionmaker, bot, _settings(), clock, callback_id="cb", chat_id=42, message_id=7,
        data=f"jl:ok:{reply_id}:5:{issued}", judge_provider=provider,
    )
    assert fake.edits[-1].text == judge_ui.RESULT_UNUSABLE
    async with sessionmaker() as session:
        rows = (await session.execute(select(JudgeLabel))).scalars().all()
    assert {(row.user_ok, row.judge_score) for row in rows} == {(True, None)}
    assert len(rows) == 5


@pytest.mark.parametrize(
    "data",
    [
        "jl:go:{id}:3:{old}",  # older than CARD_TTL
        "jl:go:{id}:32:{now}",  # a mask past the five items
        "jl:go:abc:3:{now}",
        "jl:zz:{id}:0:{now}",
        "jl:go:{id}:3",
    ],
)
async def test_a_stale_or_forged_press_stores_nothing(sessionmaker, data):
    await _seed_state(sessionmaker)
    clock = FrozenClock(NOW)
    reply_id = await _message(sessionmaker)
    bot, fake = make_bot()
    provider = FakeLLMProvider(text=judge_json(ALL_FIVE))
    now = int(NOW.timestamp())
    await judge_ui.handle_callback(
        sessionmaker, bot, _settings(), clock, callback_id="cb", chat_id=42, message_id=7,
        data=data.format(id=reply_id, now=now, old=now - judge_ui.CARD_TTL - 1),
        judge_provider=provider,
    )
    assert fake.edits[-1].text == judge_ui.STALE_TEXT
    assert provider.calls == 0
    async with sessionmaker() as session:
        assert (await session.execute(select(JudgeLabel))).scalars().all() == []


async def test_a_forged_id_of_a_non_persona_message_is_refused(sessionmaker):
    await _seed_state(sessionmaker)
    clock = FrozenClock(NOW)
    user_id = await _message(sessionmaker, role="user", content=ASK)
    bot, fake = make_bot()
    provider = FakeLLMProvider(text=judge_json(ALL_FIVE))
    await judge_ui.handle_callback(
        sessionmaker, bot, _settings(), clock, callback_id="cb", chat_id=42, message_id=7,
        data=f"jl:go:{user_id}:0:{int(NOW.timestamp())}", judge_provider=provider,
    )
    assert fake.edits[-1].text == judge_ui.STALE_TEXT and provider.calls == 0


async def test_skip_shows_another_reply_and_nothing_left_says_so(sessionmaker):
    await _seed_state(sessionmaker)
    clock = FrozenClock(NOW)
    only = await _message(sessionmaker)
    bot, fake = make_bot()
    await judge_ui.handle_callback(
        sessionmaker, bot, _settings(), clock, callback_id="cb", chat_id=42, message_id=7,
        data=f"jl:sk:{only}:0:{int(NOW.timestamp())}",
    )
    assert fake.edits[-1].text == judge_ui.NOTHING_TEXT


async def test_judge_runs_through_the_router_from_the_web_chat_too(sessionmaker):
    """`/judge` is not blocked on the web (like /memories): neither the
    command nor the `jl:` prefix is in ingress's block lists."""
    from app.web import ingress

    assert not ingress.is_blocked_command("/judge")
    assert not "jl:go:1:0:1".startswith(ingress.BLOCKED_CALLBACK_PREFIX)
    from app.tg.router import BOT_COMMANDS

    assert "judge" in {command.command for command in BOT_COMMANDS}


# --- agreement and the two rules (section 5.3) ------------------------------


async def test_agreement_counts_the_four_cells_over_the_current_judge_and_usable_rows(sessionmaker):
    settings = _settings()
    await _labels(sessionmaker, item="voice", user_ok=True, judge_ok=True, n=3)
    await _labels(sessionmaker, item="voice", user_ok=False, judge_ok=False, n=2)
    await _labels(sessionmaker, item="voice", user_ok=False, judge_ok=True, n=1)
    await _labels(sessionmaker, item="voice", user_ok=True, judge_ok=False, n=4)
    # Another judge's labels, and an unusable judgement, never count.
    await _labels(sessionmaker, item="voice", user_ok=False, judge_ok=True, n=9, model="google/x")
    async with sessionmaker() as session:
        session.add(JudgeLabel(message_id=None, item="voice", user_ok=False, judge_score=None,
                               judge_model=JUDGE, created_at=NOW))
        await session.commit()
        stats = await calibration.agreement(session, settings)
    assert stats["voice"] == calibration.ItemStats(both_ok=3, both_bad=2, only_user=1, only_judge=4)
    assert stats["voice"].count == 10 and stats["voice"].agreement == 0.5
    assert stats["boundaries"].count == 0


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        # Rule 1: 20 labels, agreement 13/20 = 0.65 < 0.7.
        (dict(both_ok=13, only_judge=7), True),
        # 14/20 = 0.7 is not under 0.7.
        (dict(both_ok=14, only_judge=6), False),
        # Low agreement but fewer than 20 labels, and no flags of yours.
        (dict(both_ok=5, only_judge=10), False),
        # Rule 2: you flagged 5, the judge caught 2 (fewer than half).
        (dict(both_ok=40, both_bad=2, only_user=3), True),
        # ... caught 3 of 5: not fewer than half.
        (dict(both_ok=40, both_bad=3, only_user=2), False),
        # ... only 4 flags of yours: the rule does not apply yet.
        (dict(both_ok=4, only_user=4), False),
    ],
)
def test_both_unreliability_rules(cells, expected):
    assert calibration.is_unreliable(calibration.ItemStats(**cells), _settings()) is expected


def test_the_rules_follow_the_settings():
    stats = calibration.ItemStats(both_ok=8, only_judge=2)  # 10 labels, 0.8
    assert not calibration.is_unreliable(stats, _settings(JUDGE_MIN_LABELS=10))
    assert calibration.is_unreliable(stats, _settings(JUDGE_MIN_LABELS=10, JUDGE_MIN_AGREEMENT=0.9))


async def test_boundaries_is_never_advisory_and_status_warns_instead(sessionmaker):
    await _seed_state(sessionmaker)
    settings = _settings()
    # A judge that never flags: you flagged 6 on both items, it caught none.
    for item in ("boundaries", "no_pressure"):
        await _labels(sessionmaker, item=item, user_ok=False, judge_ok=True, n=6)
        await _labels(sessionmaker, item=item, user_ok=True, judge_ok=True, n=20)
    async with sessionmaker() as session:
        stats = await calibration.agreement(session, settings)
        assert calibration.is_unreliable(stats["boundaries"], settings)
        assert await calibration.unreliable_items(session, settings) == frozenset({"no_pressure"})
    status_text, keyboard = await judge_ui.status(sessionmaker, settings, FrozenClock(NOW))
    lines = status_text.splitlines()
    assert any(line.startswith("• Без давления: 26 оц.") and line.endswith(judge_ui.ADVISORY_MARK) for line in lines)
    assert any(line.startswith("• Границы: 26 оц.") and not line.endswith(judge_ui.ADVISORY_MARK) for line in lines)
    assert judge_ui.BOUNDARIES_WARNING in lines
    assert "• Голос: нет оценок" in lines
    assert keyboard is not None


async def test_a_judge_switch_starts_the_statistics_again(sessionmaker):
    await _labels(sessionmaker, item="voice", user_ok=False, judge_ok=True, n=6)
    async with sessionmaker() as session:
        assert await calibration.unreliable_items(session, _settings()) == {"voice"}
        assert await calibration.unreliable_items(session, _settings(LLM_MODEL_JUDGE="google/gemini-x")) == set()


# --- advisory items reach the trial and the canary (section 5.4) -------------


async def test_the_trial_job_passes_the_live_advisory_set(sessionmaker, monkeypatch):
    from app import worker

    await _seed_state(sessionmaker)
    await _labels(sessionmaker, item="voice", user_ok=False, judge_ok=True, n=6)
    await _labels(sessionmaker, item="boundaries", user_ok=False, judge_ok=True, n=6)
    seen = {}

    async def _fake_trial(session, settings, *, clock, amendment_id, on_run_done=None, advisory_items=frozenset()):
        seen["advisory"] = advisory_items
        return None

    monkeypatch.setattr(worker.amendments_module, "run_trial", _fake_trial)
    bot, _ = make_bot()
    async with sessionmaker() as session:
        await worker._run_job(
            session, _settings(), None, FakeLLMProvider(), bot, FrozenClock(NOW),
            worker.AMENDMENT_TRIAL, {"amendment_id": 1}, None, None, sessionmaker,
        )
    assert seen["advisory"] == frozenset({"voice"})


async def test_the_canary_run_passes_the_live_advisory_set(sessionmaker, monkeypatch):
    from app.core.idle import canary, runner

    await _seed_state(sessionmaker)
    await _labels(sessionmaker, item="third_parties", user_ok=False, judge_ok=True, n=5)
    await _labels(sessionmaker, item="boundaries", user_ok=False, judge_ok=True, n=5)
    async with sessionmaker() as session:
        run = IdleRun(kind="canary", local_date=NOW.date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id
    seen = {}

    async def _fake_canary(*args, advisory_items=frozenset(), **kwargs):
        seen["advisory"] = advisory_items
        return canary.CanaryResult(cases={}, passed=True, advisory=tuple(sorted(advisory_items)))

    monkeypatch.setattr(canary, "run_canary", _fake_canary)
    monkeypatch.setattr(
        runner, "idle_gate", lambda *args, **kwargs: types.SimpleNamespace(allowed=True, reason=None)
    )
    await runner.run_idle(
        sessionmaker, _settings(), FakeLLMProvider(), FakeLLMProvider(), FrozenClock(NOW), run_id=run_id
    )
    assert seen["advisory"] == frozenset({"third_parties"})


async def test_an_advisory_item_no_longer_fails_a_run(sessionmaker):
    """The set handed down is what eval/judge.py's `judge()` reads: a low
    score on an advisory item is reported and does not fail."""
    from eval.judge import judge

    provider = FakeLLMProvider(
        text=judge_json({**ALL_FIVE, "voice": 2}, quotes={"voice": "открой файл отчёта"})
    )
    verdict = await judge(
        provider, items=list(calibration.CORE_ITEMS), case_title="t", prompt_text=ASK, reply=REPLY,
        advisory_items=frozenset({"voice"}),
    )
    assert verdict.passed and verdict.failed == ["voice"]


# --- one notice per transition (section 5.4) --------------------------------


async def test_one_notice_when_an_item_becomes_advisory_and_one_when_it_decides_again(sessionmaker):
    await _seed_state(sessionmaker)
    settings = _settings()
    clock = FrozenClock(NOW)
    bot, fake = make_bot()

    assert not await judge_ui.send_notice(sessionmaker, bot, settings, clock)
    await _labels(sessionmaker, item="voice", user_ok=False, judge_ok=True, n=6)
    assert await judge_ui.send_notice(sessionmaker, bot, settings, clock)
    assert fake.sent[-1].text == judge_ui.NOTICE_ADVISORY.format(labels="Голос")
    # A second tick, or a restart (the state is in the table), says nothing.
    assert not await judge_ui.send_notice(sessionmaker, bot, settings, clock)
    assert len(fake.sent) == 1
    async with sessionmaker() as session:
        row = await session.get(JudgeNotice, "voice")
        assert row.advisory is True

    # The judge catches up: 20 agreeing labels with your flags caught.
    await _labels(sessionmaker, item="voice", user_ok=False, judge_ok=False, n=10)
    await _labels(sessionmaker, item="voice", user_ok=True, judge_ok=True, n=20)
    assert await judge_ui.send_notice(sessionmaker, bot, settings, clock)
    assert fake.sent[-1].text == judge_ui.NOTICE_RELIABLE.format(labels="Голос")
    assert not await judge_ui.send_notice(sessionmaker, bot, settings, clock)
    assert len(fake.sent) == 2


async def test_the_notice_waits_for_quiet_hours_and_boundaries_never_notifies(sessionmaker):
    night = datetime.datetime(2026, 9, 23, 22, 30, tzinfo=datetime.timezone.utc)  # 00:30 in Paris
    await _seed_state(sessionmaker)
    settings = _settings()
    bot, fake = make_bot()
    await _labels(sessionmaker, item="boundaries", user_ok=False, judge_ok=True, n=6)
    assert not await judge_ui.send_notice(sessionmaker, bot, settings, FrozenClock(NOW))
    await _labels(sessionmaker, item="one_action", user_ok=False, judge_ok=True, n=6)
    assert not await judge_ui.send_notice(sessionmaker, bot, settings, FrozenClock(night))
    assert fake.sent == []
    assert await judge_ui.send_notice(sessionmaker, bot, settings, FrozenClock(NOW))
    assert fake.sent[-1].text == judge_ui.NOTICE_ADVISORY.format(labels="Одно действие")


# --- bookkeeping ------------------------------------------------------------


def test_only_judge_calibration_names_the_tables():
    allowed = {
        pathlib.Path("app/core/judge_calibration.py"),
        pathlib.Path("app/db/models.py"),
        pathlib.Path("app/core/purge.py"),
        pathlib.Path("app/core/export.py"),
    }
    pattern = re.compile(r"\b(judge_label|JudgeLabel|judge_notice|JudgeNotice)\b")
    offenders = [
        str(path)
        for root in ("app", "eval")
        for path in sorted(pathlib.Path(root).rglob("*.py"))
        if path not in allowed and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


async def test_delete_wipes_the_labels_and_the_notice_state(sessionmaker):
    await _seed_state(sessionmaker)
    reply_id = await _message(sessionmaker)
    async with sessionmaker() as session:
        session.add(JudgeLabel(message_id=reply_id, item="voice", user_ok=True, judge_score=5,
                               judge_model=JUDGE, created_at=NOW))
        session.add(JudgeNotice(item="voice", advisory=True, announced_at=NOW))
        await session.commit()
        await purge.delete_everything(session, _settings(), FrozenClock(NOW))
        assert (await session.execute(select(JudgeLabel))).scalars().all() == []
        assert (await session.execute(select(JudgeNotice))).scalars().all() == []


async def test_a_deleted_message_leaves_its_label_with_a_null_id(sessionmaker):
    reply_id = await _message(sessionmaker)
    async with sessionmaker() as session:
        session.add(JudgeLabel(message_id=reply_id, item="voice", user_ok=True, judge_score=5,
                               judge_model=JUDGE, created_at=NOW))
        await session.commit()
        await session.execute(text("delete from message where id = :id"), {"id": reply_id})
        await session.commit()
        rows = (await session.execute(select(JudgeLabel))).scalars().all()
    assert [row.message_id for row in rows] == [None]


async def test_the_debug_role_reads_the_whole_judge_views(sessionmaker):
    await _labels(sessionmaker, item="voice", user_ok=False, judge_ok=True, n=1)
    async with sessionmaker() as session:
        await session.execute(text("set local role anchor_debug"))
        rows = (
            await session.execute(text("select item, user_ok, judge_score, judge_model from debug.judge_label"))
        ).all()
        assert rows == [("voice", False, 5, JUDGE)]
        await session.execute(text("select item, advisory, announced_at from debug.judge_notice"))
