"""URL discovery, against a scripted provider (plan sections 6 and 14).

Every test here is about what `app/research/search.py` *throws away*.
The module's whole job is to take a searched response and keep one
thing from it -- the annotation URLs, post-filtered in code -- so the
interesting assertions are all negative: the prose is ignored, the
excerpts never appear, a look-alike domain is refused however it was
ranked, and a URL the model wrote in its own text is not a candidate.
"""

from __future__ import annotations

import pytest

from app.llm.provider import Citation, LLMResponse, LLMUsage
from app.research import search

PACKET = ["reddit.com"]
REF = ["ru.wikipedia.org", "en.wikipedia.org"]


def _usage(cost: str | None = "0.0081") -> LLMUsage:
    import decimal

    return LLMUsage(
        input_tokens=4000,
        cached_tokens=0,
        output_tokens=3,
        cost_usd=decimal.Decimal(cost) if cost else None,
    )


def _response(*urls: str, text: str = "готово", titles=None) -> LLMResponse:
    titles = titles or {}
    return LLMResponse(
        text=text,
        usage=_usage(),
        model="google/gemini-2.5-flash-lite",
        citations=tuple(Citation(url=u, title=titles.get(u)) for u in urls),
    )


class ScriptedProvider:
    """Returns a queued response per call and records what it was asked."""

    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, messages, *, conversation_id, json_schema=None, web_search=None):
        self.calls.append(
            {
                "messages": messages,
                "conversation_id": conversation_id,
                "json_schema": json_schema,
                "web_search": web_search,
            }
        )
        if not self._responses:
            raise AssertionError("scripted provider ran out of responses")
        return self._responses.pop(0)

    async def close(self) -> None:
        return None


# --- what is kept -------------------------------------------------------


async def test_only_annotation_urls_are_kept_and_the_prose_is_ignored():
    """Plan section 6: "Keep only the annotation URLs. Discard the
    model's text entirely." A URL the model typed into its answer is
    not a citation and is not a candidate."""
    provider = ScriptedProvider(
        _response(
            "https://reddit.com/r/a",
            text="Вот что я нашёл: https://evil.io/payload и https://reddit.com/r/not-cited",
        )
    )
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )
    assert outcome.urls == ("https://reddit.com/r/a",)
    assert "evil.io" not in " ".join(outcome.urls)
    assert "not-cited" not in " ".join(outcome.urls)


async def test_provider_ranking_is_preserved():
    """Section 6: "Ranking follows the provider's order." The provider
    is better at relevance than we are, and it is trusted for exactly
    that and nothing else."""
    provider = ScriptedProvider(
        _response("https://reddit.com/c", "https://reddit.com/a", "https://reddit.com/b")
    )
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )
    assert outcome.urls == (
        "https://reddit.com/c",
        "https://reddit.com/a",
        "https://reddit.com/b",
    )


# --- the code-side allowlist -------------------------------------------


@pytest.mark.parametrize(
    "url,why",
    [
        ("https://reddit.com.evil.io/x", "look-alike suffix"),
        ("https://notreddit.com/x", "look-alike prefix"),
        ("https://evil.io/x", "off the allowlist entirely"),
        ("ftp://reddit.com/x", "not http(s)"),
        ("javascript:alert(1)", "not a fetchable scheme"),
        ("https://user:pass@reddit.com/x", "credentials in the URL"),
        ("http://127.0.0.1/x", "not a public address"),
        ("http://169.254.169.254/", "cloud metadata"),
        ("http://localhost/x", "not a public hostname"),
    ],
)
async def test_an_inadmissible_citation_is_dropped(url, why):
    """The provider's domain filter is a request; the allowlist is a
    rule. Whatever comes back is filtered again here."""
    provider = ScriptedProvider(_response(url), _response(url))
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )
    assert outcome.urls == (), why
    assert outcome.error_code == search.NO_RESULTS


async def test_a_subdomain_of_an_allowlisted_domain_is_kept():
    provider = ScriptedProvider(_response("https://old.reddit.com/r/a"))
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )
    assert outcome.urls == ("https://old.reddit.com/r/a",)


async def test_each_allowlisted_domain_in_a_multi_domain_packet_works():
    provider = ScriptedProvider(
        _response("https://ru.wikipedia.org/wiki/Сон", "https://en.wikipedia.org/wiki/Sleep")
    )
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=REF, job_id=1, max_calls=4
    )
    assert len(outcome.urls) == 2


# --- dedupe ------------------------------------------------------------


async def test_two_spellings_of_one_page_count_as_one():
    provider = ScriptedProvider(
        _response("https://reddit.com/r/a", "HTTPS://Reddit.COM/r/a", "https://reddit.com/r/a#top")
    )
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )
    assert outcome.urls == ("https://reddit.com/r/a",)


