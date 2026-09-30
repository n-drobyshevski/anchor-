"""Lens research in Telegram (lens L4): the «исследовать и написать» tap, the result
message, its buttons, and `/lens undo`.

anchor-lens-plan.md section 9, the L4 spec sections 1, 4, 5 and 7, with
the owner's amendments: (a) PACKET_LENS without archive.org; (b) a
finished research is its own Telegram message, sent as soon as the job
finishes by the worker's garden hook under the garden message's holds,
with «в Inbox» (`lg:a:`) and «не нужно» (`lg:x:`) that edit that same
message; a spent job sends «ничего не нашлось» and the gap goes back to
open, never to be researched twice. This file asserts:

- the «N · исследовать и написать» row: only for `missing_note`, `tension` and
  `bridge`, only on a live gap never researched, only with every switch
  on and a packet to search;
- the `lg:` grammar, now `(d|n|r|a|x)`, at most 22 bytes;
- the tap: one job, no queue row, the gap `researched`, «Исследую.» and
  the item's mark, in one transaction; every refusal («Исследования
  выключены.», an empty packet, the quota, the budget, «Устарело»)
  rolls back and leaves the gap open; a second tap is stale;
- no message but the result message: nothing at the tap, nothing from
  /study's completion path, and the result message once;
- the result message: gap line, up to six cards with their domains,
  the rest counted, «скрыто: H», its buttons; each hold; a gap resolved
  meanwhile is dropped unsent; a spent or stale job's «ничего не
  нашлось», the gap open again and its garden row back;
- «в Inbox» and «не нужно» edit the same message and remove its
  keyboard; a refusal and an unavailable vault keep it; stale presses;
- `/lens undo`'s outcomes, and the /lens status line;
- the web chat is refused (router and ingress), and logs carry no
  title, detail, card text, quote, URL or domain;
- the worker's hook, and its failure never failing the pass.

All notes, pages and cards are synthetic; the vault is tests/vault_fake.py.
"""

from __future__ import annotations

import datetime
import hashlib
import logging

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import func, select, text

from app.config import Settings
from app.core import echo_write
from app.core.clock import FrozenClock
from app.db.models import Job, LensNote, SpendLedger, StudyCard, StudyClip, StudyJob, UserState, VaultFile
from app.research import jobs
from app.tg import data as data_ui
from app.tg import garden
from app.tg import lens as lens_ui
from app.tg.router import WEB_ONLY_REPLY, build_router
from app.vault import errors, lens
from app.vault.client import VaultClient
from app.vault.errors import VaultError
from app.web import ingress
from conftest import FakeLLMProvider, FakeSession
from tests.vault_fake import FakeVault

CHAT_ID = 555
TIMEZONE = "Europe/Paris"
EPOCH = "k3f7qa"
# 14:00 in Paris: outside the default quiet hours (22:30-08:00).
NOW = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc)
GARDEN_MESSAGE = 1  # FakeSession's first message id
PROPOSED = "Синтетический гомеостат"
DETAIL = "Синтетическая деталь пробела, которой не место в логах."
CARD_TEXT = "Синтетический тезис: регулятор должен быть не проще среды."
CARD_QUOTE = "a synthetic quote that is long enough to count as verbatim"
URL = "https://plato.stanford.edu/entries/synthetic-page/"
DOMAIN = "plato.stanford.edu"


def _clock(moment: datetime.datetime = NOW) -> FrozenClock:
    return FrozenClock(moment)


def _settings(**kwargs) -> Settings:
    base = dict(
        _env_file=None,
        RESEARCH_ENABLED=True,
        LENS_ENABLED=True,
        LENS_GARDEN_ENABLED=True,
        IDLE_ENABLED=True,
        RESEARCH_JOBS_PER_DAY=5,
        DAILY_USD_CAP=10.0,
    )
    base.update(kwargs)
    return Settings(**base)


def _sig(*parts: str) -> str:
    return hashlib.sha256("|".join(("v1",) + parts).encode("utf-8")).hexdigest()


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


def _press(bot: Bot, update_id: int, data: str, message_id: int, text: str = "…") -> Update:
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
                    "text": text,
                },
            },
        },
        context={"bot": bot},
    )


def _rows(markup) -> list[list[tuple[str, str]]]:
    if markup is None:
        return []
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


async def _note(session, title: str) -> int:
    file = VaultFile(path=f"Lens/{title}.md", role="note", note_class="knowledge")
    session.add(file)
    await session.flush()
    body = f"{title}: синтетический текст заметки."
    note = LensNote(
        vault_file_id=file.id, kind="concept", title=title, summary=f"О {title}.", body=body,
        body_hash=hashlib.sha256(body.encode()).hexdigest(), chars=len(body),
    )
    session.add(note)
    await session.flush()
    return note.id


async def _garden(sessionmaker, settings=None) -> dict[str, int]:
    """Two lens notes and one gap of each kind -- missing note, tension,
    link, bridge, numbered 1 to 4 -- sent in one garden message (id 1)."""
    async with sessionmaker() as session:
        session.add(
            UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, vault_epoch=EPOCH, notes_consent=True)
        )
        ashby = await _note(session, "Ashby")
        beer = await _note(session, "Beer")
        await session.commit()
    gaps = [
        lens.NewGap("missing_note", (ashby,), ("Ashby",), PROPOSED, DETAIL, _sig("m"), {"title": PROPOSED}),
        lens.NewGap("tension", (ashby, beer), ("Ashby", "Beer"), None, DETAIL, _sig("t"), {}),
        lens.NewGap("link", (ashby, beer), ("Ashby", "Beer"), None, DETAIL, _sig("l"), {}),
        lens.NewGap("bridge", (beer, ashby), ("Beer", "Ashby"), None, DETAIL, _sig("b"), {}),
    ]
    async with sessionmaker() as session:
        record = await lens.record_garden(
            session, idle_run_id=None, iso_week="2026-W40", version_id=None, findings={},
            resolved_ids=(), reopened_ids=(), new=gaps, now=NOW,
        )
        await session.commit()
    bot, fake = _bot()
    assert await garden.send_pending(sessionmaker, bot, settings or _settings(), _clock())
    missing, tension, link, bridge = record.new_ids
    return {
        "missing": missing, "tension": tension, "link": link, "bridge": bridge,
        "run": record.run_id, "garden_markup": fake.sent[0].reply_markup,
    }


