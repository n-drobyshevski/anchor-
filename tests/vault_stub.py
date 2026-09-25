"""A stand-in for vaultd on 127.0.0.1, for the bot's tests.

Not vaultd itself: the bot and vaultd are separate projects and import
nothing from each other (tests/test_vault_isolation.py). This stub only
speaks the API's shapes, records every request it gets, and answers
whatever a test tells it to -- so a test can assert both what the bot
did with an answer and, as importantly, which requests it never made.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer

# Low-entropy, like every other test secret in this repo.
TOKEN = "vault-token-" + "v" * 32


@dataclass
class Recorded:
    method: str
    path: str
    query: dict
    authorization: str | None


@dataclass
class VaultStub:
    requests: list[Recorded] = field(default_factory=list)
    responses: dict[tuple[str, str], tuple[int, object]] = field(default_factory=dict)
    delay_s: float = 0.0
    url: str = ""

    def __post_init__(self) -> None:
        self.set_status(running=True)

    def set_status(
        self,
        *,
        running: bool = True,
        restarts: int = 0,
        last_exit_code: int | None = None,
        running_since: str | None = "2026-09-25T08:00:00+00:00",
    ) -> None:
        self.responses[("GET", "/v1/status")] = (
            200,
            {
                "sync_running": running,
                "restarts": restarts,
                "last_exit_code": last_exit_code,
                "running_since": running_since if running else None,
            },
        )

    def respond(self, method: str, path: str, status: int, body: object) -> None:
        self.responses[(method, path)] = (status, body)

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.path) for r in self.requests]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            Recorded(
                request.method,
                request.path,
                dict(request.query),
                request.headers.get("Authorization"),
            )
        )
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        status, body = self.responses.get((request.method, request.path), (404, {"error": "not_found"}))
        if isinstance(body, bytes):
            return web.Response(status=status, body=body)
        if status in (301, 302, 307, 308):
            return web.Response(status=status, headers={"Location": str(body)})
        return web.json_response(body, status=status)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app


async def start_stub() -> tuple[VaultStub, TestServer]:
    stub = VaultStub()
    server = TestServer(stub.app(), host="127.0.0.1")
    await server.start_server()
    stub.url = f"http://127.0.0.1:{server.port}"
    return stub, server
