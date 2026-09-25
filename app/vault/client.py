"""The bot's HTTP client for vaultd (phase-8 plan section 5.4).

One method per route, each returning plain data or raising VaultError
with a code from app/vault/errors.py. 8a calls only `status()`; the
rest exist because the API they mirror ships in 8a, and 8b-8d use them.

**What the client refuses to do.** No redirects (a 3xx is a bad
response, not a hop). No proxy or `.netrc` from the environment
(`trust_env=False`), so the bearer token only ever goes to VAULT_URL,
which check_runtime_settings has already pinned to the private network.
No cookies. A response larger than MAX_RESPONSE_BYTES is refused
rather than buffered. The timeout is a constant: a hung vault service
must cost a /vault command seconds, not a worker slot.

**Nothing here is logged** -- not the URL, not a path, not a body. The
callers log codes.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp

from app.config import Settings
from app.vault import errors
from app.vault.errors import VaultError

TIMEOUT_S = 5.0
# The manifest of a vault with a few thousand notes is well under 1 MB;
# a file body is capped at 64 KB by vaultd. Eight megabytes is slack,
# not a target.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

SessionOpener = Callable[[], aiohttp.ClientSession]


def open_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={"User-Agent": "AnchorBot/1.0 (vault client)"},
        trust_env=False,
    )


@dataclass(frozen=True)
class ServiceStatus:
    sync_running: bool
    restarts: int
    last_exit_code: int | None
    running_since: datetime.datetime | None


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    sha256: str
    size: int
    scope: str


@dataclass(frozen=True)
class FileContent:
    path: str
    sha256: str
    content: str


def _status_error(status: int) -> str:
    if status == 401:
        return errors.UNAUTHORIZED
    if status == 404:
        return errors.NOT_FOUND
    if status == 412:
        return errors.CONFLICT
    if status in (400, 403, 413, 422):
        return errors.REFUSED
    return errors.UNAVAILABLE if status >= 500 else errors.BAD_RESPONSE


def _require(condition: bool) -> None:
    if not condition:
        raise VaultError(errors.BAD_RESPONSE)


def _parse_time(value: Any) -> datetime.datetime | None:
    if value is None:
        return None
    _require(isinstance(value, str))
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise VaultError(errors.BAD_RESPONSE) from None
    _require(parsed.tzinfo is not None)
    return parsed


class VaultClient:
    def __init__(self, base_url: str, token: str, *, opener: SessionOpener = open_session) -> None:
        self._base = base_url.strip().rstrip("/")
        self._token = token
        self._opener = opener

    @classmethod
    def from_settings(cls, settings: Settings) -> "VaultClient":
        return cls(settings.VAULT_URL, settings.VAULT_API_TOKEN)

    async def _request(
        self,
        method: str,
        route: str,
        *,
        params: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with self._opener() as session:
                async with session.request(
                    method,
                    self._base + route,
                    params=params,
                    json=body,
                    headers=headers,
                    allow_redirects=False,
                ) as resp:
                    raw = await resp.content.read(MAX_RESPONSE_BYTES + 1)
                    status = resp.status
        except asyncio.TimeoutError:
            raise VaultError(errors.UNAVAILABLE) from None
        except (aiohttp.ClientError, OSError):
            raise VaultError(errors.UNAVAILABLE) from None
        if status != 200:
            raise VaultError(_status_error(status))
        _require(len(raw) <= MAX_RESPONSE_BYTES)
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise VaultError(errors.BAD_RESPONSE) from None
        _require(isinstance(parsed, dict))
        return parsed

    async def status(self) -> ServiceStatus:
        data = await self._request("GET", "/v1/status")
        running = data.get("sync_running")
        restarts = data.get("restarts")
        exit_code = data.get("last_exit_code")
        _require(isinstance(running, bool))
        _require(isinstance(restarts, int) and not isinstance(restarts, bool) and restarts >= 0)
        _require(exit_code is None or (isinstance(exit_code, int) and not isinstance(exit_code, bool)))
        return ServiceStatus(
            sync_running=running,
            restarts=restarts,
            last_exit_code=exit_code,
            running_since=_parse_time(data.get("running_since")),
        )

    async def manifest(self) -> list[ManifestEntry]:
        data = await self._request("GET", "/v1/manifest")
        files = data.get("files")
        _require(isinstance(files, list))
        out = []
        for item in files:
            _require(isinstance(item, dict))
            path, sha, size, scope = (item.get(k) for k in ("path", "sha256", "size", "scope"))
            _require(isinstance(path, str) and isinstance(sha, str) and isinstance(size, int))
            _require(scope in ("anchor", "note"))
            out.append(ManifestEntry(path=path, sha256=sha, size=size, scope=scope))
        return out

    async def get_file(self, path: str) -> FileContent:
        data = await self._request("GET", "/v1/file", params={"path": path})
        content, sha = data.get("content"), data.get("sha256")
        _require(isinstance(content, str) and isinstance(sha, str) and data.get("path") == path)
        return FileContent(path=path, sha256=sha, content=content)

    async def put_file(self, path: str, content: str, if_sha256: str | None) -> str:
        data = await self._request(
            "PUT", "/v1/file", params={"path": path}, body={"content": content, "if_sha256": if_sha256}
        )
        sha = data.get("sha256")
        _require(isinstance(sha, str))
        return sha

    async def delete_file(self, path: str, if_sha256: str) -> None:
        await self._request("DELETE", "/v1/file", params={"path": path, "if_sha256": if_sha256})

    async def purge(self) -> int:
        data = await self._request("POST", "/v1/purge")
        deleted = data.get("deleted")
        _require(isinstance(deleted, int) and not isinstance(deleted, bool))
        return deleted
