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
