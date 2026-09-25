"""Manual browser E2E harness for the Anchor web UI (anchor-web-panels-plan.md §6).

Not part of the automated test suite -- there is no new dev dependency
and pytest never imports this module. Run it by hand:

    uv run --with playwright python scripts/web_e2e.py

Requirements (see the module's own README-style comments below for
detail on each):

- Postgres must already be reachable at ANCHOR_ADMIN_DATABASE_URL
  (default postgresql://anchor:anchor@127.0.0.1:5432/postgres, the same
  as tests/conftest.py and CI; --admin-dsn overrides both). This
  script creates and migrates its own scratch database for the run and
  drops it afterwards.
- Chromium must already exist under /opt/pw-browsers -- this script
  never runs `playwright install`; it finds the binary itself and
  passes it to Playwright as `executable_path`.
- Everything else (the real aiohttp app, the worker, the tail task, a
  throwaway TLS cert) is built in-process, in this file.

What it checks, end to end, against the real app/main.py wiring:

- login: passphrase -> Telegram code (read off FakeSession's recorded
  SendMessage calls, never guessed) -> session cookie
- chat: sending a message and receiving an assistant reply over SSE
- every nav screen loads with no console error, no page error, and no
  CSP / Trusted Types violation
- one write action on State (set the due-today action) and one on
  Memory (add a memory) -- and zero Telegram Bot API sends caused by
  either (web panel writes are silent in Telegram by design; the
  login-code send is the only expected Telegram send all run)
- check-in (W4, scenario_checkin): the no-note path, a same-day
  redo with a note, and a second redo without one -- each must finish
  the check-in in the database, deliver exactly one CHECKIN_FLAG
  reaction into the web chat over SSE, and send nothing to Telegram
- seeded history (seed_history: ~20 rated days, order answers, journal
  lines) so the check-in chart, history list and journal render
- screenshots at 390x844 dark and 1280x800 light for every screen
  visited, saved under --out (default: .e2e-out/ in the repo root,
  git-ignored)

Exit code is non-zero iff any scenario failed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import os
import random
import ssl
import string
import subprocess
import sys
import tempfile
import traceback  # extract_tb only -- tests/test_log.py forbids formatting whole tracebacks
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
PW_BROWSERS_DIR = Path("/opt/pw-browsers")
DEFAULT_OUT = str(REPO_ROOT / ".e2e-out")
DEFAULT_ADMIN_DSN = os.environ.get(
    "ANCHOR_ADMIN_DATABASE_URL", "postgresql://anchor:anchor@127.0.0.1:5432/postgres"
)

# tests/conftest.py is a plain module (no package __init__), never
# published as a library -- importing it the way pytest does (its
# directory on sys.path) is what "import them" in the task brief means.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import conftest  # noqa: E402 -- FakeSession, FakeLLMProvider, _run_alembic_upgrade, make_bot
import asyncpg  # noqa: E402
from aiogram import Bot  # noqa: E402
from aiogram.methods import SetMyCommands, SetWebhook  # noqa: E402
from aiohttp import web  # noqa: E402

from app.config import Settings  # noqa: E402
from app.core.clock import SystemClock  # noqa: E402
from app.db.session import create_engine_and_sessionmaker  # noqa: E402
from app.main import build_dispatcher, build_webhook_app  # noqa: E402
from app.web import auth as web_auth  # noqa: E402
from app.web.hub import WebHub  # noqa: E402
from scripts.web_passphrase import make_hash  # noqa: E402

PASSPHRASE = "correct horse battery staple e2e"
CHAT_ID = 900900900


# --- a Telegram session that also no-ops the two calls app/main.py's
# startup/register_commands paths make that conftest's FakeSession
# never had to handle (webhook mode's own on_startup) ------------------


class E2ESession(conftest.FakeSession):
    async def make_request(self, bot: Bot, method, timeout: int | None = None):
        if isinstance(method, (SetWebhook, SetMyCommands)):
            return True
        return await super().make_request(bot, method, timeout=timeout)


class _NullLLMClient:
    """Stands in for app["llm_client"] -- app/main.py's _on_cleanup calls
    .close() on it directly (never on a provider); FakeLLMProvider is
    never wired as a real AsyncOpenAI client, so this is what actually
    gets closed.
    """

    async def close(self) -> None:
        pass


# --- throwaway TLS: the __Host- session cookie is Secure, so the
# browser will not store it over plain http (app/web/http.py's
# _set_session_cookie); app/config.py's check_runtime_settings itself
# also refuses a non-https PUBLIC_URL for anything but literal
# localhost/127.0.0.1 dev exceptions -- easiest to just serve real TLS
# and have Chromium ignore the self-signed cert. No `cryptography`
# dependency in this project (checked before writing this), so the
# cert is generated with the openssl CLI, per the task brief's
# fallback. ---------------------------------------------------------


def _generate_self_signed_cert(cert_dir: Path) -> tuple[Path, Path]:
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key_path), "-out", str(cert_path),
            "-days", "1", "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert_path, key_path


# --- scratch database: create + migrate + (later) drop, mirroring
# tests/conftest.py's test_database_url fixture but standalone (no
# pytest session, no TRUNCATE-at-teardown -- this process owns the
# whole database for its own lifetime and drops it outright). --------


class ScratchDatabase:
    def __init__(self, admin_dsn: str) -> None:
        self.admin_dsn = admin_dsn
        self.db_name = "anchor_e2e_" + "".join(
            random.choices(string.ascii_lowercase + string.digits, k=10)
        )
        base = admin_dsn.rsplit("/", 1)[0]
        self.raw_url = f"{base}/{self.db_name}"
        self.asyncpg_url = self.raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    async def create_and_migrate(self) -> None:
        conn = await asyncpg.connect(self.admin_dsn)
        try:
            await conn.execute(
                f'CREATE DATABASE "{self.db_name}" '
                "TEMPLATE template0 LOCALE 'C.UTF-8' ENCODING 'UTF8'"
            )
        finally:
            await conn.close()
        # Synchronous (alembic's Config/command API), same call
        # tests/conftest.py uses -- but migrations/env.py's own
        # run_migrations_online() calls asyncio.run() internally, which
        # cannot nest inside this script's already-running event loop
        # (unlike a pytest run, where each test's fixture setup happens
        # outside any event loop). A worker thread gives it a fresh
        # thread with no running loop of its own.
        await asyncio.to_thread(conftest._run_alembic_upgrade, self.raw_url)

    async def drop(self) -> None:
        conn = await asyncpg.connect(self.admin_dsn)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{self.db_name}' AND pid <> pg_backend_pid()"
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{self.db_name}"')
        finally:
            await conn.close()


# --- seeded history for the Check-in screen: ~20 of the last 30 local
# days get a rated check-in row (some with a due result, a note and an
# order answer), plus a handful of journal lines, so the chart, history
# list and journal all render with real data. Today is never seeded --
# the check-in scenarios below fill it through the UI. One active daily
# standing order makes today's form ask an order question. ----------

SEED_TIMEZONE = "Europe/Paris"  # user_state's server default


async def seed_history(raw_url: str) -> None:
    from zoneinfo import ZoneInfo

    today = datetime.datetime.now(ZoneInfo(SEED_TIMEZONE)).date()
    rng = random.Random(4)
    conn = await asyncpg.connect(raw_url)
    try:
        order_id = await conn.fetchval(
            "INSERT INTO standing_order (text, cadence, status, source, decided_at) "
            "VALUES ('Прогулка 20 минут', 'daily', 'active', 'user', now()) RETURNING id"
        )
        skipped = {3, 7, 8, 15, 19, 22, 23, 27, 29}
        notes = {
            1: "Нормально, но устал к вечеру.",
            4: "Хороший день, много сделал.",
            10: "Плохо спал.",
            16: "Выходной, гулял.",
        }
        for back in range(1, 30):
            if back in skipped:
                continue
            day = today - datetime.timedelta(days=back)
            rating = rng.choice([2, 3, 3, 4, 4, 4, 5]) if back != 12 else 1
            due = rng.choice(["done", "partial", "no", "none", "none"])
            checkin_id = await conn.fetchval(
                "INSERT INTO checkin (local_date, day_rating, due_result, note) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                day, rating, due, notes.get(back),
            )
            if back <= 6:
                await conn.execute(
                    "INSERT INTO checkin_order_result (checkin_id, order_id, result) "
                    "VALUES ($1, $2, $3)",
                    checkin_id, order_id, "done" if back % 2 else "no",
                )
        # A row with no rating at all (a Telegram check-in abandoned
        # after /checkin): drawn as an empty slot.
        await conn.execute(
            "INSERT INTO checkin (local_date) VALUES ($1)", today - datetime.timedelta(days=8)
        )
        journal = [
            (0, "Обсуждали план на неделю, решил начать с утренних прогулок."),
            (1, "Пожаловался на усталость после длинного рабочего дня."),
            (1, "Договорились не открывать ноутбук после 22:00."),
            (2, "Рассказал, что закончил отчёт раньше срока."),
            (5, "Вспоминал поездку к морю прошлым летом."),
            (12, "Тяжёлый день: поссорился с коллегой, потом помирились."),
            (40, "Первая запись журнала -- знакомство."),
        ]
        for back, text in journal:
            await conn.execute(
                "INSERT INTO journal (local_date, text) VALUES ($1, $2)",
                today - datetime.timedelta(days=back), text,
            )
    finally:
        await conn.close()


def _find_chromium() -> str:
    if not PW_BROWSERS_DIR.is_dir():
        raise SystemExit(f"{PW_BROWSERS_DIR} not found -- expected a preinstalled Chromium there")
    candidates = sorted(PW_BROWSERS_DIR.glob("chromium-*/chrome-linux/chrome"))
    if not candidates:
        # Some installs name the binary "headless_shell" or nest it
        # differently -- fall back to a broad search rather than only
        # ever matching the one layout above.
        candidates = sorted(
            p for p in PW_BROWSERS_DIR.glob("chromium-*/**/*")
            if p.is_file() and p.name in ("chrome", "headless_shell") and "headless_shell" not in str(p.parent.parent)
        )
    if not candidates:
        raise SystemExit(f"no chromium executable found under {PW_BROWSERS_DIR}")
    return str(candidates[-1])


# ------------------------------------------------------------------
# The app: built from the real pieces (app/main.py, app/web/routes.py,
# app/worker.py, app/web/tail.py), never a stand-in aiohttp app of the
# harness's own.
# ------------------------------------------------------------------


class Harness:
    def __init__(self, *, port: int, cert_path: Path, key_path: Path, admin_dsn: str) -> None:
        self.port = port
        self.cert_path = cert_path
        self.key_path = key_path
        self.base_url = f"https://127.0.0.1:{port}"
        self.scratch_db = ScratchDatabase(admin_dsn)
        self.fake_session: E2ESession | None = None
        self.runner: web.AppRunner | None = None
        self.engine = None
        self.provider: Any = None

    async def db_fetch(self, query: str, *args) -> list:
        conn = await asyncpg.connect(self.scratch_db.raw_url)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()

    async def start(self) -> None:
        await self.scratch_db.create_and_migrate()
        await seed_history(self.scratch_db.raw_url)

        settings = Settings(
            MODE="webhook",
            TELEGRAM_BOT_TOKEN="123456:E2E-FAKE-TOKEN",
            TELEGRAM_SECRET_TOKEN="e2e-secret-token",
            ALLOWED_CHAT_ID=CHAT_ID,
            PUBLIC_URL=self.base_url,
            DATABASE_URL=self.scratch_db.asyncpg_url,
            OPENROUTER_API_KEY="e2e-fake-key",
            WEB_UI_ENABLED=True,
            WEB_PASSPHRASE_HASH=make_hash(PASSPHRASE),
            WEB_SESSION_IDLE_HOURS=72,
            WEB_SESSION_MAX_DAYS=14,
            WEB_LOGIN_CODE_TTL_S=300,
        )

        self.engine, sessionmaker = create_engine_and_sessionmaker(settings.DATABASE_URL)

        self.fake_session = E2ESession()
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, session=self.fake_session)

        provider = conftest.FakeLLMProvider(text="Отвечаю в тестовом режиме e2e.")
        self.provider = provider
        cheap_provider = conftest.FakeLLMProvider(text="cheap e2e")
        safety_provider = conftest.FakeLLMProvider(text="safety e2e")

        clock = SystemClock()
        hub = WebHub()
        code_store = web_auth.CodeStore()

        dp = build_dispatcher(sessionmaker, settings, provider, safety_provider, clock, hub, code_store)

        app = build_webhook_app(
            settings,
            bot,
            dp,
            sessionmaker,
            self.engine,
            provider,
            cheap_provider,
            safety_provider,
            _NullLLMClient(),
            clock,
            hub,
            code_store,
        )

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(str(self.cert_path), str(self.key_path))

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        # AppRunner.setup() runs on_startup, so run_worker/start_tail
        # (app/main.py's _on_startup) are already live by the time this
        # returns -- the site below only starts accepting connections.
        site = web.TCPSite(self.runner, "127.0.0.1", self.port, ssl_context=ssl_ctx)
        await site.start()

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()  # on_shutdown + on_cleanup: stops worker/tail, disposes engine
        with contextlib.suppress(Exception):
            await self.scratch_db.drop()

    @property
    def sent_count(self) -> int:
        assert self.fake_session is not None
        return len(self.fake_session.sent)

    def last_login_code(self) -> str:
        """The most recent login code Telegram "received", parsed out of
        CODE_MESSAGE (app/web/routes.py). Never read from CodeStore --
        that is exactly the private state a real user could not see
        either; this harness only ever gets the code the way a person
        would, off their phone.
        """
        assert self.fake_session is not None
        for method in reversed(self.fake_session.sent):
            text = method.text or ""
            if "Код входа" in text:
                # "Код входа в веб-Anchor: XXXX-XXXX (5 мин). ..."
                for token in text.split():
                    if len(token) == 9 and token[4] == "-":
                        return token
        raise AssertionError("no login-code SendMessage found in FakeSession.sent")


# ------------------------------------------------------------------
# Playwright scenarios
# ------------------------------------------------------------------


class Ctx:
    """Per-run state threaded through every scenario function."""

    def __init__(self, harness: Harness, out_dir: Path, label: str) -> None:
        self.harness = harness
        self.out_dir = out_dir
        self.label = label
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.csp_violations: list[str] = []
        self.failures: list[str] = []
        self.did_state_action = False
        self.did_memory_action = False

    def fail(self, message: str) -> None:
        self.failures.append(f"[{self.label}] {message}")
        print(f"FAIL [{self.label}] {message}")

    def ok(self, message: str) -> None:
        print(f"ok   [{self.label}] {message}")

    def screenshot_path(self, screen: str) -> Path:
        safe = screen.strip("#/").replace("/", "_") or "root"
        return self.out_dir / f"{self.label}_{safe}.png"


_CSP_MARKERS = ("Content Security Policy", "Refused to", "TrustedHTML", "Trusted Types")


def _wire_console_capture(page, ctx: Ctx) -> None:
    def on_console(msg) -> None:
        text = msg.text
        if msg.type == "error":
            ctx.console_errors.append(text)
        if any(marker in text for marker in _CSP_MARKERS):
            ctx.csp_violations.append(text)

    def on_page_error(exc) -> None:
        ctx.page_errors.append(str(exc))

    page.on("console", on_console)
    page.on("pageerror", on_page_error)


_INIT_SCRIPT = """
window.addEventListener('securitypolicyviolation', (e) => {
  console.error('CSPViolation: ' + e.violatedDirective + ' blocked=' + e.blockedURI);
});
"""


async def scenario_login(page, ctx: Ctx) -> None:
    await page.goto(ctx.harness.base_url + "/", wait_until="networkidle")
    await page.wait_for_selector("#login-passphrase", state="visible", timeout=10_000)

    sent_before = ctx.harness.sent_count
    await page.fill("#passphrase-input", PASSPHRASE)
    await page.click("#login-passphrase button[type=submit]")
    await page.wait_for_selector("#login-code", state="visible", timeout=10_000)
    if ctx.harness.sent_count != sent_before + 1:
        ctx.fail(
            f"expected exactly one Telegram send for the login code, "
            f"got {ctx.harness.sent_count - sent_before}"
        )
    else:
        ctx.ok("login code sent exactly once over Telegram")

    code = ctx.harness.last_login_code()
    await page.fill("#code-input", code)
    await page.click("#login-code button[type=submit]")
    await page.wait_for_selector("#chat", state="visible", timeout=10_000)
    ctx.ok("logged in")


async def scenario_chat(page, ctx: Ctx) -> None:
    text = f"e2e ping {datetime.datetime.now(datetime.timezone.utc).isoformat()}"
    sent_before = ctx.harness.sent_count
    await page.fill("#composer-input", text)
    await page.click("#send-button")

    own_row = page.locator(f"#log .msg-row.role-user:has-text({text!r})")
    await own_row.first.wait_for(state="visible", timeout=10_000)
    ctx.ok("own message rendered in the log")

    # An assistant reply must arrive over SSE, fed by the real worker
    # (app/worker.py) through the web sink (app/web/sink.py), not by
    # this harness faking anything.
    assistant_row = page.locator("#log .msg-row.role-assistant")
    try:
        await assistant_row.last.wait_for(state="visible", timeout=20_000)
        ctx.ok("assistant reply received over SSE")
    except Exception as exc:  # noqa: BLE001
        ctx.fail(f"no assistant reply arrived in time: {exc}")

    if ctx.harness.sent_count != sent_before:
        ctx.fail(
            "chat send/receive caused a Telegram Bot API send "
            f"(+{ctx.harness.sent_count - sent_before}) -- it must be silent in Telegram"
        )
    else:
        ctx.ok("chat send/receive sent nothing to Telegram")


async def _goto_screen(page, ctx: Ctx, route: str) -> bool:
    """Navigates to `route` via the nav bar (never a raw location.hash
    assignment, so this exercises router.js the way a user would).
    Returns False (and records nothing as a failure) if the route is
    not in the nav at all.
    """
    link = page.locator(f'#nav a[href="{route}"]')
    if await link.count() == 0:
        return False
    await link.first.click()
    await page.wait_for_function(
        "route => location.hash === route", arg=route, timeout=5_000
    )
    await page.wait_for_timeout(300)  # let the screen's own GET land
    return True


async def scenario_nav_screens(page, ctx: Ctx) -> None:
    for route, selector in (
        ("#/chat", "#chat"),
        ("#/state", ".screen-state"),
        ("#/memory", ".screen-memory"),
        ("#/proposals", ".screen"),
        ("#/checkin", ".screen"),
    ):
        present = await _goto_screen(page, ctx, route)
        if not present:
            ctx.ok(f"{route} not in nav (skipped)")
            continue
        try:
            await page.wait_for_selector(selector, state="visible", timeout=10_000)
            ctx.ok(f"{route} loaded")
        except Exception as exc:  # noqa: BLE001
            ctx.fail(f"{route} did not render ({selector}): {exc}")
        await page.screenshot(path=str(ctx.screenshot_path(route)))


async def scenario_state_due_action(page, ctx: Ctx) -> None:
    if not await _goto_screen(page, ctx, "#/state"):
        ctx.fail("#/state missing from nav; cannot run the State panel action")
        return
    await page.wait_for_selector(".screen-state", state="visible", timeout=10_000)

    sent_before = ctx.harness.sent_count
    edit_button = page.locator('button[aria-label="Изменить действие на сегодня"]')
    await edit_button.wait_for(state="visible", timeout=10_000)
    await edit_button.click()

    due_text = f"e2e due action {datetime.datetime.now(datetime.timezone.utc):%H:%M:%S}"
    textarea = page.locator(".screen-state textarea.field-edit")
    await textarea.fill(due_text)
    await page.locator(".screen-state button.btn-primary", has_text="Сохранить").click()

    try:
        await page.locator(".screen-state .field-value", has_text=due_text).wait_for(
            state="visible", timeout=10_000
        )
        ctx.ok("State: set due-today action")
    except Exception as exc:  # noqa: BLE001
        ctx.fail(f"State due action did not save: {exc}")

    if ctx.harness.sent_count != sent_before:
        ctx.fail(
            "State panel write caused a Telegram Bot API send "
            f"(+{ctx.harness.sent_count - sent_before}) -- panel writes must be silent in Telegram"
        )
    else:
        ctx.ok("State panel write sent nothing to Telegram")
    ctx.did_state_action = True


async def scenario_memory_add(page, ctx: Ctx) -> None:
    if not await _goto_screen(page, ctx, "#/memory"):
        ctx.fail("#/memory missing from nav; cannot run the Memory panel action")
        return
    await page.wait_for_selector(".screen-memory", state="visible", timeout=10_000)

    sent_before = ctx.harness.sent_count
    await page.click("#memory-add-toggle")
    memory_text = f"e2e memory {datetime.datetime.now(datetime.timezone.utc):%H:%M:%S}"
    await page.fill("#add-memory-text", memory_text)
    await page.locator(".add-form button.btn-primary", has_text="Сохранить").click()

    try:
        await page.locator(".memory-list", has_text=memory_text).wait_for(
            state="visible", timeout=10_000
        )
        ctx.ok("Memory: added a memory")
    except Exception as exc:  # noqa: BLE001
        ctx.fail(f"Memory add did not appear in the list: {exc}")

    if ctx.harness.sent_count != sent_before:
        ctx.fail(
            "Memory panel write caused a Telegram Bot API send "
            f"(+{ctx.harness.sent_count - sent_before}) -- panel writes must be silent in Telegram"
        )
    else:
        ctx.ok("Memory panel write sent nothing to Telegram")
    ctx.did_memory_action = True


async def _pick(page, name: str, value: str) -> None:
    """Clicks a radio-styled-as-button by its label (the input itself is
    visually hidden, so a real click lands on the label, as a user's
    would)."""
    await page.locator(f'label.radio-option:has(input[name="{name}"][value="{value}"])').click()
    checked = await page.locator(f'input[name="{name}"][value="{value}"]').is_checked()
    if not checked:
        raise AssertionError(f"radio {name}={value} did not become checked")


async def _assistant_count(page) -> int:
    return await page.locator("#log .msg-row.role-assistant").count()


async def _checkin_once(
    page, ctx: Ctx, *, step: str, rating: str, due: str | None, order: str | None,
    note: str | None, redo: bool,
) -> None:
    """One full web check-in: fill the form, submit, see the reaction
    arrive in #/chat over SSE, and verify it in the database. Failures
    go to ctx.fail (real failures -- the screen exists now)."""
    harness = ctx.harness
    if not await _goto_screen(page, ctx, "#/checkin"):
        ctx.fail(f"{step}: #/checkin missing from nav")
        return
    if redo:
        redo_button = page.locator(".screen button", has_text="Пройти заново")
        await redo_button.wait_for(state="visible", timeout=10_000)
        await redo_button.click()
    form = page.locator("form.checkin-form")
    await form.wait_for(state="visible", timeout=10_000)

    await _pick(page, "checkin-rating", rating)
    if due is not None:
        await _pick(page, "checkin-due", due)
    order_inputs = page.locator('form.checkin-form input[type="radio"][name^="checkin-order"]')
    names = sorted({await order_inputs.nth(i).get_attribute("name") for i in range(await order_inputs.count())})
    if order is None and names:
        # A same-day redo keeps the day's earlier order answers and does
        # not ask them again -- the same rule as a Telegram redo
        # (orders.remaining_due_orders).
        ctx.fail(f"{step}: a redo asked an already-answered order again")
    if order is not None:
        if not names:
            ctx.fail(f"{step}: expected an order question on the form, found none")
        for name in names:
            await _pick(page, name, order)
    await page.locator("#checkin-note").fill(note or "")
    await page.screenshot(path=str(ctx.screenshot_path(f"checkin_form_{step}")), full_page=True)

    sent_before = harness.sent_count
    llm_before = harness.provider.calls
    reply_text = f"Реакция на чек-ин e2e ({step})."
    harness.provider.text = reply_text

    await form.locator('button[type="submit"]').click()
    # Either the in-flight state or (if the worker already finished) the
    # done summary -- never the form with an error.
    try:
        await page.locator(".screen", has_text="Anchor ответит в чате").or_(
            page.locator(".screen", has_text="Чек-ин на сегодня пройден")
        ).first.wait_for(state="visible", timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        err = await page.locator("form.checkin-form .inline-error").all_inner_texts()
        ctx.fail(f"{step}: submit did not reach in-progress/done state: {exc} (form error: {err})")
        return

    await _goto_screen(page, ctx, "#/chat")
    try:
        await page.locator("#log .msg-row.role-assistant", has_text=reply_text).last.wait_for(
            state="visible", timeout=20_000
        )
        ctx.ok(f"{step}: check-in reaction arrived in the web chat over SSE")
    except Exception as exc:  # noqa: BLE001
        ctx.fail(f"{step}: check-in reaction never reached the web chat: {exc}")
    await page.wait_for_timeout(500)

    llm_calls = harness.provider.calls - llm_before
    if llm_calls != 1:
        ctx.fail(f"{step}: expected exactly one persona LLM call, got {llm_calls}")
    flagged = "только что прошёл чек-ин" in str(harness.provider.received_messages[-1])
    if not flagged:
        ctx.fail(f"{step}: the reaction turn did not carry CHECKIN_FLAG")
    if harness.sent_count != sent_before:
        ctx.fail(
            f"{step}: web check-in caused a Telegram Bot API send "
            f"(+{harness.sent_count - sent_before}) -- it must be silent in Telegram"
        )
    else:
        ctx.ok(f"{step}: nothing sent to Telegram")

    from zoneinfo import ZoneInfo

    today = datetime.datetime.now(ZoneInfo(SEED_TIMEZONE)).date()
    rows = await harness.db_fetch(
        "SELECT c.day_rating, c.due_result, c.note, s.awaiting, s.streak, s.last_checkin_at "
        "FROM checkin c CROSS JOIN user_state s WHERE c.local_date = $1",
        today,
    )
    if len(rows) != 1:
        ctx.fail(f"{step}: expected today's checkin row, got {len(rows)}")
        return
    row = rows[0]
    problems = []
    if row["day_rating"] != int(rating):
        problems.append(f"rating={row['day_rating']}")
    if due is not None and row["due_result"] != due:
        problems.append(f"due_result={row['due_result']}")
    if row["note"] != note:
        problems.append(f"note={row['note']!r}")
    if row["awaiting"] is not None:
        problems.append(f"awaiting={row['awaiting']!r}")
    if row["last_checkin_at"] is None or row["streak"] < 1:
        problems.append(f"streak={row['streak']} last={row['last_checkin_at']}")
    if problems:
        ctx.fail(f"{step}: database after check-in is wrong: {', '.join(problems)}")
    else:
        ctx.ok(f"{step}: finished in the DB (rating, due, note, streak {row['streak']})")

    # Back on the screen: the done summary, with the note if any.
    await _goto_screen(page, ctx, "#/checkin")
    try:
        await page.locator(".screen", has_text="Чек-ин на сегодня пройден").wait_for(
            state="visible", timeout=10_000
        )
        if note:
            await page.locator(".checkin-summary", has_text=note).wait_for(
                state="visible", timeout=10_000
            )
        ctx.ok(f"{step}: #/checkin shows the done summary")
    except Exception as exc:  # noqa: BLE001
        ctx.fail(f"{step}: #/checkin did not show the done summary: {exc}")


async def scenario_checkin(page, ctx: Ctx) -> None:
    """W4: the no-note path, then a same-day redo with a note,
    then a second redo back to no note. Each must: finish the check-in
    through the single worker, deliver exactly one CHECKIN_FLAG reaction
    to the web chat, and send nothing to Telegram."""
    await _checkin_once(
        page, ctx, step="skip", rating="4", due="done", order="done", note=None, redo=False
    )
    await _checkin_once(
        page, ctx, step="redo_note", rating="3", due="partial", order=None,
        note="Устал, но прогулку сделал вечером.", redo=True,
    )
    await _checkin_once(
        page, ctx, step="redo_skip", rating="5", due="no", order=None, note=None, redo=True
    )


async def scenario_checkin_views(page, ctx: Ctx) -> None:
    """Screenshots of #/checkin with the seeded history: the whole page,
    and the chart tooltip driven from the keyboard."""
    if not await _goto_screen(page, ctx, "#/checkin"):
        ctx.fail("#/checkin missing from nav")
        return
    try:
        await page.locator(".chart-svg").wait_for(state="visible", timeout=10_000)
        await page.locator(".journal-item").first.wait_for(state="visible", timeout=10_000)
        bars = await page.locator(".chart-svg .chart-bar, .chart-svg rect").count()
        ctx.ok(f"#/checkin chart + journal rendered ({bars} rects)")
    except Exception as exc:  # noqa: BLE001
        ctx.fail(f"#/checkin chart/journal did not render: {exc}")
    # The app scrolls inside its own container, so full_page captures
    # only the viewport: grow the viewport to the content's height for
    # one whole-screen shot, then restore it.
    viewport = page.viewport_size
    content_h = await page.evaluate(
        "() => Math.max(...[...document.querySelectorAll('*')].map((el) => el.scrollHeight))"
    )
    await page.set_viewport_size({"width": viewport["width"], "height": min(int(content_h) + 80, 6000)})
    await page.wait_for_timeout(300)
    await page.screenshot(path=str(ctx.screenshot_path("checkin_full")))
    await page.set_viewport_size(viewport)
    await page.wait_for_timeout(300)

    focusable = page.locator(".chart-svg[tabindex='0']")
    if await focusable.count():
        await focusable.first.focus()
        await page.keyboard.press("End")
        await page.keyboard.press("ArrowLeft")
        await page.wait_for_timeout(200)
        tip = page.locator(".chart-tip")
        if not await tip.is_visible():
            ctx.fail("#/checkin chart tooltip not visible after keyboard focus")
        box = await page.locator(".chart").bounding_box()
        if box:
            await page.screenshot(
                path=str(ctx.screenshot_path("checkin_chart_tooltip")),
                clip={"x": max(box["x"] - 8, 0), "y": max(box["y"] - 44, 0),
                      "width": box["width"] + 16, "height": box["height"] + 52},
            )


async def run_pass(
    browser, harness: Harness, out_dir: Path, *, label: str, viewport: dict, color_scheme: str,
    do_actions: bool,
) -> Ctx:
    context = await browser.new_context(
        viewport=viewport, color_scheme=color_scheme, ignore_https_errors=True
    )
    await context.add_init_script(_INIT_SCRIPT)
    page = await context.new_page()
    ctx = Ctx(harness, out_dir, label)
    _wire_console_capture(page, ctx)

    try:
        await scenario_login(page, ctx)
        await page.screenshot(path=str(ctx.screenshot_path("login")))
        await scenario_chat(page, ctx)
        if do_actions:
            await scenario_state_due_action(page, ctx)
            await scenario_memory_add(page, ctx)
        await scenario_nav_screens(page, ctx)
        if do_actions:
            await scenario_checkin(page, ctx)
        await scenario_checkin_views(page, ctx)
    except Exception as exc:  # noqa: BLE001
        where = " <- ".join(
            f"{Path(frame.filename).name}:{frame.lineno}"
            for frame in reversed(traceback.extract_tb(exc.__traceback__))
        )
        ctx.fail(f"unhandled exception in scenario run: {type(exc).__name__}: {exc} (at {where})")
    finally:
        await context.close()

    if ctx.console_errors:
        ctx.fail(f"{len(ctx.console_errors)} console error(s): {ctx.console_errors[:5]}")
    if ctx.page_errors:
        ctx.fail(f"{len(ctx.page_errors)} page error(s): {ctx.page_errors[:5]}")
    if ctx.csp_violations:
        ctx.fail(f"{len(ctx.csp_violations)} CSP/Trusted-Types violation(s): {ctx.csp_violations[:5]}")

    return ctx


async def main_async(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "playwright is not installed in this interpreter. Run with:\n"
            "  uv run --with playwright python scripts/web_e2e.py",
            file=sys.stderr,
        )
        return 2

    chromium_path = _find_chromium()
    port = 8743

    with tempfile.TemporaryDirectory(prefix="anchor-e2e-cert-") as cert_dir_str:
        cert_dir = Path(cert_dir_str)
        cert_path, key_path = _generate_self_signed_cert(cert_dir)

        harness = Harness(
            port=port, cert_path=cert_path, key_path=key_path, admin_dsn=args.admin_dsn
        )
        print(f"starting scratch database {harness.scratch_db.db_name} ...")
        await harness.start()
        print(f"app listening on {harness.base_url}")

        all_ctx: list[Ctx] = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(executable_path=chromium_path, headless=True)
                try:
                    ctx_dark = await run_pass(
                        browser, harness, out_dir,
                        label="mobile_dark", viewport={"width": 390, "height": 844},
                        color_scheme="dark", do_actions=True,
                    )
                    all_ctx.append(ctx_dark)

                    ctx_light = await run_pass(
                        browser, harness, out_dir,
                        label="desktop_light", viewport={"width": 1280, "height": 800},
                        color_scheme="light", do_actions=False,
                    )
                    all_ctx.append(ctx_light)
                finally:
                    await browser.close()
        finally:
            print("stopping app and dropping scratch database ...")
            await harness.stop()

    print("\n=== summary ===")
    total_failures = 0
    for ctx in all_ctx:
        total_failures += len(ctx.failures)
        print(f"[{ctx.label}] failures: {len(ctx.failures)}")
        for f in ctx.failures:
            print(f"  FAIL: {f}")
    print(f"screenshots written under: {out_dir}")

    if total_failures:
        print(f"\n{total_failures} failure(s) -- see FAIL lines above.")
        return 1
    print("\nall scenarios passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT, help="directory for screenshots")
    parser.add_argument(
        "--admin-dsn",
        default=DEFAULT_ADMIN_DSN,
        help="admin Postgres DSN used to create/drop the scratch database",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
