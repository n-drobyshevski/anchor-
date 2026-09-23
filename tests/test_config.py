"""The startup configuration check (app/config.py).

Every deployment of this app so far died inside a dependency's
constructor -- aiogram's "Token is invalid!", the openai SDK's "Missing
credentials ... set the OPENAI_API_KEY environment variable" -- after
the process was already gone and the healthcheck with it. These tests
pin the behaviour that replaces that: name what is missing, name all of
it at once, and never print a value.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, check_runtime_settings, missing_required

_COMPLETE = {
    "MODE": "polling",
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "DATABASE_URL": "postgresql://u:p@h/db",
    "OPENROUTER_API_KEY": "sk-or-test",
    "ALLOWED_CHAT_ID": 2126354677,
}


def _settings(**overrides) -> Settings:
    return Settings(**{**_COMPLETE, **overrides})


def test_a_complete_polling_config_passes():
    check_runtime_settings(_settings())  # must not raise


def test_a_complete_webhook_config_passes():
    check_runtime_settings(
        _settings(MODE="webhook", TELEGRAM_SECRET_TOKEN="a" * 32, PUBLIC_URL="https://x.up.railway.app")
    )


@pytest.mark.parametrize("name", ["TELEGRAM_BOT_TOKEN", "DATABASE_URL", "OPENROUTER_API_KEY"])
def test_each_always_required_credential_is_reported(name):
    assert missing_required(_settings(**{name: ""})) == [name]


def test_unset_chat_id_is_reported():
    """0 is the field default and no real chat has that id, so it reads
    as unset rather than as a deliberate value."""
    assert missing_required(_settings(ALLOWED_CHAT_ID=0)) == ["ALLOWED_CHAT_ID"]


def test_webhook_only_requirements_are_not_demanded_in_polling_mode():
    assert missing_required(_settings(MODE="polling", TELEGRAM_SECRET_TOKEN="", PUBLIC_URL="")) == []


@pytest.mark.parametrize("name", ["TELEGRAM_SECRET_TOKEN", "PUBLIC_URL"])
def test_webhook_mode_requires_the_http_settings(name):
    complete_webhook = {
        "MODE": "webhook",
        "TELEGRAM_SECRET_TOKEN": "a" * 32,
        "PUBLIC_URL": "https://x.up.railway.app",
    }
    assert missing_required(_settings(**{**complete_webhook, name: ""})) == [name]


def test_every_missing_name_is_reported_at_once():
    """One round trip to fix a misconfigured deploy, not one per
    variable -- each redeploy costs minutes."""
    settings = _settings(MODE="webhook", TELEGRAM_BOT_TOKEN="", OPENROUTER_API_KEY="", ALLOWED_CHAT_ID=0)

    with pytest.raises(SystemExit) as excinfo:
        check_runtime_settings(settings)

    message = str(excinfo.value)
    for name in ("TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "ALLOWED_CHAT_ID", "TELEGRAM_SECRET_TOKEN"):
        assert name in message


def test_the_message_names_variables_but_never_their_values():
    secret = "8700902256:AAHsupersecrettokenvalue"
    settings = _settings(TELEGRAM_BOT_TOKEN=secret, OPENROUTER_API_KEY="")

    with pytest.raises(SystemExit) as excinfo:
        check_runtime_settings(settings)

    assert "OPENROUTER_API_KEY" in str(excinfo.value)
    assert secret not in str(excinfo.value)


@pytest.mark.parametrize("mode", ["Webhook", "WEBHOOK", "web hook", "", "prod"])
def test_an_unrecognised_mode_is_rejected_rather_than_falling_through(mode):
    """main() branches `if MODE == "webhook" ... else: polling`, so any
    typo silently starts the transport that serves no HTTP -- the bot
    would look healthy in the logs while the healthcheck failed
    forever."""
    with pytest.raises(SystemExit, match="MODE must be one of"):
        check_runtime_settings(_settings(MODE=mode))


def test_mode_is_checked_before_missing_credentials():
    """A bad MODE changes which variables are required, so reporting a
    missing-variable list computed from it would send you after the
    wrong ones."""
    with pytest.raises(SystemExit, match="MODE must be one of"):
        check_runtime_settings(_settings(MODE="polllling", TELEGRAM_BOT_TOKEN=""))


# --- whitespace and the secret-token charset ---------------------------------


@pytest.mark.parametrize(
    "padded",
    ["abc123 ", " abc123", "abc123\n", "abc123\r\n", "\tabc123\t"],
)
def test_surrounding_whitespace_is_stripped_from_credentials(padded):
    """A pasted value carries whatever the dashboard or shell attached
    to it. `openssl rand -hex 32` ends in a newline and a Windows
    clipboard adds CR; neither is part of the credential."""
    settings = _settings(TELEGRAM_SECRET_TOKEN=padded, OPENROUTER_API_KEY=padded)
    assert settings.TELEGRAM_SECRET_TOKEN == "abc123"
    assert settings.OPENROUTER_API_KEY == "abc123"


# A deliberately synthetic stand-in for the value that caused the
# production failure: 64 hex characters, the shape `openssl rand -hex 32`
# produces, carrying the trailing space that broke the charset check. The
# repeating pattern keeps it under every secret scanner's entropy
# threshold -- the previous fixture was random-looking hex and gitleaks
# reported it as a live credential, which is noise a manual scan cannot
# afford.
PADDED_HEX_SECRET = "deadbeef" * 8 + " "


def test_a_whitespace_padded_secret_token_is_accepted():
    """The exact production failure: a 64-char hex secret rejected as
    outside Telegram's charset because a space rode along with it."""
    check_runtime_settings(
        _settings(
            MODE="webhook",
            TELEGRAM_SECRET_TOKEN=PADDED_HEX_SECRET,
            PUBLIC_URL="https://x.up.railway.app",
        )
    )