async def _gap(sessionmaker, gap_id: int):
    async with sessionmaker() as session:
        return (
            await session.execute(text("select * from lens_gap where id = :i"), {"i": gap_id})
        ).mappings().one()


async def _jobs(sessionmaker) -> list[StudyJob]:
    async with sessionmaker() as session:
        return list((await session.execute(select(StudyJob).order_by(StudyJob.id))).scalars())


async def _tap_research(sessionmaker, gap_id: int, settings=None, *, epoch=EPOCH, message_id=GARDEN_MESSAGE):
    dp, bot, fake = _dispatcher(sessionmaker, settings)
    await dp.feed_update(bot, _press(bot, 1, f"lg:r:{gap_id}:{epoch}", message_id))
    return fake


async def _finish(sessionmaker, job_id: int, *, cards=(("pending", CARD_TEXT),), status: str = "done") -> list[int]:
    """Play the pipeline: one clip, these cards, the job finished."""
    async with sessionmaker() as session:
        job = await session.get(StudyJob, job_id)
        job.query = job.query or "synthetic query"
        job.status = status
        job.finished_at = NOW
        if status == "failed":
            job.error_code = "search_failed"
        clip = StudyClip(job_id=job_id, url=URL, domain=DOMAIN, text=CARD_QUOTE, fetched_at=NOW)
        session.add(clip)
        await session.flush()
        ids = []
        for card_status, card_text in cards:
            card = StudyCard(
                job_id=job_id, clip_id=clip.id, kind="lens", text=card_text, quote=CARD_QUOTE,
                source_url=URL, risk_model="low", risk_rules="low",
                risk_final="high" if card_status == "hidden" else "low", status=card_status,
            )
            session.add(card)
            await session.flush()
            ids.append(card.id)
        await session.commit()
        return ids


async def _researched(sessionmaker, kind: str = "missing", *, cards=(("pending", CARD_TEXT),), status="done"):
    """A garden, a research tapped on the `kind` gap, and its job finished."""
    g = await _garden(sessionmaker)
    fake = await _tap_research(sessionmaker, g[kind])
    assert fake.answered[-1].text == "Исследую."
    (job,) = await _jobs(sessionmaker)
    card_ids = await _finish(sessionmaker, job.id, cards=cards, status=status)
    return g, job.id, card_ids


async def _deliver(sessionmaker, settings=None, clock=None) -> tuple[int, FakeSession]:
    bot, fake = _bot()
    fake._next_message_id = 50
    sent = await garden.send_research_results(sessionmaker, bot, settings or _settings(), clock or _clock())
    return sent, fake


# --- the «исследовать и написать» row -------------------------------------------------------------


async def test_the_research_row_is_under_each_researchable_gap(sessionmaker):
    g = await _garden(sessionmaker)
    assert _rows(g["garden_markup"]) == [
        [("1 · закрыл", f"lg:d:{g['missing']}:{EPOCH}"), ("1 · не нужно", f"lg:n:{g['missing']}:{EPOCH}")],
        [("1 · исследовать и написать", f"lg:r:{g['missing']}:{EPOCH}")],
        [("2 · закрыл", f"lg:d:{g['tension']}:{EPOCH}"), ("2 · не нужно", f"lg:n:{g['tension']}:{EPOCH}")],
        [("2 · исследовать и написать", f"lg:r:{g['tension']}:{EPOCH}")],
        # No research for a link: its fix is an edge between existing notes.
        [("3 · закрыл", f"lg:d:{g['link']}:{EPOCH}"), ("3 · не нужно", f"lg:n:{g['link']}:{EPOCH}")],
        [("4 · закрыл", f"lg:d:{g['bridge']}:{EPOCH}"), ("4 · не нужно", f"lg:n:{g['bridge']}:{EPOCH}")],
        [("4 · исследовать и написать", f"lg:r:{g['bridge']}:{EPOCH}")],
    ]


@pytest.mark.parametrize(
    "off",
    [
        {"RESEARCH_ENABLED": False},
        {"LENS_ENABLED": False},
        {"IDLE_ENABLED": False},
        {"PACKET_LENS": ""},
    ],
    ids=["research", "lens", "idle", "empty-packet"],
)
async def test_no_research_row_with_a_switch_off_or_no_packet(sessionmaker, off):
    g = await _garden(sessionmaker, _settings(**off))
    assert not [t for row in _rows(g["garden_markup"]) for t, _ in row if "исследовать" in t]
    assert len(_rows(g["garden_markup"])) == 4


def test_research_on_needs_every_switch_and_a_packet():
    assert garden.research_on(_settings())
    for off in ("RESEARCH_ENABLED", "LENS_ENABLED", "LENS_GARDEN_ENABLED", "IDLE_ENABLED"):
        assert not garden.research_on(_settings(**{off: False}))
    assert not garden.research_on(_settings(PACKET_LENS=""))
    # Amendment (a): archive.org (and so web.archive.org) is not searched.
    assert not any("archive.org" in d for d in _settings().PACKET_LENS)


# --- the grammar ----------------------------------------------------------------------------


def test_the_grammar_takes_all_five_actions():
    assert garden.parse_callback("lg:r:12:k3f7qa") == ("research", 12, "k3f7qa")
    assert garden.parse_callback("lg:a:12:k3f7qa") == ("adopt", 12, "k3f7qa")
    assert garden.parse_callback("lg:x:12:k3f7qa") == ("decline", 12, "k3f7qa")
    longest = garden.callback_data("r", 2**31 - 1, "abcdef")
    assert len(longest.encode()) == 22 and garden.parse_callback(longest) is not None


