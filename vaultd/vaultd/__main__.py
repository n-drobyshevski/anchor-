"""`python -m vaultd`: boot, supervise ob, serve the API on [::]:$PORT.

Binding `::` is deliberate: Railway's private DNS resolves to IPv6 only
in legacy environments (dual-stack in newer ones), and a dual-stack
Linux socket bound to `::` accepts both.
"""

from __future__ import annotations

import asyncio
import os
import signal

from aiohttp import web

from vaultd import api, boot, config
from vaultd.log import setup_logging
from vaultd.manifest import Manifest
from vaultd.store import Store
from vaultd.supervisor import Supervisor


async def serve(cfg: config.Config, env: dict[str, str], ob_bin: str = config.OB_BIN) -> None:
    ob_env = await boot.boot(cfg, env, ob_bin)
    supervisor = Supervisor(boot.sync_argv(ob_bin, cfg.vault_path), ob_env, cfg.config_home)
    app = api.make_app(
        token=cfg.api_token,
        store=Store(cfg.vault_path, cfg.tmp_path),
        manifest_=Manifest(cfg.vault_path),
        status_source=supervisor,
    )
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    await web.TCPSite(runner, host="::", port=cfg.port).start()
    sync_task = asyncio.create_task(supervisor.run())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await supervisor.stop()
    sync_task.cancel()
    await runner.cleanup()


def main() -> None:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    env = dict(os.environ)
    try:
        cfg = config.from_env(env)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    asyncio.run(serve(cfg, env))


if __name__ == "__main__":
    main()
