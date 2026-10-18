"""A lens research reads different publishers first and cross-checks them.

app/research/sources.py orders the candidates, and app/research/jobs.py's
`_crosscheck` asks each page about the other publishers' cards
(app/research/crosscheck.py) and keeps the verdicts on the cards, which
the result message then shows (app/tg/garden.py). Built on
tests/test_lens_research_idle.py's seeding and scripted provider; every
page, claim and card is synthetic.
"""

from __future__ import annotations

import decimal
import json

from sqlalchemy import select

from app.db.models import IdleRun, StudyCard, StudyClip, StudyJob
from app.llm.provider import LLMError, LLMResponse
from app.research import crosscheck, search
from app.research import jobs as research_jobs
from app.research.fetch import Clip
from app.tg import garden
from app.vault import lens
from test_lens_research_idle import (
    ScriptedProvider,
    distill_reply,
    lens_settings,
    query_reply,
    run,
    seed_garden,
    tap,
    usage,
)

SEP = "https://plato.stanford.edu/entries/cybernetics/"
WIKI = "https://en.wikipedia.org/wiki/Variety_(cybernetics)"
WIKI_2 = "https://en.wikipedia.org/wiki/W._Ross_Ashby"
SEP_TEXT = (
    "Ashby's law of requisite variety holds that only variety can absorb variety. "
    "A regulator must command at least as many responses as there are disturbances."
)
WIKI_TEXT = (
    "In cybernetics, the law of requisite variety states that only variety can absorb variety. "
    "Some authors argue that requisite variety does not apply to social systems at all."
)
SEP_CARD = (True, "Только разнообразие может поглотить разнообразие.", "only variety can absorb variety")
WIKI_CARD = (
    True, "Закон, по мнению некоторых, неприменим к социальным системам.",
    "requisite variety does not apply to social systems at all",
)


def verdicts(*items: tuple[int, str, str]) -> str:
    return json.dumps(
        {"verdicts": [{"card": n, "verdict": v, "quote": q} for n, v, q in items]}
    )


class Pages:
    """The search returns `urls`; the fetcher serves each URL's text."""

    def __init__(self, monkeypatch, urls, texts) -> None:
        self.fetched: list[str] = []

        async def _find_urls(provider, **kwargs):
            return search.SearchOutcome(
                urls=tuple(urls),
                responses=(LLMResponse(text="", usage=usage("0.002"), model="fake-safety"),),
            )

        async def _fetch(url, **kwargs):
            self.fetched.append(url)
            host = url.split("/")[2]
            return Clip(
                url=url, domain=host, title=host, text=texts[url],
                text_sha256="deadbeef", http_status=200,
            )

        monkeypatch.setattr("app.research.search.find_urls", _find_urls)
        monkeypatch.setattr("app.research.jobs.default_fetch", _fetch)


async def _cards(sessionmaker) -> dict[str, StudyCard]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(StudyCard, StudyClip.url).join(StudyClip, StudyClip.id == StudyCard.clip_id)
            )
        ).all()
    return {url: card for card, url in rows}


