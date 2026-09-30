"""The lens garden's Telegram delivery (L3 spec section 2, with the
owner's amendment (b): one message per run) and its `lg:` buttons.

- `send_pending`: each hold (the flag, a pause, /quiet, quiet hours, the
  welfare cooldown), one message per run with its header and numbered
  gaps, the mark after the send, an empty run marked without a message,
  a reopened gap carried into the new run's message with «снова»;
- the callback, through the real router: «сделал» and «не нужно» edit
  the same message, the item gains its mark and loses its row, the
  keyboard goes with the last row; a stale epoch, a replay, `lg:r:`, a
  malformed press and a button on a message the gap has left are all
  «Устарело»;
- the web chat is refused twice (router and ingress);
- the worker's hook sends after a vault pass, and its failure never
  fails the pass;
- Telegram's 4096 characters;
- logs carry no title or detail.

All notes are synthetic.
"""

from __future__ import annotations

import datetime
import hashlib
import logging

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import Job, UserState, VaultFile
from app.tg import garden
from app.tg.router import WEB_ONLY_REPLY, build_router
from app.vault import lens
from app.web import ingress
from conftest import FakeLLMProvider, FakeSession

CHAT_ID = 555
TIMEZONE = "Europe/Paris"
EPOCH = "k3f7qa"
# 14:00 in Paris: outside the default quiet hours (22:30-08:00).
NOW = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc)
DETAIL = "Обе заметки говорят о необходимом разнообразии, но не ссылаются друг на друга."
SECRET_TITLE = "Синтетическая заметка Зета"
SECRET_DETAIL = "Синтетическая деталь, которой не место в логах."


def _clock(moment: datetime.datetime = NOW) -> FrozenClock:
    return FrozenClock(moment)


def _settings(**kwargs) -> Settings:
    kwargs.setdefault("LENS_GARDEN_ENABLED", True)
    return Settings(**kwargs)


def _sig(*parts: str) -> str:
    return hashlib.sha256("|".join(("v1",) + parts).encode("utf-8")).hexdigest()


def _gap(titles=("Ashby", "Beer"), *, kind="link", title=None, detail=DETAIL) -> lens.NewGap:
    return lens.NewGap(
        kind=kind,
        note_ids=tuple(range(1, len(titles) + 1)),
        titles=tuple(titles),
        title=title,
        detail=detail,
        signature=_sig(kind, *(titles if title is None else (title,))),
        recheck={"titles": list(titles)},
    )


async def _seed(sessionmaker, **state_kwargs) -> None:
    async with sessionmaker() as session:
        session.add(
            UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, vault_epoch=EPOCH, **state_kwargs)
        )
        await session.commit()


async def _record(sessionmaker, week="2026-W40", *, new=(), reopened=()) -> lens.GardenRecord:
    async with sessionmaker() as session:
        record = await lens.record_garden(
            session,
            idle_run_id=None,
            iso_week=week,
            version_id=None,
            findings={},
            resolved_ids=(),
            reopened_ids=reopened,
            new=list(new),
            now=NOW,
        )
        await session.commit()
        return record


def _bot() -> tuple[Bot, FakeSession]:
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


def _dispatcher(sessionmaker, settings=None) -> tuple[Dispatcher, Bot, FakeSession]:
    bot, fake = _bot()
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, settings or _settings(), FakeLLMProvider(), clock=_clock())
    )
    return dp, bot, fake


def _press(bot: Bot, update_id: int, data: str, message_id: int) -> Update:
    return Update.model_validate(
        {
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
                    "text": "…",
                },
            },
        },
        context={"bot": bot},
    )


def _rows(markup) -> list[list[tuple[str, str]]]:
    if markup is None:
        return []
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


async def _status(sessionmaker, gap_id: int) -> str:
    async with sessionmaker() as session:
        return next(g.status for g in await lens.known_gaps(session) if g.id == gap_id)


# --- sending: one message per run ----------------------------------------------------


