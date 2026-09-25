#!/usr/bin/env python3
"""PreToolUse guard: keep Claude Code away from the conversation data.

Anchor's dialogs, memory and journal live only in the production
Postgres. Claude may debug through logs, deployment status and the
content-free `debug.*` views (via ANCHOR_DEBUG_DATABASE_URL), but must
never reach the production DATABASE_URL, the bot token (Telegram's
getUpdates returns message text) or the local .env that holds both.

This is a guardrail, not a sandbox: the hard boundary is that the
`anchor_debug` role cannot select from any public table. See
docs/claude-access.md.

Protocol: the tool call arrives as JSON on stdin; exit code 2 blocks it
and stderr is shown to Claude as the reason.
"""

from __future__ import annotations

import json
import re
import sys

SECRET_NAMES = (
    "DATABASE_URL",
    "ANCHOR_ADMIN_DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_SECRET_TOKEN",
    "OPENROUTER_API_KEY",
    # Phase 8 (plan section 11): the bot's one vault credential, and the
    # two Obsidian ones that live only on the vault service.
    "VAULT_API_TOKEN",
    "OBSIDIAN_AUTH_TOKEN",
    "OBSIDIAN_E2EE_PASSWORD",
)
_SECRET = "|".join(SECRET_NAMES)
# A secret read out of the environment: $NAME, ${NAME}, os.environ["NAME"],
# getenv("NAME"), printenv NAME. The (?<![A-Z0-9_]) keeps
# ANCHOR_DEBUG_DATABASE_URL from matching DATABASE_URL.
SECRET_REF = re.compile(
    rf"(?<![A-Za-z0-9_])(?:\$\{{?|printenv\s+|environ(?:\.get)?\(?\[?\s*['\"]|getenv\(\s*['\"])"
    rf"(?:{_SECRET})(?![A-Za-z0-9_])"
)
ENV_DUMP = re.compile(r"(?:^|[;&|(]\s*)(?:printenv|env|export\s+-p|set)\s*(?:$|[;&|>)])")
TELEGRAM_API = re.compile(r"api\.telegram\.org", re.IGNORECASE)
RAILWAY_CLI = re.compile(r"\brailway\s+(?:variables|vars|run|connect|shell|ssh)\b")
RAILWAY_HOSTS = re.compile(r"(?:rlwy\.net|railway\.internal|railway\.app)", re.IGNORECASE)
PG_URL_HOST = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s'\"@/]*@\[?([^/:\s'\"\]]+)")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# A .env file, but not .env.example and not .venv.
DOTENV = re.compile(r"(?:^|[\s/'\"=<])\.env(?!\.example)(?:\.[\w-]+)?(?![\w.-])")

BLOCKED_TOOLS = {
    "mcp__Railway__list-variables": "shows DATABASE_URL and the bot token",
    "mcp__Railway__set-variables": "changes production secrets",
    "mcp__Railway__railway-agent": "unrestricted agent with access to variables and the DB",
    "mcp__Railway__create-tcp-proxy": "would expose the production DB",
    "mcp__Railway__get-function-source-code": "not needed for debugging",
    "mcp__Railway__update-function-source-code": "not needed for debugging",
    "mcp__Railway__deploy-template": "not needed for debugging",
}

HINT = (
    " Debug with Railway get-logs / deployment status, or query the content-free"
    ' views: psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select ... from debug.<table>".'
)


def check_command(command: str) -> str | None:
    """Return why a shell command is blocked, or None if it may run."""
    if SECRET_REF.search(command):
        return "reads a production secret from the environment"
    if ENV_DUMP.search(command):
        return "dumps the whole environment, which may hold production secrets"
    if TELEGRAM_API.search(command):
        return "calls the Telegram Bot API, which returns message text"
    if RAILWAY_CLI.search(command):
        return "Railway CLI command that exposes production variables or a shell"
    if RAILWAY_HOSTS.search(command):
        return "connects to a Railway host directly; use ANCHOR_DEBUG_DATABASE_URL"
    for host in PG_URL_HOST.findall(command):
        if host.lower() not in LOCAL_HOSTS:
            return "uses a non-local Postgres URL; use ANCHOR_DEBUG_DATABASE_URL"
    if DOTENV.search(command):
        return "touches .env, which holds production secrets"
    return None


def check(tool_name: str, tool_input: dict) -> str | None:
    """Return why a tool call is blocked, or None if it may run."""
    if tool_name in BLOCKED_TOOLS:
        return f"{tool_name} is blocked: {BLOCKED_TOOLS[tool_name]}"
    if tool_name == "Bash":
        reason = check_command(str(tool_input.get("command", "")))
        return f"command blocked: {reason}" if reason else None
    if tool_name == "WebFetch":
        if TELEGRAM_API.search(str(tool_input.get("url", ""))):
            return "WebFetch blocked: the Telegram Bot API returns message text"
    if tool_name in ("Read", "Grep", "Edit", "Write"):
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if DOTENV.search(" " + path):
            return f"{tool_name} blocked: .env holds production secrets"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    reason = check(event.get("tool_name", ""), event.get("tool_input") or {})
    if reason is None:
        return 0
    print(reason + "." + HINT, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
