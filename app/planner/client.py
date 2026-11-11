"""A small hand-written MCP client: JSON-RPC 2.0 over aiohttp.

Not the `mcp` Python SDK. The SDK depends on `httpx`, and this repo's
lockfile carries `httpx2` (a transitive dependency of `openai`) with no
plain `httpx` anywhere in it -- adding the SDK would pull in a second,
overlapping HTTP stack for one small client. `aiohttp` is already
present (aiogram depends on it), so this file uses that instead. See
the design review for the verification (`uv.lock` has no `httpx`).

Speaks streamable-HTTP MCP against the planner's `/api/mcp` endpoint:
POST JSON-RPC bodies, `Authorization: Bearer <access token>`, `Accept:
application/json, text/event-stream`, and once a session exists,
`Mcp-Session-Id` plus `MCP-Protocol-Version` on every request after
`initialize`. `protocolVersion: "2025-06-18"` in the `initialize` call,
which the planner's `@modelcontextprotocol/sdk@1.29.0` accepts (see the
design review's P0 spike).

`ALLOWED_TOOLS` is enforced here, not only by the planner's own RLS: a
future caller that tries `update_event` (which can edit the partner's
joint events) or any `delete_*` tool fails fast and loud, in Anchor's
own process, rather than depending on the planner never adding a tool
this client should not use. `update_task` is on the list for board sync
only, and its wrapper can send a title, a due date, tags, the task's
blockers, its state and column, and its collection, and nothing else
(see the comment on ALLOWED_TOOLS); `create_collection` is on it for
board sync too.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.planner.auth import PlannerUnavailable, force_expire, get_access_token

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "anchor", "version": "1.0"}

# 5s: this client sits on the PLANNER_SYNC job path (and, later, the
# write path), never on the chat-turn path itself -- a chat turn only
# ever reads the local snapshot (see app/planner/snapshot.py). A slow
# planner therefore costs one job retry, not a delayed reply.
_TIMEOUT = aiohttp.ClientTimeout(total=5)

# A task's state as the planner's `list_tasks` rows and `update_task`
# carry it (board sync: a card's lane, by role). `done` is the same as
# the row's `done`.
TASK_STATES = ("planned", "wip", "done")

# Plan section 2.3 left out every update and delete tool. The reason was
# update_event: it can edit the partner's own joint events (design
# review, table 1), and in P2-P4 nothing needed to change an item once
# made -- `/done` uses complete_task, which only flips a task's done
# state -- so a generic edit tool was a risk with no use.
#
# Board sync (app/planner/board_job.py) needs one: keeping a board card
# and a planner task in step means carrying a renamed card's title, a
# moved due date and a removed board tag to the task, and a card's
# `after:` line to the task's blockers (`blockedBy`, the whole set), and
# the card's lane (by role) to the task's state. So update_task is allowed
# now, and only through `update_task` below, which can send nothing but a
# task's title, due date, tags, blockers and state; and
# the board job only ever calls it on tasks it read with
# `list_tasks(mine=True)`, the user's own, subtasks included (the
# planner's `update_task` is RLS-scoped, but RLS lets a member edit the
# partner's shared tasks, so `mine` is this side's own line). Subtasks
# need no new tool: create_task takes a `parentId`, update_task and
# complete_task work on a subtask as on any task. update_event and every
# delete_* tool stay out: a card that leaves a board takes the board's
# tag off its task, never the task, and a subtask that leaves a card is
# let go of (its pair is detached), never deleted.
#
#
# Board sync also keeps one planner collection per board (docs/decisions.md,
# "Collection per board and columns by name"): a task in no collection has
# no column, so the planner can record neither its lane nor its completion.
# So `create_collection` is allowed, for board sync only, through
# `create_collection` below: a name and its columns, nothing else. It is
# idempotent by name, so a retried pass makes nothing twice, and it only
# adds: there is still no tool here that deletes or renames anything.
# `update_task` may now also carry a task's `collectionId` (a task the
# board sync paired that sits in no collection, moved into its board's)
# and a `column` by name (the card's lane of that name). `get_workspace`
# (read-only) gives the collections and their columns.
#
# Anchor plan, "Anchor" section: get_health, read-only, caller's own
# data only (same as get_agenda). No get_sleep_summary -- get_health's
# `sleep` field is enough for the one health line render_lines() adds.
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "get_agenda",
        "list_tasks",
        "get_workspace",
        "create_task",
        "create_event",
        "complete_task",
        # Board sync: a task's title, due date, tags, blockers, state,
        # column and collection only (above).
        "update_task",
        # Board sync: one collection per board, made once (above).
        "create_collection",
        "get_health",
        # PLANNER_FLOWS: read-only, the caller's own tasks grouped into
        # streams with an attention score.
        "get_flows",
    }
)

# update_task's "leave the due date as it is", distinct from None ("clear it").
UNSET = object()

FLOWS_GROUP_BY = frozenset({"tag", "category", "collection", "assignee", "status", "priority"})
FLOWS_ATTENTION = frozenset({"deadline", "priority", "combined"})
FLOWS_LIMIT_MAX = 10


class PlannerToolError(Exception):
    """A JSON-RPC error came back, the tool is not allowlisted, or the
    response shape was not what an MCP tool call returns."""


class PlannerToolRefused(PlannerToolError):
    """The tool ran and said no (`isError: true`), e.g. `update_task`
    refusing blockers that would make a cycle. A PlannerToolError like any
    other, for every caller that does not tell the two apart."""


class PlannerToolUnknown(PlannerToolRefused):
    """The planner has no tool of that name (an older planner): the MCP
    SDK answers `isError: true` with "Tool <name> not found". A refusal
    like any other, for every caller that does not tell the two apart;
    board sync reads it as "collections not supported yet"."""


@dataclass(frozen=True)
class Column:
    """One column of a planner collection, as `get_workspace` and
    `create_collection` report it. `done`: the collection's done column."""

    id: str
    name: str
    done: bool
    position: float = 0