@pytest.mark.parametrize(
    "data",
    ["lg:r", "lg:r:1", "lg:a:1:k3f7q", "lg:x:1:k3f7qa ", "lg:r:0:k3f7qa", "lg:a:2147483648:k3f7qa", "lg:ra:1:k3f7qa"],
)
def test_the_grammar_refuses_near_misses(data):
    assert garden.parse_callback(data) is None


# --- the tap -----------------------------------------------------------------------------------


async def test_the_tap_queues_one_job_without_a_queue_row_and_marks_the_item(sessionmaker):
    g = await _garden(sessionmaker)
    fake = await _tap_research(sessionmaker, g["missing"])

    assert fake.answered[-1].text == "Исследую."
    assert fake.sent == []  # no completion or any other message at the tap
    edit = fake.edits[-1]
    assert edit.message_id == GARDEN_MESSAGE
    lines = edit.text.splitlines()
    first = lines.index(f"1. Нет заметки: «{PROPOSED}» — упоминают «Ashby»")
    assert lines[first + 2] == "— исследую, итог придёт отдельным сообщением"
    rows = _rows(edit.reply_markup)
    assert all(g["missing"] != int(data.split(":")[2]) for row in rows for _t, data in row)
    assert [("2 · исследовать и написать", f"lg:r:{g['tension']}:{EPOCH}")] in rows

    (job,) = await _jobs(sessionmaker)
    assert (job.kind, job.packet, job.lens_gap_id, job.query, job.status) == (
        "study", "lens", g["missing"], None, "queued"
    )
    gap = await _gap(sessionmaker, g["missing"])
    assert (gap["status"], gap["research_requested_at"]) == ("researched", NOW)
    async with sessionmaker() as session:
        assert (await session.execute(select(func.count()).select_from(Job))).scalar_one() == 0


async def test_a_second_tap_is_stale_and_queues_nothing(sessionmaker):
    g = await _garden(sessionmaker)
    await _tap_research(sessionmaker, g["tension"])
    fake = await _tap_research(sessionmaker, g["tension"])
    assert fake.answered[-1].text == "Устарело"
    assert len(await _jobs(sessionmaker)) == 1


async def _assert_rolled_back(sessionmaker, gap_id: int) -> None:
    gap = await _gap(sessionmaker, gap_id)
    assert (gap["status"], gap["research_requested_at"]) == ("open", None)
    assert await _jobs(sessionmaker) == []


@pytest.mark.parametrize(
    "settings,answer",
    [
        (_settings(RESEARCH_ENABLED=False), "Исследования выключены."),
        (_settings(IDLE_ENABLED=False), "Исследования выключены."),
        (_settings(PACKET_LENS=""), "Исследования выключены."),
        (_settings(RESEARCH_JOBS_PER_DAY=0), "На сегодня лимит поиска исчерпан."),
    ],
    ids=["disabled", "idle-off", "empty-packet", "quota"],
)
async def test_each_refusal_rolls_back_and_leaves_the_gap_open(sessionmaker, settings, answer):
    g = await _garden(sessionmaker)
    fake = await _tap_research(sessionmaker, g["missing"], settings)
    assert fake.answered[-1].text == answer
    assert fake.sent == []
    await _assert_rolled_back(sessionmaker, g["missing"])
    # The re-render shows the gap open, its row still there.
    rows = _rows(fake.edits[-1].reply_markup)
    assert ("1 · закрыл", f"lg:d:{g['missing']}:{EPOCH}") in rows[0]


async def test_the_budget_refusal_rolls_back(sessionmaker):
    g = await _garden(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            SpendLedger(local_date=NOW.date(), category="chat", model="m", tokens_in=1, tokens_out=1, usd_cost=5)
        )
        await session.commit()
    fake = await _tap_research(sessionmaker, g["bridge"], _settings(DAILY_USD_CAP=1.0))
    assert fake.answered[-1].text == "На сегодня бюджет на исследования исчерпан."
    await _assert_rolled_back(sessionmaker, g["bridge"])


async def test_the_quota_is_shared_with_study(sessionmaker):
    g = await _garden(sessionmaker)
    async with sessionmaker() as session:
        session.add(StudyJob(kind="study", packet="ref", query="x", local_date=datetime.date(2026, 9, 30)))
        await session.commit()
    fake = await _tap_research(sessionmaker, g["missing"], _settings(RESEARCH_JOBS_PER_DAY=1))
    assert fake.answered[-1].text == "На сегодня лимит поиска исчерпан."
    assert (await _gap(sessionmaker, g["missing"]))["status"] == "open"


@pytest.mark.parametrize(
    "kind,epoch,message_id",
    [("link", EPOCH, GARDEN_MESSAGE), ("missing", "abcdef", GARDEN_MESSAGE), ("missing", EPOCH, 99)],
    ids=["link", "old-epoch", "other-message"],
)
async def test_stale_taps_queue_nothing(sessionmaker, kind, epoch, message_id):
    g = await _garden(sessionmaker)
    fake = await _tap_research(sessionmaker, g[kind], epoch=epoch, message_id=message_id)
    assert fake.answered[-1].text == "Устарело"
    await _assert_rolled_back(sessionmaker, g[kind])


