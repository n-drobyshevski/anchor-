"""Bearer auth (plan sections 5.4, 15)."""

from __future__ import annotations

import pytest

from tests.conftest import AUTH, TOKEN

ROUTES = [
    ("get", "/v1/status"),
    ("get", "/v1/manifest"),
    ("get", "/v1/file?path=Anchor/Memory/x.md"),
    ("put", "/v1/file?path=Anchor/Memory/x.md"),
    ("delete", "/v1/file?path=Anchor/Memory/x.md&if_sha256=" + "0" * 64),
    ("post", "/v1/purge"),
]

BAD_HEADERS = [
    {},
    {"Authorization": "Bearer wrong-token-" + "y" * 32},
    {"Authorization": TOKEN},
    {"Authorization": "bearer " + TOKEN},
    {"Authorization": "Bearer " + TOKEN + " "},
    {"Authorization": "Basic " + TOKEN},
]


@pytest.mark.parametrize("method,url", ROUTES)
@pytest.mark.parametrize("headers", BAD_HEADERS)
async def test_missing_or_wrong_token_is_401(client, vault, method, url, headers) -> None:
    kwargs = {"headers": headers}
    if method == "put":
        kwargs["json"] = {"content": "x", "if_sha256": None}
    resp = await getattr(client, method)(url, **kwargs)
    assert resp.status == 401
    assert list(vault.rglob("*")) == []


async def test_healthz_needs_no_token_and_says_nothing(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert await resp.json() == {"ok": True}


async def test_status_with_the_token(client) -> None:
    resp = await client.get("/v1/status", headers=AUTH)
    assert resp.status == 200
    assert set(await resp.json()) == {"sync_running", "restarts", "last_exit_code", "running_since"}
