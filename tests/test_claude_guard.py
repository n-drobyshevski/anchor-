"""The Claude Code PreToolUse guard blocks paths to conversation data.

.claude/hooks/guard_private_data.py is not part of the app, but a
regex that silently stopped matching would be a leak nobody notices,
so its decisions are pinned here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "guard_private_data.py"

_spec = importlib.util.spec_from_file_location("guard_private_data", HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "command",
    [
        'psql "$DATABASE_URL" -c "select content from message"',
        "psql ${DATABASE_URL}",
        "python -c 'import os; print(os.environ[\"DATABASE_URL\"])'",
        "python -c 'import os; print(os.getenv(\"TELEGRAM_BOT_TOKEN\"))'",
        "echo $OPENROUTER_API_KEY",
        "printenv DATABASE_URL",
        "printenv",
        "env | grep URL",
        "curl https://api.telegram.org/bot123/getUpdates",
        "railway variables",
        "railway run python -m app.main",
        "railway connect Postgres",
        "psql postgresql://postgres:pw@monorail.proxy.rlwy.net:12345/railway",
        "psql -h postgres.railway.internal -U postgres",
        "python x.py postgresql+asyncpg://u:p@db.example.com/prod",
        "cat .env",
        "grep TOKEN ./.env",
        "source .env && python -m app.main",
        # Phase 8: the vault's three credentials.
        "echo $VAULT_API_TOKEN",
        'curl -H "Authorization: Bearer ${VAULT_API_TOKEN}" http://127.0.0.1:8080/v1/status',
        "printenv OBSIDIAN_AUTH_TOKEN",
        "python -c 'import os; print(os.environ[\"OBSIDIAN_E2EE_PASSWORD\"])'",
        "python -c 'import os; print(os.getenv(\"OBSIDIAN_AUTH_TOKEN\"))'",
    ],
)
def test_blocks(command):
    assert guard.check("Bash", {"command": command}) is not None


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest",
        "git status",
        'psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select status, count(*) from debug.job group by 1"',
        "DATABASE_URL=postgresql://anchor:anchor@127.0.0.1:5432/anchor_x uv run alembic upgrade head",
        "cat .env.example",
        "ls .venv/bin",
        "grep -rn DATABASE_URL app/config.py",
        "set -euo pipefail; echo ok",
        "env FOO=1 python -V",
        "grep -rn VAULT_API_TOKEN app/config.py",
        "uv run --directory vaultd pytest",
    ],
)
def test_allows(command):
    assert guard.check("Bash", {"command": command}) is None


def test_blocks_railway_tools_but_not_logs():
    assert guard.check("mcp__Railway__list-variables", {}) is not None
    assert guard.check("mcp__Railway__railway-agent", {"prompt": "x"}) is not None
    assert guard.check("mcp__Railway__get-logs", {"serviceId": "x"}) is None


def test_file_tools_and_webfetch():
    assert guard.check("Read", {"file_path": "/home/user/anchor-/.env"}) is not None
    assert guard.check("Read", {"file_path": "/home/user/anchor-/.env.example"}) is None
    assert guard.check("Read", {"file_path": "/home/user/anchor-/app/config.py"}) is None
    assert guard.check("WebFetch", {"url": "https://api.telegram.org/bot1/getMe"}) is not None
    assert guard.check("WebFetch", {"url": "https://docs.railway.com"}) is None


def test_hook_protocol_exit_code():
    def run(event: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True, text=True
        )

    blocked = run({"tool_name": "Bash", "tool_input": {"command": "railway variables"}})
    assert blocked.returncode == 2
    assert "ANCHOR_DEBUG_DATABASE_URL" in blocked.stderr

    allowed = run({"tool_name": "Bash", "tool_input": {"command": "uv run pytest"}})
    assert allowed.returncode == 0


# --- Anchor's connector and Railway's edge log (connector plan 6.3, 11.7) ---


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__Anchor__get_journal",
        "mcp__claude_ai_Anchor__get_journal",
        "mcp__anchor_2__get_memory",
        "mcp__Anchor__initialize_anything",
        "mcp__ANCHOR__get_state",
        # A connector renamed to something else is caught by its tools.
        "mcp__Renamed__get_dialogs",
        "mcp__claude_ai_My_Notes__search_library",
        # Any server whose name starts with "anchor": over-blocking an
        # unrelated one costs little, missing a renamed Anchor costs all.
        "mcp__anchorage__list",
    ],
)
def test_blocks_anchor_connector_tools(tool):
    assert guard.check(tool, {}) is not None


@pytest.mark.parametrize(
    "tool_input",
    [
        {"serviceId": "x", "types": ["http"]},
        {"serviceId": "x", "types": ["deploy", "http"]},
        {"serviceId": "x", "types": ["HTTP"]},
        {"serviceId": "x", "types": "http"},
    ],
)
def test_blocks_the_http_log_stream(tool_input):
    assert guard.check("mcp__Railway__get-logs", tool_input) is not None


@pytest.mark.parametrize(
    "tool, tool_input",
    [
        ("mcp__Railway__get-logs", {"serviceId": "x"}),
        ("mcp__Railway__get-logs", {"serviceId": "x", "types": ["deploy", "build"]}),
        ("mcp__Railway__get-logs", {"serviceId": "x", "types": ["network-flow", "dns"]}),
        ("mcp__Railway__http-requests", {"serviceId": "x"}),
        ("mcp__github__get_me", {}),
        ("mcp__Supabase__list_tables", {}),
    ],
)
def test_allows_other_mcp_tools(tool, tool_input):
    assert guard.check(tool, tool_input) is None


def test_malformed_mcp_input_is_blocked():
    assert guard.check("mcp__Railway__get-logs", ["types", "http"]) is not None
    assert guard.check("mcp__github__get_me", "x") is not None

    def run(raw: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HOOK)], input=raw, capture_output=True, text=True)

    assert run('{"tool_name": "mcp__Anchor__get_journal", "tool_input": ').returncode == 2
    assert run('["mcp__Railway__get-logs"]').returncode == 2
    assert run('{"tool_name": "mcp__Anchor__get_memory", "tool_input": []}').returncode == 2
    assert run('{"tool_name": "mcp__github__get_me"}').returncode == 0
    # Malformed input that is not an MCP call keeps the old behaviour.
    assert run("not json").returncode == 0
    assert run('{"tool_name": "Bash", "tool_input": []}').returncode == 0


def test_settings_deny_the_connector_and_route_every_mcp_tool_through_the_hook():
    settings = json.loads((HOOK.parent.parent / "settings.json").read_text())
    deny = settings["permissions"]["deny"]
    assert "mcp__Anchor" in deny and "mcp__claude_ai_Anchor" in deny
    matchers = [entry["matcher"] for entry in settings["hooks"]["PreToolUse"]]
    assert any("mcp__.*" in matcher.split("|") for matcher in matchers)