async def test_a_gap_decided_first_cannot_be_researched(sessionmaker):
    g = await _garden(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:n:{g['tension']}:{EPOCH}", GARDEN_MESSAGE))
    await dp.feed_update(bot, _press(bot, 2, f"lg:r:{g['tension']}:{EPOCH}", GARDEN_MESSAGE))
    assert fake.answered[-1].text == "Устарело"
    assert await _jobs(sessionmaker) == []


# --- the result message ----------------------------------------------------------------------


async def test_a_finished_research_is_its_own_message_sent_once(sessionmaker):
    g, job_id, _cards = await _researched(
        sessionmaker,
        cards=[("pending", f"{CARD_TEXT} {i}") for i in range(8)] + [("hidden", "скрытая"), ("hidden", "ещё")],
    )
    sent, fake = await _deliver(sessionmaker)
    assert sent == 1 and len(fake.sent) == 1
    lines = fake.sent[0].text.splitlines()
    assert lines[:2] == ["Сад линзы: исследование.", f"Нет заметки: «{PROPOSED}» — упоминают «Ashby»"]
    assert lines[2:8] == [f"• {CARD_TEXT} {i} ({DOMAIN})" for i in range(6)]
    assert lines[8:] == ["Ещё карточек: 2 — войдут в заметку.", "скрыто: 2"]
    assert "скрытая" not in fake.sent[0].text
    assert _rows(fake.sent[0].reply_markup) == [
        [("в Inbox", f"lg:a:{g['missing']}:{EPOCH}"), ("не нужно", f"lg:x:{g['missing']}:{EPOCH}")]
    ]
    async with sessionmaker() as session:
        job = await session.get(StudyJob, job_id)
        assert job.offered_at == NOW
    gap = await _gap(sessionmaker, g["missing"])
    assert (gap["status"], gap["research_message_id"]) == ("researched", 50)

    again, fake2 = await _deliver(sessionmaker)
    assert again == 0 and fake2.sent == []


async def test_nothing_is_sent_while_the_job_runs(sessionmaker):
    g = await _garden(sessionmaker)
    await _tap_research(sessionmaker, g["missing"])
    sent, fake = await _deliver(sessionmaker)
    assert sent == 0 and fake.sent == []


@pytest.mark.parametrize(
    "settings,state,moment",
    [
        pytest.param(_settings(LENS_GARDEN_ENABLED=False), {}, NOW, id="flag-off"),
        pytest.param(_settings(), {"persona_active": False}, NOW, id="paused"),
        pytest.param(_settings(), {"quiet_until": NOW + datetime.timedelta(hours=2)}, NOW, id="quiet-cmd"),
        pytest.param(
            _settings(), {}, datetime.datetime(2026, 9, 30, 21, 30, tzinfo=datetime.timezone.utc), id="quiet-hours"
        ),
        pytest.param(_settings(), {"welfare_at": NOW - datetime.timedelta(hours=23)}, NOW, id="welfare"),
    ],
)
async def test_each_hold_keeps_the_result_unsent(sessionmaker, settings, state, moment):
    g, job_id, _cards = await _researched(sessionmaker)
    async with sessionmaker() as session:
        for key, value in state.items():
            await session.execute(
                text(f"update user_state set {key} = :v where id = 1"), {"v": value}
            )
        await session.commit()
    sent, fake = await _deliver(sessionmaker, settings, _clock(moment))
    assert sent == 0 and fake.sent == []
    async with sessionmaker() as session:
        assert (await session.get(StudyJob, job_id)).offered_at is None
    assert (await _gap(sessionmaker, g["missing"]))["status"] == "researched"


@pytest.mark.parametrize(
    "cards,status",
    [((), "done"), ((("hidden", "x"),), "done"), ((), "failed")],
    ids=["nothing-found", "all-hidden", "failed"],
)
async def test_a_spent_research_says_nothing_found_and_reopens_the_gap(sessionmaker, cards, status):
    g, job_id, _ids = await _researched(sessionmaker, "tension", cards=cards, status=status)
    sent, fake = await _deliver(sessionmaker)
    assert sent == 1
    assert fake.sent[0].text.splitlines() == [
        "Сад линзы: исследование.",
        "Противоречие: «Ashby» — «Beer»",
        "Ничего не нашлось. Пункт снова открыт в сообщении сада.",
    ]
    assert fake.sent[0].reply_markup is None
    gap = await _gap(sessionmaker, g["tension"])
    assert (gap["status"], gap["research_requested_at"], gap["tg_message_id"]) == ("open", NOW, GARDEN_MESSAGE)
    async with sessionmaker() as session:
        assert (await session.get(StudyJob, job_id)).offered_at == NOW
    # The garden message is re-rendered: the gap's row is back, without
    # «исследовать и написать» (never twice), and the item says what happened.
    edit = fake.edits[-1]
    assert edit.message_id == GARDEN_MESSAGE
    rows = _rows(edit.reply_markup)
    assert [("2 · закрыл", f"lg:d:{g['tension']}:{EPOCH}"), ("2 · не нужно", f"lg:n:{g['tension']}:{EPOCH}")] in rows
    assert ("2 · исследовать и написать", f"lg:r:{g['tension']}:{EPOCH}") not in [b for row in rows for b in row]
    assert "— исследовано, в Inbox ничего не записано" in edit.text
    # And a new tap on it is stale.
    fake2 = await _tap_research(sessionmaker, g["tension"])
    assert fake2.answered[-1].text == "Устарело"
    assert len(await _jobs(sessionmaker)) == 1


async def test_a_job_unfinished_after_three_days_goes_out_as_nothing_found(sessionmaker):
    g = await _garden(sessionmaker)
    await _tap_research(sessionmaker, g["bridge"])
    (job,) = await _jobs(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            text("update study_job set created_at = :t where id = :i"),
            {"t": NOW - datetime.timedelta(days=3, minutes=1), "i": job.id},
        )
        await session.commit()
    sent, fake = await _deliver(sessionmaker)
    assert sent == 1
    assert "Ничего не нашлось." in fake.sent[0].text
    async with sessionmaker() as session:
        row = await session.get(StudyJob, job.id)
        assert (row.status, row.error_code) == ("failed", jobs.STALE)
    assert (await _gap(sessionmaker, g["bridge"]))["status"] == "open"


async def test_a_result_whose_cards_all_expired_gives_the_gap_back(sessionmaker):
    """Sweeps expire untapped lens cards; the gap must not stay
    `researched` for good with no row anywhere."""
    g, _job, (card,) = await _researched(sessionmaker, "tension")
    await _deliver(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(text("update study_card set status = 'expired' where id = :i"), {"i": card})
        await session.commit()
    sent, fake = await _deliver(sessionmaker)
    assert sent == 0 and fake.sent == []
    gap = await _gap(sessionmaker, g["tension"])
    assert (gap["status"], gap["research_requested_at"]) == ("open", NOW)
    garden_edit = next(e for e in fake.edits if e.message_id == GARDEN_MESSAGE)
    assert [("2 · закрыл", f"lg:d:{g['tension']}:{EPOCH}"), ("2 · не нужно", f"lg:n:{g['tension']}:{EPOCH}")] in _rows(
        garden_edit.reply_markup
    )
    # Once only.
    _sent, again = await _deliver(sessionmaker)
    assert again.edits == []


async def test_a_result_whose_gap_was_resolved_is_marked_and_not_sent(sessionmaker):
    g, job_id, _cards = await _researched(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(text("update lens_gap set status = 'resolved', resolved_at = now() where id = :i"), {"i": g["missing"]})
        await session.commit()
    sent, fake = await _deliver(sessionmaker)
    assert sent == 0 and fake.sent == []
    async with sessionmaker() as session:
        assert (await session.get(StudyJob, job_id)).offered_at == NOW


# --- «в Inbox» and «не нужно» ---------------------------------------------------------------


@pytest.fixture
def vault(monkeypatch) -> FakeVault:
    """The router looks the client up per press: swap the factory."""
    fake = FakeVault()
    monkeypatch.setattr(VaultClient, "from_settings", classmethod(lambda cls, settings: fake))
    return fake


async def _sent_result(sessionmaker, kind="missing", **kw):
    g, job_id, cards = await _researched(sessionmaker, kind, **kw)
    _sent, fake = await _deliver(sessionmaker)
    return g, job_id, cards, fake.sent[0].text


async def test_adopt_writes_the_note_and_edits_the_same_message(sessionmaker, vault):
    g, _job, (card,), result_text = await _sent_result(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['missing']}:{EPOCH}", 50, result_text))

    assert fake.answered[-1].text == "Записано в Inbox."
    assert fake.sent == []
    edit = next(e for e in fake.edits if e.message_id == 50)
    assert edit.text == result_text + "\n— записано в Inbox. Отменить: /lens undo"
    assert edit.reply_markup is None
    ((path, (note_class, content)),) = vault.notes.items()
    assert (path, note_class) == (f"Echo/Inbox/{PROPOSED}.md", "knowledge")
    assert CARD_TEXT in content
    assert (await _gap(sessionmaker, g["missing"]))["status"] == "done"
    async with sessionmaker() as session:
        assert (await session.get(StudyCard, card)).status == "adopted"
    # The garden message now says where the gap went.
    garden_edit = next(e for e in fake.edits if e.message_id == GARDEN_MESSAGE)
    assert "— записано в Inbox (проверю в следующем саду)" in garden_edit.text

    # A replay is stale and writes nothing. It carries the text as it
    # was sent (a double tap, a crash replay), before the outcome line:
    # only the keyboard goes, and the outcome line stays.
    await dp.feed_update(bot, _press(bot, 2, f"lg:a:{g['missing']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Устарело"
    assert len(vault.echo_changesets) == 1
    assert [e for e in fake.edits if e.message_id == 50] == [edit]
    assert (fake.markup_edits[-1].message_id, fake.markup_edits[-1].reply_markup) == (50, None)


async def test_a_refused_write_keeps_the_message_and_its_buttons(sessionmaker, vault):
    g, _job, _cards, result_text = await _sent_result(sessionmaker)
    vault.echo_inbox = None
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['missing']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Не получилось записать в Inbox."
    assert fake.edits == []
    assert (await _gap(sessionmaker, g["missing"]))["status"] == "researched"


async def test_an_unavailable_vault_keeps_the_buttons_and_the_next_tap_replays(sessionmaker, vault):
    g, _job, _cards, result_text = await _sent_result(sessionmaker, "tension")
    vault.echo_crash_after_put = VaultError(errors.UNAVAILABLE)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['tension']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Хранилище не ответило. Попробуй ещё раз."
    assert fake.edits == []
    await dp.feed_update(bot, _press(bot, 2, f"lg:a:{g['tension']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Записано в Inbox."
    assert len(vault.notes) == 1


async def test_no_card_left_closes_the_message_and_reopens_the_gap(sessionmaker, vault):
    g, _job, (card,), result_text = await _sent_result(sessionmaker, "bridge")
    async with sessionmaker() as session:
        await session.execute(text("update study_card set status = 'expired' where id = :i"), {"i": card})
        await session.commit()
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['bridge']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Карточек не осталось."
    edit = next(e for e in fake.edits if e.message_id == 50)
    assert edit.text.endswith("\n— карточек не осталось") and edit.reply_markup is None
    assert (await _gap(sessionmaker, g["bridge"]))["status"] == "open"
    assert vault.notes == {}


async def test_decline_rejects_the_cards_reopens_the_gap_and_edits_the_same_message(sessionmaker, vault):
    g, _job, (card,), result_text = await _sent_result(sessionmaker, "tension")
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:x:{g['tension']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Отмечено: не нужно."
    edit = next(e for e in fake.edits if e.message_id == 50)
    assert edit.text == result_text + "\n— не нужно"
    assert edit.reply_markup is None
    async with sessionmaker() as session:
        assert (await session.get(StudyCard, card)).status == "rejected"
    gap = await _gap(sessionmaker, g["tension"])
    assert (gap["status"], gap["research_requested_at"]) == ("open", NOW)
    garden_edit = next(e for e in fake.edits if e.message_id == GARDEN_MESSAGE)
    assert [("2 · закрыл", f"lg:d:{g['tension']}:{EPOCH}"), ("2 · не нужно", f"lg:n:{g['tension']}:{EPOCH}")] in _rows(
        garden_edit.reply_markup
    )
    assert vault.notes == {}
    # «в Inbox» after «не нужно» is stale, and keeps «— не нужно» even
    # though the press carries the text from before it.
    await dp.feed_update(bot, _press(bot, 2, f"lg:a:{g['tension']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Устарело"
    assert vault.notes == {}
    assert [e for e in fake.edits if e.message_id == 50] == [edit]


# --- a write whose answer was lost ------------------------------------------------------------


async def _row(sessionmaker):
    async with sessionmaker() as session:
        return (
            await session.execute(text("select * from echo_changeset order by id"))
        ).mappings().all()


async def _lost_answer(sessionmaker, vault, kind="tension"):
    """A result sent, then «в Inbox» whose answer from vaultd was lost:
    the note is in the inbox, its row unconfirmed."""
    g, _job, cards, result_text = await _sent_result(sessionmaker, kind)
    vault.echo_crash_after_put = VaultError(errors.UNAVAILABLE)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g[kind]}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Хранилище не ответило. Попробуй ещё раз."
    assert len(vault.notes) == 1
    (row,) = await _row(sessionmaker)
    assert row["confirmed_at"] is None
    return g, cards, result_text


async def _expire(sessionmaker, card_ids) -> None:
    async with sessionmaker() as session:
        await session.execute(
            text("update study_card set status = 'expired' where id = any(:ids)"), {"ids": list(card_ids)}
        )
        await session.commit()


async def test_a_replay_after_the_cards_expired_still_confirms_the_write(sessionmaker, vault):
    g, cards, result_text = await _lost_answer(sessionmaker, vault)
    await _expire(sessionmaker, cards)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 2, f"lg:a:{g['tension']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Записано в Inbox."
    assert len(vault.notes) == 1 and len(vault.echo_changesets) == 1
    (row,) = await _row(sessionmaker)
    assert row["confirmed_at"] == NOW
    assert (await _gap(sessionmaker, g["tension"]))["status"] == "done"
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.UNDONE]
    assert vault.notes == {}


async def test_a_replay_of_a_write_that_never_landed_writes_the_chosen_cards(sessionmaker, vault):
    g, cards, result_text = await _lost_answer(sessionmaker, vault)
    # vaultd had not written it after all.
    vault.notes.clear()
    vault.echo_changesets.clear()
    await _expire(sessionmaker, cards)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 2, f"lg:a:{g['tension']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Записано в Inbox."
    ((_path, (_class, content)),) = vault.notes.items()
    assert CARD_TEXT in content


async def test_decline_after_a_lost_answer_confirms_the_written_note(sessionmaker, vault):
    g, cards, result_text = await _lost_answer(sessionmaker, vault)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 2, f"lg:x:{g['tension']}:{EPOCH}", 50, result_text))
    # The note is in the inbox after all: that is the outcome, and undo can take it back.
    assert fake.answered[-1].text == "Записано в Inbox."
    edit = next(e for e in fake.edits if e.message_id == 50)
    assert edit.text == result_text + "\n— записано в Inbox. Отменить: /lens undo"
    (row,) = await _row(sessionmaker)
    assert row["confirmed_at"] == NOW
    assert (await _gap(sessionmaker, g["tension"]))["status"] == "done"
    async with sessionmaker() as session:
        assert (await session.get(StudyCard, cards[0])).status == "adopted"
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.UNDONE]


async def test_decline_after_a_write_that_never_landed_forgets_it(sessionmaker, vault):
    g, _job, (card,), result_text = await _sent_result(sessionmaker, "tension")
    vault.echo_put_error = VaultError(errors.UNAVAILABLE)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['tension']}:{EPOCH}", 50, result_text))
    assert len(await _row(sessionmaker)) == 1 and vault.notes == {}
    await dp.feed_update(bot, _press(bot, 2, f"lg:x:{g['tension']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Отмечено: не нужно."
    assert await _row(sessionmaker) == []
    assert (await _gap(sessionmaker, g["tension"]))["status"] == "open"
    async with sessionmaker() as session:
        assert (await session.get(StudyCard, card)).status == "rejected"


async def test_decline_with_the_vault_down_after_a_lost_answer_keeps_the_buttons(sessionmaker, vault):
    g, _cards, result_text = await _lost_answer(sessionmaker, vault)
    vault.down = True
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 2, f"lg:x:{g['tension']}:{EPOCH}", 50, result_text))
    assert fake.answered[-1].text == "Хранилище не ответило. Попробуй ещё раз."
    assert fake.edits == [] and fake.markup_edits == []
    assert (await _gap(sessionmaker, g["tension"]))["status"] == "researched"
    assert len(await _row(sessionmaker)) == 1


async def test_the_hook_settles_a_lost_write_whose_gap_a_recheck_resolved(sessionmaker, vault):
    g, _cards, _text = await _lost_answer(sessionmaker, vault, "missing")
    async with sessionmaker() as session:
        await session.execute(
            text("update lens_gap set status = 'resolved', resolved_at = now() where id = :i"),
            {"i": g["missing"]},
        )
        await session.commit()
    await _deliver(sessionmaker)
    (row,) = await _row(sessionmaker)
    assert row["confirmed_at"] == NOW
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.UNDONE]


async def test_the_hook_forgets_a_lost_write_vaultd_never_made(sessionmaker, vault):
    g, _cards, _text = await _lost_answer(sessionmaker, vault, "missing")
    vault.notes.clear()
    vault.echo_changesets.clear()
    async with sessionmaker() as session:
        await session.execute(
            text("update lens_gap set status = 'resolved', resolved_at = now() where id = :i"),
            {"i": g["missing"]},
        )
        await session.commit()
    await _deliver(sessionmaker)
    assert await _row(sessionmaker) == []
    assert vault.writes() == [] and vault.notes == {}


async def test_spent_results_settle_a_lost_write_before_reopening(sessionmaker, vault):
    g, cards, _text = await _lost_answer(sessionmaker, vault)
    await _expire(sessionmaker, cards)
    vault.down = True
    await _deliver(sessionmaker)
    # The vault did not answer: the gap waits, nothing is dropped.
    assert (await _gap(sessionmaker, g["tension"]))["status"] == "researched"
    assert len(await _row(sessionmaker)) == 1
    vault.down = False
    await _deliver(sessionmaker)
    (row,) = await _row(sessionmaker)
    assert row["confirmed_at"] == NOW
    # The note exists: the gap is done, not reopened.
    assert (await _gap(sessionmaker, g["tension"]))["status"] == "done"


@pytest.mark.parametrize(
    "data_of",
    [
        lambda gap: f"lg:a:{gap}:abcdef",
        lambda gap: f"lg:x:{gap}:abcdef",
    ],
    ids=["adopt-old-epoch", "decline-old-epoch"],
)
async def test_stale_result_presses_remove_the_dead_keyboard(sessionmaker, vault, data_of):
    g, _job, (card,), result_text = await _sent_result(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, data_of(g["missing"]), 50, result_text))
    assert fake.answered[-1].text == "Устарело"
    assert [e for e in fake.edits if e.message_id == 50] == []
    assert (fake.markup_edits[-1].message_id, fake.markup_edits[-1].reply_markup) == (50, None)
    assert (await _gap(sessionmaker, g["missing"]))["status"] == "researched"
    async with sessionmaker() as session:
        assert (await session.get(StudyCard, card)).status == "pending"


async def test_result_buttons_on_the_garden_message_are_stale(sessionmaker, vault):
    g, _job, _cards, _text = await _sent_result(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['missing']}:{EPOCH}", GARDEN_MESSAGE))
    assert fake.answered[-1].text == "Устарело"
    assert vault.notes == {}


def test_with_outcome_replaces_an_earlier_outcome_and_fits():
    assert garden.with_outcome("a\nb", garden.RESULT_DECLINED) == "a\nb\n— не нужно"
    assert garden.with_outcome("a\nb\n— не нужно", garden.RESULT_ADOPTED) == (
        "a\nb\n— записано в Inbox. Отменить: /lens undo"
    )
    long = garden.with_outcome("д" * 5000, garden.RESULT_DECLINED)
    assert len(long) == 4096 and long.endswith("…\n— не нужно")


# --- the web chat ----------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["r", "a", "x"])
async def test_research_presses_through_the_web_sink_are_refused(sessionmaker, vault, action):
    g, _job, _cards, _text = await _sent_result(sessionmaker)
    dp, bot, fake = _dispatcher(sessionmaker)
    bot.is_web_sink = True
    await dp.feed_update(bot, _press(bot, 1, f"lg:{action}:{g['tension']}:{EPOCH}", GARDEN_MESSAGE))
    await dp.feed_update(bot, _press(bot, 2, f"lg:{action}:{g['missing']}:{EPOCH}", 50))
    assert [a.text for a in fake.answered] == [WEB_ONLY_REPLY, WEB_ONLY_REPLY]
    assert fake.edits == []
    assert len(await _jobs(sessionmaker)) == 1
    assert vault.notes == {}


def test_ingress_blocks_every_research_button_and_the_command():
    for data in ("lg:r:1:k3f7qa", "lg:a:1:k3f7qa", "lg:x:1:k3f7qa"):
        assert data.startswith(ingress.BLOCKED_CALLBACK_PREFIX)
    assert "lens" in ingress.BLOCKED_COMMANDS


# --- /lens undo and the status line ---------------------------------------------------------


async def _undo(sessionmaker, vault) -> str:
    return await lens_ui.command(sessionmaker, _settings(), _clock(), "undo", client_factory=lambda s: vault)


async def test_lens_undo_replies_for_every_outcome(sessionmaker, vault):
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.NOTHING]
    assert "Нечего отменять" in lens_ui.UNDO_REPLIES[echo_write.NOTHING]

    g, _job, _cards, result_text = await _sent_result(sessionmaker)
    dp, bot, _fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['missing']}:{EPOCH}", 50, result_text))
    (path,) = vault.notes

    vault.notes[path] = ("knowledge", vault.notes[path][1] + "правка\n")
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.CHANGED]
    vault.notes[path] = ("knowledge", vault.notes[path][1].removesuffix("правка\n"))

    vault.down = True
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.UNAVAILABLE]
    vault.down = False

    assert await _undo(sessionmaker, vault) == "Отменено: последняя заметка Echo в Inbox удалена."
    assert vault.notes == {}
    assert vault.undo_calls[-1][1] == "echo"
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.NOTHING]