@dataclass(frozen=True)
class Collection:
    """A planner collection with its columns. `id` is "" for one a board
    sync preview would create (it never is)."""

    id: str
    name: str
    columns: tuple[Column, ...] = ()


def _parse_column(raw: object) -> Column:
    if not isinstance(raw, dict):
        raise PlannerToolError("malformed column")
    col_id, name, done, position = raw.get("id"), raw.get("name"), raw.get("done"), raw.get("position", 0)
    if not (isinstance(col_id, str) and col_id and isinstance(name, str) and isinstance(done, bool)):
        raise PlannerToolError("malformed column")
    if isinstance(position, bool) or not isinstance(position, (int, float)):
        raise PlannerToolError("malformed column position")
    return Column(col_id, name, done, float(position))


def _parse_collection(raw: object) -> Collection:
    """A collection with its columns, strictly."""
    if not isinstance(raw, dict):
        raise PlannerToolError("malformed collection")
    col_id, name, columns = raw.get("id"), raw.get("name"), raw.get("columns")
    if not (isinstance(col_id, str) and col_id and isinstance(name, str) and isinstance(columns, list)):
        raise PlannerToolError("malformed collection")
    parsed = tuple(_parse_column(c) for c in columns)
    if len({c.id for c in parsed}) != len(parsed):
        raise PlannerToolError("malformed collection: a column twice")
    return Collection(col_id, name, parsed)


def parse_collections(raw: object) -> tuple[Collection, ...] | None:
    """`get_workspace`'s collections, strictly: a collection of the wrong
    shape is a planner failure. None when any collection carries no
    `columns` key: a planner from before collections were synced, with
    which board sync keeps to tasks with no collection, as before."""
    collections = raw.get("collections") if isinstance(raw, dict) else None
    if not isinstance(collections, list):
        raise PlannerToolError("get_workspace: malformed answer")
    if any(isinstance(c, dict) and "columns" not in c for c in collections):
        return None
    out = tuple(_parse_collection(c) for c in collections)
    if len({c.id for c in out}) != len(out):
        raise PlannerToolError("get_workspace: a collection twice")
    return out


def _parse_body(raw: str) -> dict | None:
    """A JSON-RPC response body, whether sent as plain JSON or as a
    single SSE `data:` frame. None if neither parses."""
    raw = raw.strip()
    if not raw:
        return None
    if raw[0] == "{":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                return None
    return None