async def test_one_message_per_run_with_header_items_and_one_row_per_gap(sessionmaker):
    await _seed(sessionmaker)
    record = await _record(
        sessionmaker,
        new=[
            _gap(("Ashby", "Beer")),
            _gap(("Ashby", "Wiener"), kind="missing_note", title="Гомеостат"),
        ],
    )
    a, b = record.new_ids
    bot, fake = _bot()

    assert await garden.send_pending(sessionmaker, bot, _settings(), _clock())

    assert len(fake.sent) == 1
    text = fake.sent[0].text
    lines = text.splitlines()
    assert lines[0] == "Сад линзы, неделя 2026-W40."
    assert lines[1] == "Новых: 2 · снова: 0 · открыто с прошлых недель: 0"
    assert "1. Связь: «Ashby» — «Beer»" in lines
    assert "2. Нет заметки: «Гомеостат» — упоминают «Ashby», «Wiener»" in lines
    assert text.count(DETAIL) == 2
    assert "снова" not in text.split("\n", 2)[2]
    assert _rows(fake.sent[0].reply_markup) == [
        [("1 · сделал", f"lg:d:{a}:{EPOCH}"), ("1 · не нужно", f"lg:n:{a}:{EPOCH}")],
        [("2 · сделал", f"lg:d:{b}:{EPOCH}"), ("2 · не нужно", f"lg:n:{b}:{EPOCH}")],
    ]


async def test_the_run_is_marked_after_the_send_and_never_sent_twice(sessionmaker):
    await _seed(sessionmaker)
    record = await _record(sessionmaker, new=[_gap()])
    bot, fake = _bot()

    assert await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    message_id = 1  # FakeSession's first message id
    async with sessionmaker() as session:
        assert await lens.unsent_run(session) is None
        assert await lens.message_run_id(session, message_id) == record.run_id
        state = await lens.run_message_state(session, record.run_id)
    assert [g.id for g in state.gaps] == list(record.new_ids)
    assert all(g.actionable for g in state.gaps)

    assert not await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    assert len(fake.sent) == 1


async def test_two_runs_recorded_before_the_first_send_share_one_message(sessionmaker):
    """A run held past the next one (a /quiet renewed for a week, say):
    the next run's message lists both runs' gaps, each with a live row."""
    await _seed(sessionmaker)
    earlier = await _record(sessionmaker, "2026-W40", new=[_gap()])
    later = await _record(sessionmaker, "2026-W41", new=[_gap(("Ashby", "Wiener"), kind="tension")])
    bot, fake = _bot()
    assert await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    assert not await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    assert len(fake.sent) == 1
    a, b = earlier.new_ids[0], later.new_ids[0]
    assert _rows(fake.sent[0].reply_markup) == [
        [("1 · сделал", f"lg:d:{a}:{EPOCH}"), ("1 · не нужно", f"lg:n:{a}:{EPOCH}")],
        [("2 · сделал", f"lg:d:{b}:{EPOCH}"), ("2 · не нужно", f"lg:n:{b}:{EPOCH}")],
    ]
    async with sessionmaker() as session:
        assert await lens.decide_gap(session, a, EPOCH, "done", NOW, message_id=1) == "ok"


async def test_the_header_names_the_report_once_written(sessionmaker):
    await _seed(sessionmaker)
    await _record(sessionmaker, new=[_gap()])
    path = lens.report_path("2026-W40", EPOCH)
    async with sessionmaker() as session:
        # As a confirmed create leaves it (`_written` sets the digest).
        session.add(VaultFile(path=path, role="report", disk_sha256="0" * 64))
        await session.commit()
    bot, fake = _bot()
    await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    assert fake.sent[0].text.splitlines()[2] == f"Заметка: {path}"


async def test_a_run_with_nothing_to_show_is_marked_without_a_message(sessionmaker):
    await _seed(sessionmaker)
    await _record(sessionmaker, new=[])
    bot, fake = _bot()
    assert not await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    assert fake.sent == []
    async with sessionmaker() as session:
        assert await lens.unsent_run(session) is None