async def test_lens_undo_expired(sessionmaker, vault):
    g, _job, _cards, result_text = await _sent_result(sessionmaker)
    dp, bot, _fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['missing']}:{EPOCH}", 50, result_text))
    vault.echo_changesets.clear()
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.EXPIRED]


async def test_lens_undo_after_a_lost_answer_is_undone_not_changed(sessionmaker, vault):
    g, _job, _cards, result_text = await _sent_result(sessionmaker)
    dp, bot, _fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['missing']}:{EPOCH}", 50, result_text))
    vault.echo_undo_crash_after = VaultError(errors.UNAVAILABLE)
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.UNAVAILABLE]
    assert vault.notes == {}
    # vaultd did undo it: the retry says so, marks the row, and does not stick.
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.UNDONE]
    assert await _undo(sessionmaker, vault) == lens_ui.UNDO_REPLIES[echo_write.NOTHING]


def test_every_undo_outcome_has_a_reply_and_the_usage_names_undo():
    for outcome in (
        echo_write.UNDONE, echo_write.NOTHING, echo_write.EXPIRED, echo_write.CHANGED,
        echo_write.REFUSED, echo_write.UNAVAILABLE,
    ):
        assert lens_ui.UNDO_REPLIES[outcome]
    assert "/lens undo" in lens_ui.USAGE