def test_database_url_scheme_rewrite_still_runs_after_stripping():
    """The strip validator is mode="before", so it must not displace the
    asyncpg rewrite, which compares a prefix."""
    settings = _settings(DATABASE_URL="  postgresql://u:p@h/db\n")
    assert settings.DATABASE_URL == "postgresql+asyncpg://u:p@h/db"


def test_a_genuinely_malformed_secret_token_is_rejected():
    with pytest.raises(SystemExit, match="Telegram's charset"):
        check_runtime_settings(
            _settings(
                MODE="webhook",
                TELEGRAM_SECRET_TOKEN="abc\ndef",  # inner break survives stripping
                PUBLIC_URL="https://x.up.railway.app",
            )
        )


def test_rejecting_a_malformed_secret_token_never_prints_it():
    """A pydantic field validator rendered the offending value into the
    error, which put a live webhook secret into the deploy logs. Nothing
    that validates a credential may echo it."""
    secret = "1ddda22978cf\n2059532d1316e5506f267f5934e49045d8f"

    with pytest.raises(SystemExit) as excinfo:
        check_runtime_settings(
            _settings(MODE="webhook", TELEGRAM_SECRET_TOKEN=secret, PUBLIC_URL="https://x.up.railway.app")
        )

    message = str(excinfo.value)
    assert "1ddda22978cf" not in message
    assert "2059532d1316" not in message


def test_constructing_settings_with_a_malformed_secret_no_longer_raises():
    """Settings() itself must stay quiet: it is built all over the suite,
    and a raising validator is also what leaked the value."""
    assert Settings(TELEGRAM_SECRET_TOKEN="abc\ndef").TELEGRAM_SECRET_TOKEN == "abc\ndef"


# --- 4a: the research loop ---


def test_research_is_off_by_default():
    """The master switch ships false and is flipped by hand after 4d.

    Every other research setting has a working default, so this one flag
    is the only thing standing between a fresh deploy and a bot that
    reaches the open web.
    """
    assert Settings(_env_file=None).RESEARCH_ENABLED is False


def test_packets_parse_a_bare_comma_list_from_the_environment():
    """The plan writes these as `PACKET_REF=a.org,b.org`, and a
    tuple-typed field read from env would otherwise need JSON syntax."""
    settings = Settings(_env_file=None, PACKET_REF="ru.wikipedia.org, en.wikipedia.org")
    assert settings.PACKET_REF == ("ru.wikipedia.org", "en.wikipedia.org")


def test_packet_entries_are_lowercased_and_deduped():
    """A duplicate would silently shrink the packet; a capitalised entry
    would never match, because the fetcher compares lowercased hosts."""
    settings = Settings(_env_file=None, PACKET_GUIDES="Example.COM, example.com , b.org")
    assert settings.PACKET_GUIDES == ("example.com", "b.org")


def test_the_guides_packet_is_empty_by_default():
    """Choosing those domains is the user's call, not a default we
    invent. /study guides refuses until it is set."""
    assert Settings(_env_file=None).PACKET_GUIDES == ()


