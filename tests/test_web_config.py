"""app/config.py's web-chat additions (web-chat plan track 2, design
section 9).

- the web UI is off by default, and nothing about it is required then
- WEB_UI_ENABLED=true with MODE=polling exits
- WEB_UI_ENABLED=true with a non-https, non-localhost PUBLIC_URL exits
- http://localhost and http://127.0.0.1 are allowed for dev
- a missing or malformed WEB_PASSPHRASE_HASH exits, and the exit
  message never contains the (would-be) value
- WEB_PASSPHRASE_HASH is stripped of surrounding whitespace, like every
  other pasted credential
"""

from __future__ import annotations

import pytest

from app.config import Settings, check_runtime_settings
from scripts.web_passphrase import make_hash

REQUIRED = dict(
    TELEGRAM_BOT_TOKEN="123456:TEST",
    DATABASE_URL="postgresql+asyncpg://a/b",
    OPENROUTER_API_KEY="key",
    ALLOWED_CHAT_ID=1,
)

VALID_HASH = make_hash("correct horse battery staple")


def _settings(**overrides) -> Settings:
    base = dict(REQUIRED)
    base.update(overrides)
    return Settings(**base)


def test_web_ui_disabled_by_default():
    settings = _settings(MODE="polling")
    assert settings.WEB_UI_ENABLED is False
    check_runtime_settings(settings)  # must not raise


def test_web_ui_disabled_ignores_missing_hash_even_over_http():
    settings = _settings(MODE="webhook", PUBLIC_URL="http://example.com", TELEGRAM_SECRET_TOKEN="s")
    check_runtime_settings(settings)  # WEB_UI_ENABLED=False -> none of the web checks run


def test_enabled_with_polling_mode_exits():
    settings = _settings(MODE="polling", WEB_UI_ENABLED=True, WEB_PASSPHRASE_HASH=VALID_HASH)
    with pytest.raises(SystemExit, match="MODE=webhook"):
        check_runtime_settings(settings)


def test_enabled_with_plain_http_exits():
    settings = _settings(
        MODE="webhook",
        WEB_UI_ENABLED=True,
        PUBLIC_URL="http://example.com",
        TELEGRAM_SECRET_TOKEN="s",
        WEB_PASSPHRASE_HASH=VALID_HASH,
    )
    with pytest.raises(SystemExit, match="https"):
        check_runtime_settings(settings)


@pytest.mark.parametrize("host", ["http://localhost", "http://localhost:8080", "http://127.0.0.1:8080"])
def test_enabled_with_localhost_is_permitted(host):
    settings = _settings(
        MODE="webhook",
        WEB_UI_ENABLED=True,
        PUBLIC_URL=host,
        TELEGRAM_SECRET_TOKEN="s",
        WEB_PASSPHRASE_HASH=VALID_HASH,
    )
    check_runtime_settings(settings)  # must not raise


def test_localhost_lookalike_host_is_not_permitted():
    """http://localhost.evil.example must not pass a prefix-only check."""
    settings = _settings(
        MODE="webhook",
        WEB_UI_ENABLED=True,
        PUBLIC_URL="http://localhost.evil.example",
        TELEGRAM_SECRET_TOKEN="s",
        WEB_PASSPHRASE_HASH=VALID_HASH,
    )
    with pytest.raises(SystemExit, match="https"):
        check_runtime_settings(settings)


def test_enabled_with_https_and_valid_hash_passes():
    settings = _settings(
        MODE="webhook",
        WEB_UI_ENABLED=True,
        PUBLIC_URL="https://anchor.example.com",
        TELEGRAM_SECRET_TOKEN="s",
        WEB_PASSPHRASE_HASH=VALID_HASH,
    )
    check_runtime_settings(settings)  # must not raise


def test_missing_hash_exits():
    settings = _settings(
        MODE="webhook", WEB_UI_ENABLED=True, PUBLIC_URL="https://x.example", TELEGRAM_SECRET_TOKEN="s"
    )
    with pytest.raises(SystemExit, match="WEB_PASSPHRASE_HASH"):
        check_runtime_settings(settings)


@pytest.mark.parametrize(
    "bad_hash",
    [
        "not-a-hash-at-all",
        "scrypt$16$8$1$c2FsdA$aGFzaA",  # wrong N exponent
        "scrypt$17$8$1$onlyonesegment",
        "md5$deadbeef",
        "",
    ],
)
def test_malformed_hash_exits_and_never_echoes_the_value(bad_hash):
    settings = _settings(
        MODE="webhook",
        WEB_UI_ENABLED=True,
        PUBLIC_URL="https://x.example",
        TELEGRAM_SECRET_TOKEN="s",
        WEB_PASSPHRASE_HASH=bad_hash,
    )
    with pytest.raises(SystemExit) as excinfo:
        check_runtime_settings(settings)
    message = str(excinfo.value)
    assert "WEB_PASSPHRASE_HASH" in message
    if bad_hash:
        assert bad_hash not in message


def test_hash_whitespace_is_stripped():
    """Copy-pasted into a dashboard field, a trailing newline/space must
    not turn a valid hash into a malformed one."""
    settings = _settings(WEB_PASSPHRASE_HASH=f"  {VALID_HASH}\n")
    assert settings.WEB_PASSPHRASE_HASH == VALID_HASH


def test_session_and_code_defaults():
    settings = _settings()
    assert settings.WEB_SESSION_IDLE_HOURS == 72
    assert settings.WEB_SESSION_MAX_DAYS == 14
    assert settings.WEB_LOGIN_CODE_TTL_S == 300
