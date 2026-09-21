"""aiogram Router: 1a has a single handler that echoes text back.

Handler order per plan section 6.4 is established here as placeholders:
commands and the pause path are TODOs for later milestones, not stubs
that do anything yet.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.types import Message


async def echo(message: Message) -> None:
    """1a's only behavior: echo the received text back verbatim."""
    if message.text is None:
        # TODO(phase-1b): stickers/photos/voice get a fixed reply per
        # plan section 6.4 step 3. 1a only handles text.
        return
    await message.answer(message.text)


def build_router() -> Router:
    """Build a fresh Router with 1a's handlers.

    A factory rather than a shared module-level instance, because a
    Router can only ever be attached to one Dispatcher — tests that
    build several Dispatchers each need their own Router instance.
    """
    router = Router(name="anchor")

    # TODO(phase-1b): /start, /out, /in, /state command handlers go
    # here, registered before the text handler below (plan section 6.4
    # step 1).

    # TODO(phase-1d): pause.match(text) branch goes here, before
    # turn.run() (plan section 6.4 step 2, and plan section 7).

    router.message()(echo)
    return router


# The single router instance used by the running app (app/main.py).
router = build_router()