async def test_the_status_counts_research_in_flight(sessionmaker):
    g = await _garden(sessionmaker)
    await _tap_research(sessionmaker, g["missing"])
    status = await lens_ui.command(sessionmaker, _settings(), _clock(), None)
    assert "Исследования: идёт 1, ждут решения 0." in status.splitlines()
    (job,) = await _jobs(sessionmaker)
    await _finish(sessionmaker, job.id)
    # Finished but not sent yet (a hold): nothing to decide on so far.
    status = await lens_ui.command(sessionmaker, _settings(), _clock(), None)
    assert not [line for line in status.splitlines() if line.startswith("Исследования:")]
    await _deliver(sessionmaker)
    status = await lens_ui.command(sessionmaker, _settings(), _clock(), None)
    assert "Исследования: идёт 0, ждут решения 1." in status.splitlines()
    assert CARD_TEXT not in status and PROPOSED not in status
    # A recheck resolved the gap: its buttons are dead, so it no longer waits.
    async with sessionmaker() as session:
        await session.execute(
            text("update lens_gap set status = 'resolved', resolved_at = now() where id = :i"),
            {"i": g["missing"]},
        )
        await session.commit()
    status = await lens_ui.command(sessionmaker, _settings(), _clock(), None)
    assert not [line for line in status.splitlines() if line.startswith("Исследования:")]


