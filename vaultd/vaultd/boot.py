"""Boot: refuse, or set ob up (plan section 5.2).

**Refusals come first, and each one names a variable, never a value.**
vaultd exits non-zero before anything else runs if:

- `RAILWAY_PUBLIC_DOMAIN` or `RAILWAY_TCP_PROXY_DOMAIN` is set. Either
  means the service is reachable from the internet, and this API is
  meant for the private network only. The TCP proxy is not in the plan;
  it was found while verifying Railway's docs and is refused for the
  same reason (docs/decisions.md);
- `VAULT_API_TOKEN` is shorter than 32 characters;
- any `OBSIDIAN_*` variable is empty.

**Then ob, in order, every command as an argv list and never through a
shell:**

1. `sync-list-remote --json`. `OBSIDIAN_VAULT` must name exactly one
   vault in `vaults` (by id, else by a unique name) and nothing in
   `shared`. That array is the only thing that marks a vault as shared
   with you -- verified in obsidian-headless 0.0.14's cli.js. A
   collaborator on a shared vault could write your facts.
2. `sync-list-local --json`. If VAULT_PATH is already linked to a
   different vault id, refuse: a changed OBSIDIAN_VAULT must not quietly
   keep syncing the old one. If it is not linked, run
   `sync-setup --vault <id> --path … --password … --device-name … --json`,
   passing the resolved id so ob's own name lookup, which also searches
   shared vaults, never runs.
3. `sync-config … --mode bidirectional --conflict-strategy merge
   --configs "" --json`, **on every boot**, so the settings cannot drift.
   The file-type filters are never touched (0.0.13 fixed a bug where
   changing them deleted remote files).

The end-to-end password is accepted by ob only as `--password` on
argv, so it is visible to other processes in this container while
`sync-setup` runs. That residual exposure is recorded in
docs/decisions.md. Command output is parsed where it is JSON and
otherwise discarded; none of it is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from vaultd.config import OB_COMMAND_TIMEOUT_S, Config

logger = logging.getLogger("vaultd.boot")

PUBLIC_EXPOSURE_VARS = ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_TCP_PROXY_DOMAIN")
OBSIDIAN_VARS = ("OBSIDIAN_AUTH_TOKEN", "OBSIDIAN_VAULT", "OBSIDIAN_E2EE_PASSWORD")
TOKEN_MIN_CHARS = 32


class BootRefused(SystemExit):
    """Exit non-zero with a message that names variables, never values."""


def check_env(env: Mapping[str, str], cfg: Config) -> None:
    for name in PUBLIC_EXPOSURE_VARS:
        if env.get(name, "").strip():
            raise BootRefused(
                f"{name} is set: this service has a public endpoint. vaultd serves "
                "Railway's private network only -- remove the domain or TCP proxy."
            )
    if len(cfg.api_token) < TOKEN_MIN_CHARS:
        raise BootRefused(
            f"VAULT_API_TOKEN must be at least {TOKEN_MIN_CHARS} characters "
            "(the value is deliberately not shown)."
        )
    empty = [name for name in OBSIDIAN_VARS if not env.get(name, "").strip()]
    if empty:
        raise BootRefused("Missing required environment variables: " + ", ".join(empty) + ".")


def child_env(env: Mapping[str, str], cfg: Config) -> dict[str, str]:
    """The only variables an ob process ever sees."""
    out = {
        "PATH": env.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": env.get("HOME", "/root"),
        "XDG_CONFIG_HOME": str(cfg.config_home),
        "OBSIDIAN_AUTH_TOKEN": cfg.auth_token,
    }
    return out


async def run_ob(ob_bin: str, args: list[str], env: Mapping[str, str]) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        ob_bin,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=dict(env),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), OB_COMMAND_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BootRefused(f"ob {args[0]} timed out.") from None
    return (proc.returncode if proc.returncode is not None else -1), stdout


def _parse_json(command: str, stdout: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(stdout)
    except ValueError:
        raise BootRefused(f"ob {command} did not print JSON.") from None
    if not isinstance(parsed, dict):
        raise BootRefused(f"ob {command} printed unexpected JSON.")
    return parsed


def _vault_list(parsed: dict[str, Any], key: str, command: str) -> list[dict[str, Any]]:
    value = parsed.get(key)
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise BootRefused(f"ob {command} printed unexpected JSON.")
    return value


def resolve_owned_vault(remote: dict[str, Any], wanted: str) -> str:
    """The id of the vault OBSIDIAN_VAULT names, if it is one you own."""
    owned = _vault_list(remote, "vaults", "sync-list-remote")
    shared = _vault_list(remote, "shared", "sync-list-remote")
    by_id = [v for v in owned if v.get("id") == wanted]
    if len(by_id) == 1:
        return str(by_id[0]["id"])
    if any(v.get("id") == wanted or v.get("name") == wanted for v in shared):
        raise BootRefused(
            "OBSIDIAN_VAULT names a vault shared with this account, not one it owns. "
            "A collaborator on a shared vault could write Anchor's facts; use your own "
            "vault (by id if the name is ambiguous)."
        )
    by_name = [v for v in owned if v.get("name") == wanted]
    if len(by_name) == 1 and isinstance(by_name[0].get("id"), str):
        return by_name[0]["id"]
    raise BootRefused(
        "OBSIDIAN_VAULT matches no vault owned by this account, or more than one; "
        "use the vault id from `ob sync-list-remote`."
    )


async def boot(cfg: Config, env: Mapping[str, str], ob_bin: str) -> dict[str, str]:
    """Run every boot step. Returns the environment for `ob sync`."""
    check_env(env, cfg)
    for path in (cfg.vault_path, cfg.config_home, cfg.tmp_path):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    ob_env = child_env(env, cfg)

    code, out = await run_ob(ob_bin, ["sync-list-remote", "--json"], ob_env)
    if code != 0:
        raise BootRefused(f"ob sync-list-remote failed (exit {code}). Is OBSIDIAN_AUTH_TOKEN valid?")
    vault_id = resolve_owned_vault(_parse_json("sync-list-remote", out), cfg.vault)

    code, out = await run_ob(ob_bin, ["sync-list-local", "--json"], ob_env)
    if code != 0:
        raise BootRefused(f"ob sync-list-local failed (exit {code}).")
    local = _vault_list(_parse_json("sync-list-local", out), "vaults", "sync-list-local")
    here = os.path.realpath(cfg.vault_path)
    linked = [
        v for v in local if isinstance(v.get("path"), str) and os.path.realpath(v["path"]) == here
    ]
    if any(v.get("id") != vault_id for v in linked):
        raise BootRefused(
            "VAULT_PATH is already linked to a different remote vault than OBSIDIAN_VAULT. "
            "Refusing to sync it; unlink it on the volume first if the change is intended."
        )
    if not linked:
        code, _ = await run_ob(
            ob_bin,
            [
                "sync-setup",
                "--vault", vault_id,
                "--path", str(cfg.vault_path),
                "--password", cfg.e2ee_password,
                "--device-name", cfg.device_name,
                "--json",
            ],
            ob_env,
        )
        if code != 0:
            raise BootRefused(
                f"ob sync-setup failed (exit {code}). Exit 2 usually means "
                "OBSIDIAN_E2EE_PASSWORD is wrong."
            )
        logger.info("ob sync-setup done", extra={"event": "sync_setup"})

    code, _ = await run_ob(
        ob_bin,
        [
            "sync-config",
            "--path", str(cfg.vault_path),
            "--mode", "bidirectional",
            "--conflict-strategy", "merge",
            "--configs", "",
            "--json",
        ],
        ob_env,
    )
    if code != 0:
        raise BootRefused(f"ob sync-config failed (exit {code}).")
    logger.info("boot done", extra={"event": "boot_done"})
    return ob_env


def sync_argv(ob_bin: str, vault_path: Path) -> list[str]:
    return [ob_bin, "sync", "--continuous", "--path", str(vault_path)]