async def test_two_publishers_are_read_and_checked_against_each_other(sessionmaker, monkeypatch):
    g = await seed_garden(sessionmaker)
    job_id = await tap(sessionmaker, g["tension"])
    pages = Pages(monkeypatch, [WIKI, WIKI_2, SEP], {WIKI: WIKI_TEXT, WIKI_2: WIKI_TEXT, SEP: SEP_TEXT})
    provider = ScriptedProvider(
        query_reply(),
        distill_reply(SEP_CARD),
        distill_reply(WIKI_CARD),
        # SEP's page on Wikipedia's card: nothing it says either way.
        verdicts((1, "unrelated", "")),
        # Wikipedia's page on SEP's card: the same claim, quoted.
        verdicts((1, "supports", "only variety can absorb variety")),
    )

    await run(sessionmaker, lens_settings(), provider)

    # The reference work first, then the other publisher; the second
    # Wikipedia page is never reached (RESEARCH_MAX_PINS is 2).
    assert pages.fetched == [SEP, WIKI]
    assert [schema.name for schema in provider.schemas] == [
        "anchor_lens_query", "anchor_distill_lens", "anchor_distill_lens",
        "anchor_crosscheck", "anchor_crosscheck",
    ]
    # Each page sees only the other publisher's claim.
    assert "1. Закон, по мнению некоторых" in provider.messages[3][1].content
    assert "1. Только разнообразие" in provider.messages[4][1].content

    cards = await _cards(sessionmaker)
    async with sessionmaker() as session:
        wiki_clip = (
            await session.execute(select(StudyClip.id).where(StudyClip.url == WIKI))
        ).scalar_one()
        job = await session.get(StudyJob, job_id)
    assert (cards[SEP].corroboration, cards[SEP].corroboration_clip_id) == (
        crosscheck.SUPPORTED, wiki_clip,
    )
    assert cards[SEP].corroboration_quote == "only variety can absorb variety"
    assert (cards[WIKI].corroboration, cards[WIKI].corroboration_quote) == (
        crosscheck.UNVERIFIED, None,
    )
    # Every call is charged to the job: query, search, two distills, two checks.
    assert (job.status, job.usd_cost) == ("done", decimal.Decimal("0.017"))

    # The result message names the kind of each source and the agreement.
    async with sessionmaker() as session:
        [result] = await research_jobs.unsent_lens_results(session)
        gaps = await lens.research_gaps(session, [g["tension"]])
    text = garden.render_result(gaps[g["tension"]], result.cards, result.hidden)
    assert (
        "(plato.stanford.edu · рецензируемая энциклопедия; подтверждает en.wikipedia.org)" in text
    )
    assert "(en.wikipedia.org · Википедия)" in text


async def test_one_publisher_is_never_its_own_confirmation(sessionmaker, monkeypatch):
    g = await seed_garden(sessionmaker)
    await tap(sessionmaker, g["tension"])
    Pages(monkeypatch, [WIKI, WIKI_2], {WIKI: WIKI_TEXT, WIKI_2: WIKI_TEXT})
    provider = ScriptedProvider(query_reply(), distill_reply(WIKI_CARD), distill_reply(SEP_CARD))

    await run(sessionmaker, lens_settings(), provider)

    assert "anchor_crosscheck" not in [schema.name for schema in provider.schemas]
    assert {card.corroboration for card in (await _cards(sessionmaker)).values()} == {None}


async def test_a_failed_check_leaves_the_cards_unchecked_and_the_job_done(
    sessionmaker, monkeypatch
):
    g = await seed_garden(sessionmaker)
    job_id = await tap(sessionmaker, g["tension"])
    Pages(monkeypatch, [WIKI, SEP], {WIKI: WIKI_TEXT, SEP: SEP_TEXT})

    class _Failing(ScriptedProvider):
        async def complete(self, messages, *, conversation_id, json_schema=None):
            if json_schema is crosscheck.SCHEMA:
                raise LLMError("APIError")
            return await super().complete(
                messages, conversation_id=conversation_id, json_schema=json_schema
            )

    provider = _Failing(query_reply(), distill_reply(SEP_CARD), distill_reply(WIKI_CARD))
    run_id = await run(sessionmaker, lens_settings(), provider)

    async with sessionmaker() as session:
        job = await session.get(StudyJob, job_id)
        idle_run = await session.get(IdleRun, run_id)
    assert (job.status, job.error_code) == ("done", None)
    assert idle_run.status == "done"
    cards = await _cards(sessionmaker)
    assert len(cards) == 2
    assert {card.corroboration for card in cards.values()} == {None}


async def test_a_contradiction_is_kept_over_nothing(sessionmaker, monkeypatch):
    g = await seed_garden(sessionmaker)
    await tap(sessionmaker, g["tension"])
    Pages(monkeypatch, [SEP, WIKI], {WIKI: WIKI_TEXT, SEP: SEP_TEXT})
    provider = ScriptedProvider(
        query_reply(),
        distill_reply(SEP_CARD),
        distill_reply(WIKI_CARD),
        verdicts((1, "unrelated", "")),
        verdicts((1, "contradicts", "requisite variety does not apply to social systems at all")),
    )

    await run(sessionmaker, lens_settings(), provider)

    cards = await _cards(sessionmaker)
    assert cards[SEP].corroboration == crosscheck.CONTRADICTED
    async with sessionmaker() as session:
        [result] = await research_jobs.unsent_lens_results(session)
    [sep_card] = [card for card in result.cards if card.domain == "plato.stanford.edu"]
    assert (sep_card.corroborating_domain, sep_card.corroborating_url) == ("en.wikipedia.org", WIKI)