def test_the_delete_confirmation_says_inbox_notes_stay():
    # The inbox is a setting: the line names it, and its default.
    assert "echo_inbox, по умолчанию Echo/Inbox" in data_ui.CONFIRM_VAULT_LINE
    assert "их удаляешь ты" in data_ui.CONFIRM_VAULT_LINE


# --- the worker's hook -------------------------------------------------------------------------


async def _vault_job(sessionmaker, monkeypatch) -> None:
    from app import worker as worker_module
    from app.vault.sync import PassResult

    async def _fake_run_vault_sync(session, settings, clock, client_factory=None):
        return PassResult()

    monkeypatch.setattr(worker_module, "run_vault_sync", _fake_run_vault_sync)
    async with sessionmaker() as session:
        session.add(Job(kind="vault_sync", payload={}, dedup_key="vault_sync:research"))
        await session.commit()


async def test_the_worker_sends_the_result_after_a_vault_pass(sessionmaker, monkeypatch):
    from app import worker as worker_module

    await _researched(sessionmaker)
    await _vault_job(sessionmaker, monkeypatch)
    bot, fake = _bot()
    assert await worker_module.process_one_job(sessionmaker, _settings(), FakeLLMProvider(), _clock(), bot)
    assert [m.text.splitlines()[0] for m in fake.sent] == ["Сад линзы: исследование."]


