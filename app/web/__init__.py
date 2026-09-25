"""The web chat: a second, browser-based front end onto the same bot.

Track 1 (this package's transport half) makes web messages flow through
the *existing* turn pipeline unchanged, by disguising them as Telegram
Updates:

    POST /api/send --> app/web/ingress.py builds a synthetic Update with
    a negative update_id --> app/db/queue.py's enqueue_web stores it in
    telegram_update (the negative id itself marks it web-origin -- see
    app/db/models.py's TelegramUpdate/WebUpdate) --> app/worker.py's
    single claim loop claims it exactly like a Telegram row, but feeds
    it to a `web_bot` whose session is app/web/sink.py's WebSinkSession
    instead of a real HTTP client --> every send that turn makes becomes
    a WebHub event (app/web/hub.py) instead of a Telegram API call -->
    track 2's GET /api/events streams those events out over SSE.

app/web/tail.py is the other half of delivery: it mirrors Telegram-
origin and proactive `message` rows into the same hub, so the web view
is a second window onto the whole conversation, not just a private one.

Nothing in app/core/* changes for any of this -- see the module
docstrings in app/web/sink.py and app/web/hub.py for why swapping the
Bot's session is sufficient. This package is inert unless
WEB_UI_ENABLED, and track 2 (app/web/routes.py, auth.py, security.py,
ratelimit.py, app/main.py) is what turns it on.
"""

from __future__ import annotations
