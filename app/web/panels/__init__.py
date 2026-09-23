"""The web control panels: State, Proposals (W2, roadmap section 4) and
Memory (W3).

Each submodule provides its own `register(app)` that adds its routes;
`register(app)` here just calls every panel's, so `app/web/routes.py`'s
`setup_web` has one call to make and a later milestone (W4 check-in, W5
research) only has to add one line here, not touch `setup_web` itself.

Every handler in this package follows the same shape app/web/routes.py's
handlers already do (`app/web/http.py`'s helpers, the CSRF/security
middleware already covers `/api/*`): `_session_token_valid` first, then
`_read_body`/validation, then `request.app["web_rate_limiter"].
check_panel_write()`, then core calls only -- `app/core/commands.py`,
`app/core/proposal.py` and `app/core/memory.py`, never a bare SQL write
against `user_state`, `proposal` or `memory` -- and finally
`hub.publish_invalidate(...)`. See each module's own docstring for the
one property specific to it.
"""

from __future__ import annotations

from aiohttp import web

from app.web.panels import memory as memory_panel
from app.web.panels import proposals as proposals_panel
from app.web.panels import state as state_panel


def register(app: web.Application) -> None:
    state_panel.register(app)
    proposals_panel.register(app)
    memory_panel.register(app)