async def test_a_result_hook_failure_never_fails_the_pass_nor_the_garden(sessionmaker, monkeypatch, caplog, live_loggers):
    from app import worker as worker_module

    async def _boom(*args, **kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(worker_module.garden_ui, "send_research_results", _boom)
    calls = []

    async def _garden_send(*args, **kwargs):
        calls.append(1)
        return False

    monkeypatch.setattr(worker_module.garden_ui, "send_pending", _garden_send)
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, vault_epoch=EPOCH))
        await session.commit()
    await _vault_job(sessionmaker, monkeypatch)
    bot, _fake = _bot()
    caplog.set_level(logging.WARNING)
    assert await worker_module.process_one_job(sessionmaker, _settings(), FakeLLMProvider(), _clock(), bot)
    async with sessionmaker() as session:
        job = (await session.execute(select(Job))).scalar_one()
    assert job.status == "done"
    assert calls == [1]
    assert "lens research send failed" in {r.getMessage() for r in caplog.records}


# --- logs --------------------------------------------------------------------------------------


LOGGERS = ("app.tg.garden", "app.tg.router", "app.tg.lens", "app.vault.lens", "app.research.jobs", "app.core.echo_write", "app.worker")


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


async def test_no_research_log_line_carries_a_gap_id(sessionmaker, vault, caplog, live_loggers):
    """The L4 spec section 6: a research line naming its gap would tell a
    researched gap from a resolved one in Railway's logs. The tap, the
    job, the result (sent and spent), «в Inbox», «не нужно», a lost
    write settled, and /lens undo -- none names a gap."""
    caplog.set_level(logging.DEBUG)
    g = await _garden(sessionmaker)
    for kind in ("missing", "tension", "bridge"):
        await _tap_research(sessionmaker, g[kind])
    missing_job, tension_job, bridge_job = await _jobs(sessionmaker)
    await _finish(sessionmaker, missing_job.id)
    await _finish(sessionmaker, tension_job.id, cards=(), status="failed")
    await _finish(sessionmaker, bridge_job.id)
    # Result messages 50 (missing), 51 (tension: «ничего не нашлось»), 52 (bridge).
    _sent, delivered = await _deliver(sessionmaker)
    dp, bot, _fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['missing']}:{EPOCH}", 50, "…"))
    vault.echo_put_error = VaultError(errors.UNAVAILABLE)
    await dp.feed_update(bot, _press(bot, 2, f"lg:a:{g['bridge']}:{EPOCH}", 52, "…"))
    await dp.feed_update(bot, _press(bot, 3, f"lg:x:{g['bridge']}:{EPOCH}", 52, "…"))
    await dp.feed_update(bot, _press(bot, 4, f"lg:a:{g['missing']}:abcdef", 50, "…"))
    await _undo(sessionmaker, vault)
    assert len(delivered.sent) == 3
    messages = {r.getMessage() for r in caplog.records}
    assert {
        "lens research requested", "lens study job queued", "lens research result sent",
        "lens research press", "lens cards rejected", "echo inbox", "lens research closed",
    } <= messages
    for rec in caplog.records:
        assert getattr(rec, "gap_id", None) is None, rec.getMessage()


async def test_logs_carry_no_title_detail_card_or_url(sessionmaker, vault, caplog, live_loggers):
    caplog.set_level(logging.DEBUG)
    g, _job, _cards, result_text = await _sent_result(sessionmaker)
    dp, bot, _fake = _dispatcher(sessionmaker)
    await dp.feed_update(bot, _press(bot, 1, f"lg:a:{g['missing']}:{EPOCH}", 50, result_text))
    await lens_ui.command(sessionmaker, _settings(), _clock(), "undo", client_factory=lambda s: vault)
    messages = {r.getMessage() for r in caplog.records}
    assert {"lens garden press", "lens research result sent", "lens research press"} <= messages
    for rec in caplog.records:
        rendered = _record_text(rec)
        for secret in (PROPOSED, DETAIL, CARD_TEXT, CARD_QUOTE, URL, DOMAIN, "Echo/Inbox", "Ashby"):
            assert secret not in rendered, (rec.getMessage(), secret)
        assert "message_id" not in vars(rec)
