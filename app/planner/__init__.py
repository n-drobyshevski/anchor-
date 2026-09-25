"""Anchor's MCP client of the planner (P2: read path + OAuth link).

See the design review (`scratchpad/full-review.md` in the planning
session that produced this package) and its plan for the full picture.
Short version: the planner is a separate Next.js app with its own MCP
server; Anchor never gives the persona model tool access to it, and
every read/write is deterministic code calling a fixed MCP tool.

Submodules:
- `auth.py`    — OAuth 2.1 + PKCE against the planner's Supabase
                 authorization server; credential storage and rotation.
- `client.py`  — a small hand-written JSON-RPC 2.0 client over aiohttp.
- `snapshot.py`— the local cache (`planner_snapshot`) the chat-turn path
                 reads from, so a turn never waits on the network.
- `jobs.py`    — the `PLANNER_SYNC` job body (read path only in P2).

Nothing in `app/core/extract.py`, `app/core/scene.py` or
`app/core/memory.py` may import this package — see
`tests/test_planner_isolation.py`.
"""

from __future__ import annotations