class PlannerClient:
    """One client per running process, reused across calls.

    The `aiohttp.ClientSession` is owned by the caller (app/main.py
    builds and closes it, mirroring how app/llm/openrouter.py's shared
    AsyncOpenAI client works) -- this class never opens or closes its
    own connection pool.
    """

    def __init__(self, http: aiohttp.ClientSession, base_url: str) -> None:
        self._http = http
        self._url = base_url
        self._session_id: str | None = None
        self._initialized = False
        self._next_id = 1
        self._init_lock = asyncio.Lock()

    def _id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _headers(self, token: str, *, with_session: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if with_session and self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    async def _post(
        self, body: dict, *, token: str, with_session: bool = True
    ) -> tuple[int, dict | None, str | None]:
        headers = self._headers(token, with_session=with_session)
        try:
            async with self._http.post(
                self._url, json=body, headers=headers, timeout=_TIMEOUT
            ) as resp:
                session_id = resp.headers.get("Mcp-Session-Id")
                raw = await resp.text()
                return resp.status, _parse_body(raw), session_id
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise PlannerUnavailable(type(exc).__name__) from exc

    async def _do_initialize(self, token: str) -> None:
        status, payload, session_id = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            },
            token=token,
            with_session=False,
        )
        if status >= 400 or payload is None or "result" not in payload:
            raise PlannerUnavailable(f"initialize failed: status {status}")
        self._session_id = session_id
        # Notification: no "id", no response expected.
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token=token)
        self._initialized = True

    async def _ensure_initialized(self, token: str) -> None:
        async with self._init_lock:
            if not self._initialized:
                await self._do_initialize(token)

    async def call_tool(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        name: str,
        arguments: dict[str, Any],
    ) -> dict:
        """Call one MCP tool; returns its parsed JSON text content.

        Re-initializes once and retries on a 400/404 (session lost --
        the planner's Redis session store expired or was cycled), per
        the design review's client spec.
        """
        if name not in ALLOWED_TOOLS:
            raise PlannerToolError(f"tool not allowed: {name}")

        token = await get_access_token(session, settings, clock)
        await self._ensure_initialized(token)

        call_body = {
            "jsonrpc": "2.0",
            "id": self._id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        status, payload, _ = await self._post(call_body, token=token)
        if status == 401:
            # The cached token was rejected even though our local
            # expires_at said it was still good (early revocation or
            # rotation on the planner side). Force a refresh and retry
            # exactly once, rather than retrying with the same stale
            # token until it locally "expires" and burning the whole
            # write job's retry budget on our own bookkeeping being wrong.
            await force_expire(session, clock)
            token = await get_access_token(session, settings, clock)
            call_body["id"] = self._id()
            status, payload, _ = await self._post(call_body, token=token)

        if status in (400, 404):
            self._initialized = False
            await self._ensure_initialized(token)
            call_body["id"] = self._id()
            status, payload, _ = await self._post(call_body, token=token)

        if status == 401:
            raise PlannerToolError("planner rejected the access token (401)")
        if payload is None:
            raise PlannerUnavailable(f"unparseable response: status {status}")
        if "error" in payload:
            error = payload["error"] or {}
            raise PlannerToolError(f"tool error: code {error.get('code')}")

        result = payload.get("result")
        if not isinstance(result, dict):
            raise PlannerToolError("malformed tool result")
        text = next(
            (item.get("text") for item in result.get("content", []) if item.get("type") == "text"),
            None,
        )
        if result.get("isError") is True:
            # The text is the planner's own words: never logged, never kept.
            if text == f"Tool {name} not found":
                raise PlannerToolUnknown("no such tool")
            raise PlannerToolRefused("tool refused the call")
        if text is None:
            raise PlannerToolError("tool result had no text content")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlannerToolError("tool result text was not JSON") from exc

    async def get_agenda(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        date: str,
        timezone: str,
        days: int = 1,
        partner: str = "shared",
    ) -> dict:
        return await self.call_tool(
            settings,
            session,
            clock,
            "get_agenda",
            {"date": date, "timeZone": timezone, "days": days, "partner": partner},
        )

    async def get_health(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        date: str,
        days: int = 1,
    ) -> dict:
        # PLANNER_HEALTH: called once per sync (app/planner/snapshot.py),
        # never per-turn -- days=14 covers "today" plus the baseline
        # window snapshot.py's median needs.
        return await self.call_tool(
            settings, session, clock, "get_health", {"date": date, "days": days}
        )

    async def get_flows(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        date: str,
        group_by: str | None = None,
        stream: str | None = None,
        attention: str | None = None,
        mine: bool | None = None,
        limit: int | None = None,
    ) -> dict:
        # PLANNER_FLOWS: called once per sync (app/planner/snapshot.py),
        # never per-turn. Every optional argument is omitted when None so
        # the planner's own defaults (groupBy "tag", attention
        # "combined", limit 3) apply; `limit` is clamped to the 1..10
        # the planner accepts rather than risking a rejected call.
        arguments: dict[str, Any] = {"date": date}
        if group_by is not None:
            if group_by not in FLOWS_GROUP_BY:
                raise ValueError(f"unknown groupBy: {group_by}")
            arguments["groupBy"] = group_by
        if stream:
            arguments["stream"] = stream
        if attention is not None:
            if attention not in FLOWS_ATTENTION:
                raise ValueError(f"unknown attention: {attention}")
            arguments["attention"] = attention
        if mine is not None:
            arguments["mine"] = bool(mine)
        if limit is not None:
            arguments["limit"] = max(1, min(FLOWS_LIMIT_MAX, int(limit)))
        return await self.call_tool(settings, session, clock, "get_flows", arguments)

    # --- P3: writes (app/planner/jobs.py's PLANNER_WRITE job) -------------
    #
    # Each of the three wraps call_tool the same way get_agenda does --
    # ALLOWED_TOOLS is still the enforcement point, this is just the
    # typed call site a caller does not have to hand-build.

    async def create_task(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        title: str,
        due_date: str | None,
        client_request_id: str,
        tags: list[str] | None = None,
        parent_id: str | None = None,
        blocked_by: list[str] | None = None,
        collection_id: str | None = None,
    ) -> dict:
        """A task, or with `parent_id` a subtask of that task (it takes its
        parent's collection), optionally blocked by `blocked_by`; with
        `collection_id`, made in that collection (its first open column)."""
        # The planner's schema is `dueDate: z.string().optional()`, which
        # rejects an explicit null -- omit the key entirely rather than
        # sending `"dueDate": null` for a dateless task.
        arguments: dict[str, Any] = {"title": title, "clientRequestId": client_request_id}
        if due_date is not None:
            arguments["dueDate"] = due_date
        # `tags` is optional on the planner side too; omit the key when
        # there are none rather than sending an empty list.
        if tags:
            arguments["tags"] = list(tags)
        if parent_id is not None:
            arguments["parentId"] = parent_id
        if blocked_by:
            arguments["blockedBy"] = list(blocked_by)
        if collection_id is not None:
            if not collection_id:
                raise ValueError("an empty collection id")
            arguments["collectionId"] = collection_id
        return await self.call_tool(settings, session, clock, "create_task", arguments)

    async def create_event(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        title: str,
        start: str,
        end: str,
        all_day: bool,
        is_private: bool,
        client_request_id: str,
        timezone: str,
    ) -> dict:
        # create_event has no timeZone default that means "the user's
        # zone" -- an omitted timeZone defaults to UTC on the planner
        # side (tools.ts:198), so it must always be sent explicitly.
        return await self.call_tool(
            settings,
            session,
            clock,
            "create_event",
            {
                "title": title,
                "start": start,
                "end": end,
                "allDay": all_day,
                "isPrivate": is_private,
                "clientRequestId": client_request_id,
                "timeZone": timezone,
            },
        )

    async def complete_task(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        task_id: str,
        done: bool | None = None,
    ) -> dict:
        # `done` omitted is the planner's default (complete); board sync
        # sends it both ways, since an unchecked card reopens its task.
        arguments: dict[str, Any] = {"id": task_id}
        if done is not None:
            arguments["done"] = bool(done)
        return await self.call_tool(settings, session, clock, "complete_task", arguments)

    # --- Board sync (app/planner/board_job.py) ----------------------------

    async def list_tasks(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        tag: str | None,
        mine: bool = True,
        include_subtasks: bool = False,
        include_completed: bool = True,
    ) -> dict:
        """The tasks carrying `tag` (None: any tags or none): by default
        the user's own (`mine`), top-level ones, done or not. Rows are
        `{id, title, done, tags?, dueDate?, parentId?, position, blockedBy?,
        ...}`; the caller parses them. Subtasks carry no tags, so
        `include_subtasks` with a tag filter does not find a tagged
        task's subtasks: board sync lists with no tag."""
        args: dict = {
            "mine": bool(mine),
            "includeSubtasks": bool(include_subtasks),
            "includeCompleted": bool(include_completed),
        }
        if tag is not None:
            args = {"tag": tag, **args}
        return await self.call_tool(settings, session, clock, "list_tasks", args)

    async def update_task(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        task_id: str,
        title: str | None = None,
        due_date: str | None | object = UNSET,
        tags: list[str] | None = None,
        blocked_by: list[str] | None = None,
        state: str | None = None,
        column: str | None = None,
        collection_id: str | None = None,
    ) -> dict:
        """Change a task's title, due date (None clears it), tags (the
        whole list), blockers (`blocked_by`, the whole set; None leaves
        them), state (`planned`, `wip` or `done`; None leaves it: a card's
        lane, by role), column (`column`, a column name in the task's
        collection: a card's lane by name; with `state`, a column of that
        role) or collection (`collection_id`: the task, and its subtasks,
        moved into it), and nothing else: there is no argument here for any
        other field update_task accepts (ALLOWED_TOOLS' comment). Blockers
        that would make a cycle are PlannerToolRefused."""
        arguments: dict[str, Any] = {"id": task_id}
        if title is not None:
            arguments["title"] = title
        if due_date is not UNSET:
            arguments["dueDate"] = due_date
        if tags is not None:
            arguments["tags"] = list(tags)
        if blocked_by is not None:
            arguments["blockedBy"] = list(blocked_by)
        if state is not None:
            if state not in TASK_STATES:
                raise ValueError(f"not a task state: {state}")
            arguments["state"] = state
        if column is not None:
            if not (isinstance(column, str) and column.strip()):
                raise ValueError("an empty column name")
            arguments["column"] = column
        if collection_id is not None:
            if not (isinstance(collection_id, str) and collection_id):
                raise ValueError("an empty collection id")
            arguments["collectionId"] = collection_id
        if len(arguments) == 1:
            raise ValueError("update_task needs a field to change")
        return await self.call_tool(settings, session, clock, "update_task", arguments)

    async def get_workspace(
        self, settings: Settings, session: AsyncSession, clock: Clock
    ) -> tuple[Collection, ...] | None:
        """The workspace's collections with their columns, parsed strictly
        (`parse_collections`; None: a planner whose collections carry no
        columns). The rest of the answer is not read."""
        raw = await self.call_tool(settings, session, clock, "get_workspace", {})
        return parse_collections(raw)

    async def create_collection(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        name: str,
        columns: list[tuple[str, bool]],
    ) -> tuple[Collection, bool]:
        """A collection named `name` with `columns` ((name, done) each, in
        order, exactly one done), or the one already of that name (the
        planner matches names case-insensitively). Returns it, with its
        columns, and whether this call made it. An older planner with no
        such tool is PlannerToolUnknown."""
        if not (isinstance(name, str) and name.strip()):
            raise ValueError("a collection needs a name")
        if not columns or sum(1 for _n, done in columns if done) != 1:
            raise ValueError("a collection needs exactly one done column")
        if any(not (isinstance(n, str) and n.strip()) for n, _done in columns):
            raise ValueError("a column needs a name")
        arguments = {"name": name, "columns": [{"name": n, "done": bool(done)} for n, done in columns]}
        raw = await self.call_tool(settings, session, clock, "create_collection", arguments)
        created = raw.get("created") if isinstance(raw, dict) else None
        if not isinstance(created, bool):
            raise PlannerToolError("create_collection: malformed answer")
        return _parse_collection(raw), created


def build_planner_client(settings: Settings, http: aiohttp.ClientSession) -> PlannerClient:
    """Constructed once in app/main.py, shared across the worker's jobs."""
    return PlannerClient(http, settings.PLANNER_MCP_URL)


__all__ = [
    "PROTOCOL_VERSION",
    "ALLOWED_TOOLS",
    "PlannerClient",
    "PlannerUnavailable",
    "PlannerToolError",
    "PlannerToolRefused",
    "PlannerToolUnknown",
    "Collection",
    "Column",
    "parse_collections",
    "TASK_STATES",
    "build_planner_client",
]