@pytest.mark.parametrize(
    "value,reason",
    [
        ("https://reddit.com", "a scheme"),
        ("reddit.com/r/anchor", "a path -- suffix matching would admit the whole site"),
        ("reddit.com:443", "a port"),
        ("localhost", "not a domain"),
    ],
)
def test_a_packet_entry_that_is_not_a_bare_domain_is_refused(value, reason):
    """Refused rather than stripped. `https://reddit.com/r/x` in a packet
    means somebody expected path filtering, and quietly turning it into
    `reddit.com` would admit far more than they asked for."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PACKET_GUIDES=value)


def test_the_guides_packet_is_capped_at_five_domains():
    """Plan section 3's cap, on the one packet the user fills. An open
    slot with no ceiling is how a packet becomes "the web"."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PACKET_GUIDES="a.com,b.com,c.com,d.com,e.com,f.com")
    assert len(Settings(_env_file=None, PACKET_GUIDES="a.com,b.com,c.com,d.com,e.com").PACKET_GUIDES) == 5


# --- 5a: voice, mood and nicknames ---


def test_voice_and_nickname_defaults_match_the_plan():
    settings = Settings(_env_file=None)
    assert settings.NICKNAMES_FILE == "persona/nicknames.txt"
    assert settings.NICKNAME_RATE == 0.5
    assert settings.VOICE_FILE == "persona/voice.md"
    assert settings.VOICE_PER_SCENE == 4


@pytest.mark.parametrize("value", [0.0, 1.0, 0.5])
def test_nickname_rate_accepts_the_closed_unit_interval(value):
    assert Settings(_env_file=None, NICKNAME_RATE=value).NICKNAME_RATE == value


@pytest.mark.parametrize("value", [-0.01, 1.01, -1, 2])
def test_nickname_rate_rejects_outside_zero_to_one(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, NICKNAME_RATE=value)


def test_voice_per_scene_rejects_negative():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, VOICE_PER_SCENE=-1)


def test_voice_per_scene_accepts_zero():
    assert Settings(_env_file=None, VOICE_PER_SCENE=0).VOICE_PER_SCENE == 0


# --- 5b: the notebook ---


def test_notebook_defaults_match_the_plan():
    settings = Settings(_env_file=None)
    assert settings.NOTEBOOK_MAX_INTENTIONS == 4
    assert settings.NOTEBOOK_MAX_OBSERVATIONS == 4
    assert settings.NOTEBOOK_MAX_THREADS == 6
    assert settings.NOTEBOOK_THREAD_TTL_DAYS == 21


@pytest.mark.parametrize(
    "field",
    [
        "NOTEBOOK_MAX_INTENTIONS",
        "NOTEBOOK_MAX_OBSERVATIONS",
        "NOTEBOOK_MAX_THREADS",
        "NOTEBOOK_THREAD_TTL_DAYS",
    ],
)
def test_notebook_settings_reject_below_one(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


@pytest.mark.parametrize(
    "field",
    [
        "NOTEBOOK_MAX_INTENTIONS",
        "NOTEBOOK_MAX_OBSERVATIONS",
        "NOTEBOOK_MAX_THREADS",
        "NOTEBOOK_THREAD_TTL_DAYS",
    ],
)
def test_notebook_settings_accept_one(field):
    assert getattr(Settings(_env_file=None, **{field: 1}), field) == 1


# --- 5c: standing orders ---


def test_orders_defaults_match_the_plan():
    settings = Settings(_env_file=None)
    assert settings.ORDERS_MAX_ACTIVE == 5
    assert settings.ORDERS_IN_CHECKIN_MAX == 3


@pytest.mark.parametrize("field", ["ORDERS_MAX_ACTIVE", "ORDERS_IN_CHECKIN_MAX"])
def test_orders_settings_reject_below_one(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


@pytest.mark.parametrize("field", ["ORDERS_MAX_ACTIVE", "ORDERS_IN_CHECKIN_MAX"])
def test_orders_settings_accept_one(field):
    assert getattr(Settings(_env_file=None, **{field: 1}), field) == 1


def test_the_user_agent_names_us_and_no_browser():
    """Plan section 5.9 forbids spoofing this. A default that already
    looked like a browser would make that rule a formality."""
    agent = Settings(_env_file=None).FETCH_USER_AGENT
    assert agent.startswith("AnchorBot/")
    for browser in ("Mozilla", "Chrome", "Safari", "AppleWebKit", "Gecko"):
        assert browser not in agent
