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
