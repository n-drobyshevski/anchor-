"""The API listens on IPv4 as well as IPv6.

Railway's healthcheck connects over IPv4; its private DNS may resolve to
IPv6 only. asyncio makes a listener on `::` IPv6-only, which failed
every healthcheck, so `start_site` binds every family instead.
"""

from __future__ import annotations

import socket

import aiohttp
import pytest
from aiohttp import web

from vaultd import api
from vaultd.__main__ import start_site


def _has_ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.fixture
async def listening():
    app = web.Application()
    app.router.add_get("/healthz", api.healthz)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await start_site(runner, 0)
    # Port 0 gives each family its own port; an IPv6 address has 4 fields.
    ports = {
        socket.AF_INET6 if len(addr) == 4 else socket.AF_INET: addr[1]
        for addr in runner.addresses
    }
    yield ports
    await runner.cleanup()


async def _get(url: str) -> int:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return resp.status


async def test_healthz_answers_over_ipv4(listening) -> None:
    port = listening[socket.AF_INET]
    assert await _get(f"http://127.0.0.1:{port}/healthz") == 200


@pytest.mark.skipif(not _has_ipv6(), reason="no IPv6 on this host")
async def test_healthz_answers_over_ipv6(listening) -> None:
    port = listening[socket.AF_INET6]
    assert await _get(f"http://[::1]:{port}/healthz") == 200