async def test_nothing_to_send_before_the_first_run(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = _bot()
    assert not await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    assert fake.sent == []


# --- holds -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "settings,state,moment",
    [
        pytest.param(Settings(), {}, NOW, id="flag-off"),
        pytest.param(_settings(), {"persona_active": False}, NOW, id="paused"),
        pytest.param(
            _settings(), {"quiet_until": NOW + datetime.timedelta(hours=2)}, NOW, id="quiet-cmd"
        ),
        pytest.param(
            _settings(),
            {},
            datetime.datetime(2026, 9, 30, 21, 30, tzinfo=datetime.timezone.utc),
            id="quiet-hours",
        ),
        pytest.param(
            _settings(), {"welfare_at": NOW - datetime.timedelta(hours=23)}, NOW, id="welfare"
        ),
    ],
)
async def test_each_hold_sends_nothing_and_keeps_the_run_unsent(sessionmaker, settings, state, moment):
    await _seed(sessionmaker, **state)
    await _record(sessionmaker, new=[_gap()])
    bot, fake = _bot()
    assert not await garden.send_pending(sessionmaker, bot, settings, _clock(moment))
    assert fake.sent == []
    async with sessionmaker() as session:
        assert await lens.unsent_run(session) is not None


async def test_the_welfare_hold_ends_with_its_cooldown(sessionmaker):
    await _seed(sessionmaker, welfare_at=NOW - datetime.timedelta(hours=25))
    await _record(sessionmaker, new=[_gap()])
    bot, fake = _bot()
    assert await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    assert len(fake.sent) == 1


async def test_a_held_run_goes_out_on_a_later_pass(sessionmaker):
    await _seed(sessionmaker)
    await _record(sessionmaker, new=[_gap()])
    bot, fake = _bot()
    night = _clock(datetime.datetime(2026, 9, 30, 23, 0, tzinfo=datetime.timezone.utc))
    assert not await garden.send_pending(sessionmaker, bot, _settings(), night)
    assert await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    assert len(fake.sent) == 1


# --- the lg: callback --------------------------------------------------------------------


async def _sent(sessionmaker, n: int = 2) -> tuple[list[int], int]:
    """A run with `n` link gaps, sent; its gap ids and message id."""
    titles = [("Ashby", "Beer"), ("Beer", "Wiener"), ("Wiener", "Ashby")][:n]
    record = await _record(sessionmaker, new=[_gap(t) for t in titles])
    bot, fake = _bot()
    assert await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    return list(record.new_ids), 1


async def test_done_marks_the_item_and_drops_its_row_in_the_same_message(sessionmaker):
    await _seed(sessionmaker)
    (a, b), message_id = await _sent(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)

    await dp.feed_update(bot, _press(bot, 1, f"lg:d:{a}:{EPOCH}", message_id))

    assert fake.answered[-1].text == "Отмечено: сделал."
    assert fake.sent == []  # an edit, never a new message
    edit = fake.edits[-1]
    assert edit.message_id == message_id
    lines = edit.text.splitlines()
    first = lines.index("1. Связь: «Ashby» — «Beer»")
    assert lines[first + 2] == "— отмечено: сделал (проверю в следующем саду)"
    assert "— отмечено" not in "\n".join(lines[first + 3 :])
    assert _rows(edit.reply_markup) == [
        [("2 · сделал", f"lg:d:{b}:{EPOCH}"), ("2 · не нужно", f"lg:n:{b}:{EPOCH}")]
    ]
    assert await _status(sessionmaker, a) == "done"