async def test_a_page_clipped_recently_is_not_a_candidate():
    """Re-reading a page we read last week spends a fetch and a distill
    to produce cards the user already decided about."""
    provider = ScriptedProvider(
        _response("https://reddit.com/r/old", "https://reddit.com/r/new")
    )
    outcome = await search.find_urls(
        provider,
        topic="сон",
        allowed_domains=PACKET,
        recent_urls={"https://reddit.com/r/old"},
        job_id=1,
        max_calls=4,
    )
    assert outcome.urls == ("https://reddit.com/r/new",)


# --- the request shape -------------------------------------------------


async def test_the_search_call_carries_no_schema_and_one_message():
    provider = ScriptedProvider(_response("https://reddit.com/r/a"))
    await search.find_urls(provider, topic="сон", allowed_domains=PACKET, job_id=7, max_calls=4)

    [call] = provider.calls
    assert call["json_schema"] is None
    assert len(call["messages"]) == 1
    assert call["messages"][0].role == "user"
    assert call["messages"][0].content == "Найди страницы по теме «сон» на сайтах: reddit.com."
    assert call["conversation_id"] == "anchor-search-7-0"


async def test_the_packet_is_sent_as_a_hint_and_capped():
    provider = ScriptedProvider(_response("https://ru.wikipedia.org/wiki/x"))
    await search.find_urls(provider, topic="сон", allowed_domains=REF, job_id=1, max_calls=4)

    [call] = provider.calls
    assert call["web_search"].include_domains == tuple(REF)
    assert call["web_search"].max_results == search.MAX_RESULTS
    assert search.MAX_RESULTS <= 5, "plan section 6 caps this at 5"


# --- reformulate once --------------------------------------------------


async def test_nothing_usable_reformulates_once_without_the_site_hint():
    """Plan section 6: "reformulate once (topic only, no site hint) and
    filter again". The allowlist still applies to the second answer --
    dropping the hint widens what the provider looks at, never what we
    are willing to read."""
    provider = ScriptedProvider(
        _response("https://evil.io/x"),
        _response("https://reddit.com/r/found"),
    )
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )

    assert outcome.urls == ("https://reddit.com/r/found",)
    assert outcome.calls == 2
    first, second = provider.calls
    assert first["messages"][0].content.endswith("на сайтах: reddit.com.")
    assert second["messages"][0].content == "Найди страницы по теме «сон»."
    assert second["web_search"].include_domains == ()


async def test_the_second_attempt_is_still_allowlist_filtered():
    provider = ScriptedProvider(_response("https://evil.io/x"), _response("https://evil.io/y"))
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )
    assert outcome.urls == ()
    assert outcome.error_code == search.NO_RESULTS


async def test_it_reformulates_at_most_once():
    provider = ScriptedProvider(*[_response("https://evil.io/x") for _ in range(5)])
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )
    assert outcome.calls == 2, "two attempts, never three"


async def test_a_first_attempt_that_works_does_not_reformulate():
    provider = ScriptedProvider(_response("https://reddit.com/r/a"))
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )
    assert outcome.calls == 1


# --- the search budget -------------------------------------------------


async def test_the_remaining_budget_bounds_the_attempts():
    """`max_calls` is what is left of RESEARCH_MAX_SEARCHES for this
    job, so a job that already searched cannot get two more tries."""
    provider = ScriptedProvider(_response("https://evil.io/x"))
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=1
    )
    assert outcome.calls == 1
    assert outcome.error_code == search.NO_RESULTS


async def test_a_zero_budget_makes_no_call_at_all():
    provider = ScriptedProvider()
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=0
    )
    assert provider.calls == []
    assert outcome.error_code == search.NO_RESULTS


async def test_every_call_is_returned_for_the_ledger_even_when_useless():
    """A search that found nothing still cost a plugin fee and a
    promptful of input tokens. An accounting that only counted useful
    calls would under-report exactly the runs worth noticing."""
    provider = ScriptedProvider(_response("https://evil.io/x"), _response("https://evil.io/y"))
    outcome = await search.find_urls(
        provider, topic="сон", allowed_domains=PACKET, job_id=1, max_calls=4
    )
    assert outcome.calls == 2
    assert len(outcome.responses) == 2
    assert all(r.usage.cost_usd is not None for r in outcome.responses)


# --- filter_citations on its own ---------------------------------------


def test_filter_citations_is_pure_and_order_preserving():
    kept = search.filter_citations(
        [
            Citation(url="https://reddit.com/b"),
            Citation(url="https://evil.io/x"),
            Citation(url="https://reddit.com/a"),
        ],
        allowed_domains=PACKET,
    )
    assert kept == ["https://reddit.com/b", "https://reddit.com/a"]


def test_an_empty_allowlist_admits_nothing():
    """Fails closed. A /study job whose packet was emptied in config must
    not silently become a search of the open web -- which is what an
    allowlist read as "no restriction" would make it, at exactly the
    moment nobody is watching. jobs.py refuses such a job before this is
    reached; this is the second line."""
    kept = search.filter_citations(
        [Citation(url="https://evil.io/x"), Citation(url="https://reddit.com/a")],
        allowed_domains=[],
    )
    assert kept == []