async def test_the_last_row_takes_the_keyboard_with_it(sessionmaker):
    await _seed(sessionmaker)
    (a, b), message_id = await _sent(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)

    await dp.feed_update(bot, _press(bot, 1, f"lg:n:{a}:{EPOCH}", message_id))
    await dp.feed_update(bot, _press(bot, 2, f"lg:n:{b}:{EPOCH}", message_id))

    assert fake.answered[-1].text == "Отмечено: не нужно."
    edit = fake.edits[-1]
    assert edit.message_id == message_id
    assert edit.reply_markup is None
    assert edit.text.count("— отмечено: не нужно") == 2
    assert await _status(sessionmaker, a) == "dismissed"
    assert await _status(sessionmaker, b) == "dismissed"


async def test_a_replay_is_stale_and_changes_nothing(sessionmaker):
    await _seed(sessionmaker)
    (a, _b), message_id = await _sent(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:d:{a}:{EPOCH}", message_id))
    await dp.feed_update(bot, _press(bot, 2, f"lg:n:{a}:{EPOCH}", message_id))
    assert fake.answered[-1].text == "Устарело"
    assert await _status(sessionmaker, a) == "done"
    # The re-render is the same text and keyboard: the message says the truth.
    assert fake.edits[-1].text == fake.edits[0].text


async def test_a_stale_epoch_is_stale(sessionmaker):
    await _seed(sessionmaker)
    (a, _b), message_id = await _sent(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:d:{a}:abcdef", message_id))
    assert fake.answered[-1].text == "Устарело"
    assert await _status(sessionmaker, a) == "open"


async def test_research_is_reserved_and_stale(sessionmaker):
    """«Исследовать» waits for L4: `lg:r:` must never become paid research."""
    await _seed(sessionmaker)
    (a, _b), message_id = await _sent(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:r:{a}:{EPOCH}", message_id))
    assert fake.answered[-1].text == "Устарело"
    assert fake.edits == []
    assert await _status(sessionmaker, a) == "open"


@pytest.mark.parametrize(
    "data",
    [
        "lg:",
        "lg:d",
        "lg:d:1",
        "lg:d:1:k3f7q",
        "lg:d:1:k3f7qa:x",
        "lg:d:1:K3F7QA",
        "lg:d:1:k3f9qa",  # 9 is not base32
        "lg:x:1:k3f7qa",
        "lg:r:1:k3f7qa",
        "lg:d:-1:k3f7qa",
        "lg:d:0:k3f7qa",
        "lg:d:١:k3f7qa",
        "lg:d: 1:k3f7qa",
        "lg:d:1:k3f7qa\n",
        "lg:d:99999999999:k3f7qa",
        "v:y:1:k3f7qa",
        None,
    ],
)
def test_the_callback_grammar(data):
    assert garden.parse_callback(data) is None


def test_the_callback_grammar_accepts_both_actions():
    assert garden.parse_callback("lg:d:12:k3f7qa") == ("done", 12, "k3f7qa")
    assert garden.parse_callback("lg:n:12:k3f7qa") == ("dismissed", 12, "k3f7qa")


async def test_a_reopened_gap_moves_to_the_new_run_s_message(sessionmaker):
    """Spec section 7: a done gap the recheck finds undone is reopened into
    the next run and shown again with «снова»; its button on the old
    message is stale, and the old message says where it went."""
    await _seed(sessionmaker)
    (a, b), first_message = await _sent(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:d:{a}:{EPOCH}", first_message))

    record = await _record(sessionmaker, "2026-W41", new=[_gap(("Wiener", "Ashby"))], reopened=[a])
    (c,) = record.new_ids
    send_bot, send_fake = _bot()
    send_fake._next_message_id = 50
    assert await garden.send_pending(sessionmaker, send_bot, _settings(), _clock())
    text = send_fake.sent[0].text
    assert text.splitlines()[1] == "Новых: 1 · снова: 1 · открыто с прошлых недель: 1"
    assert "1. Связь: «Ashby» — «Beer» (снова)" in text.splitlines()
    assert _rows(send_fake.sent[0].reply_markup) == [
        [("1 · сделал", f"lg:d:{a}:{EPOCH}"), ("1 · не нужно", f"lg:n:{a}:{EPOCH}")],
        [("2 · сделал", f"lg:d:{c}:{EPOCH}"), ("2 · не нужно", f"lg:n:{c}:{EPOCH}")],
    ]

    # The old message's button for the moved gap is stale; the old
    # message keeps its numbering and marks the gap as moved.
    await dp.feed_update(bot, _press(bot, 2, f"lg:n:{a}:{EPOCH}", first_message))
    assert fake.answered[-1].text == "Устарело"
    assert await _status(sessionmaker, a) == "open"
    edit = fake.edits[-1]
    assert edit.message_id == first_message
    assert "— перенесено в новое сообщение сада" in edit.text
    assert _rows(edit.reply_markup) == [
        [("2 · сделал", f"lg:d:{b}:{EPOCH}"), ("2 · не нужно", f"lg:n:{b}:{EPOCH}")]
    ]

    # The new message's button works.
    await dp.feed_update(bot, _press(bot, 3, f"lg:n:{a}:{EPOCH}", 50))
    assert fake.answered[-1].text == "Отмечено: не нужно."
    assert fake.edits[-1].message_id == 50


# --- the web chat -----------------------------------------------------------------------


async def test_a_press_through_the_web_sink_is_refused(sessionmaker):
    await _seed(sessionmaker)
    (a, _b), message_id = await _sent(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    bot.is_web_sink = True
    await dp.feed_update(bot, _press(bot, 1, f"lg:d:{a}:{EPOCH}", message_id))
    assert fake.answered[-1].text == WEB_ONLY_REPLY
    assert fake.edits == []
    assert await _status(sessionmaker, a) == "open"


def test_ingress_blocks_the_garden_buttons():
    """Layer one, before the router's own is_web_sink guard."""
    for data in ("lg:d:1:k3f7qa", "lg:n:1:k3f7qa", "lg:r:1:k3f7qa"):
        assert data.startswith(ingress.BLOCKED_CALLBACK_PREFIX)


# --- the worker's hook -------------------------------------------------------------------


async def _vault_job(sessionmaker, monkeypatch) -> None:
    from app import worker as worker_module
    from app.vault.sync import PassResult

    async def _fake_run_vault_sync(session, settings, clock, client_factory=None):
        return PassResult()

    monkeypatch.setattr(worker_module, "run_vault_sync", _fake_run_vault_sync)
    async with sessionmaker() as session:
        session.add(Job(kind="vault_sync", payload={}, dedup_key="vault_sync:garden"))
        await session.commit()


async def test_the_worker_sends_the_garden_message_after_a_vault_pass(sessionmaker, monkeypatch):
    from app import worker as worker_module

    await _seed(sessionmaker)
    await _record(sessionmaker, new=[_gap()])
    await _vault_job(sessionmaker, monkeypatch)
    bot, fake = _bot()
    assert await worker_module.process_one_job(
        sessionmaker, _settings(), FakeLLMProvider(), _clock(), bot
    )
    assert [m.text.splitlines()[0] for m in fake.sent] == ["Сад линзы, неделя 2026-W40."]


async def test_a_hook_failure_never_fails_the_pass(sessionmaker, monkeypatch, caplog, live_loggers):
    from app import worker as worker_module

    async def _boom(*args, **kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(worker_module.garden_ui, "send_pending", _boom)
    await _seed(sessionmaker)
    await _vault_job(sessionmaker, monkeypatch)
    bot, _fake = _bot()
    caplog.set_level(logging.WARNING)
    assert await worker_module.process_one_job(
        sessionmaker, _settings(), FakeLLMProvider(), _clock(), bot
    )
    async with sessionmaker() as session:
        job = (await session.execute(select(Job))).scalar_one()
    assert job.status == "done"
    assert "lens garden send failed" in {r.getMessage() for r in caplog.records}
    assert "job failed" not in {r.getMessage() for r in caplog.records}


# --- Telegram's 4096 characters -----------------------------------------------------------


def _message(n: int, *, title_len: int = 70, detail_len: int = 300, report=True) -> lens.GardenMessage:
    gaps = tuple(
        lens.MessageGap(
            id=i,
            kind="link",
            titles=(f"{i}" + "а" * title_len, f"{i}" + "б" * title_len),
            title=None,
            detail="д" * detail_len,
            reopened=0,
            status="open",
            actionable=False,
        )
        for i in range(1, n + 1)
    )
    return lens.GardenMessage(
        run_id=1,
        iso_week="2026-W40",
        new=n,
        reopened=0,
        older_open=0,
        report_path=lens.report_path("2026-W40", EPOCH) if report else None,
        gaps=gaps,
        tg_message_id=None,
    )


def test_a_message_that_fits_is_whole():
    text = garden.render(_message(3))
    assert text.count("д" * 300) == 3
    assert garden.SHORTENED_LINE not in text


@pytest.mark.parametrize("n", [12, 20, 40, 120])
def test_a_long_run_is_shortened_to_fit(n):
    text = garden.render(_message(n))
    assert len(text) <= 4096
    assert garden.SHORTENED_LINE in text
    assert text.startswith("Сад линзы, неделя 2026-W40.")
    # Every item keeps its number while any shortening step is enough.
    if n <= 40:
        assert f"\n{n}. Связь: " in text


def test_shortened_without_a_report_says_so_plainly():
    text = garden.render(_message(20, report=False))
    assert garden.SHORTENED_NO_REPORT_LINE in text.splitlines()


async def test_a_long_run_is_sent_within_the_limit(sessionmaker):
    await _seed(sessionmaker)
    long = "Очень длинное синтетическое название заметки номер "
    await _record(
        sessionmaker,
        new=[
            _gap((f"{long}{i}а"[:80], f"{long}{i}б"[:80]), detail="д" * 300) for i in range(10)
        ]
        + [
            _gap((f"{long}{i}в"[:80],), kind="missing_note", title=f"{long}{i}г"[:80], detail="е" * 300)
            for i in range(10)
        ],
    )
    bot, fake = _bot()
    assert await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    assert len(fake.sent[0].text) <= 4096
    assert len(fake.sent[0].reply_markup.inline_keyboard) == 20


# --- logs ----------------------------------------------------------------------------------


LOGGERS = ("app.tg.garden", "app.tg.router", "app.vault.lens", "app.worker")


@pytest.fixture
def live_loggers(monkeypatch):
    """Some earlier test may leave these disabled (logging.config); the
    log checks below must see every record."""
    for name in LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


def _record_text(record: logging.LogRecord) -> str:
    parts = [record.getMessage()]
    for key, value in vars(record).items():
        if key in ("msg", "args", "message") or key.startswith("_"):
            continue
        parts.append(f"{key}={value!r}")
    return " ".join(parts)


async def test_no_title_or_detail_in_logs(sessionmaker, caplog, live_loggers):
    caplog.set_level(logging.DEBUG)
    await _seed(sessionmaker)
    record = await _record(sessionmaker, new=[_gap((SECRET_TITLE, "Beer"), detail=SECRET_DETAIL)])
    bot, _fake = _bot()
    await garden.send_pending(sessionmaker, bot, _settings(), _clock())
    dp, bot, _fake = _dispatcher(sessionmaker)
    (gap_id,) = record.new_ids
    await dp.feed_update(bot, _press(bot, 1, f"lg:d:{gap_id}:{EPOCH}", 1))
    await dp.feed_update(bot, _press(bot, 2, "lg:r:1:k3f7qa", 1))
    messages = {r.getMessage() for r in caplog.records}
    assert {"lens garden message sent", "lens gap decided", "lens garden press"} <= messages
    for rec in caplog.records:
        rendered = _record_text(rec)
        assert SECRET_TITLE not in rendered
        assert SECRET_DETAIL not in rendered
        assert "Anchor/Reports" not in rendered
