"""Shared handles so customer ↔ admin bots can talk.

Admin web runs in a daemon thread with its own asyncio loop. Aiogram Bot
sessions belong to the main bot loop — never call them directly from the web
thread. Use run_on_bot_loop / wake_renew_queue instead.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Iterable, TypeVar

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

log = logging.getLogger(__name__)

T = TypeVar("T")

admin_bot: Bot | None = None
customer_bot: Bot | None = None
renew_queue = None  # RenewQueue | None
bot_loop: asyncio.AbstractEventLoop | None = None


async def notify_admins_action_required(
    admin_ids: Iterable[int],
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> list[Any]:
    """Ping every admin that something needs their reaction."""
    bot = admin_bot
    if not bot:
        return []
    sent: list[Any] = []
    for admin_id in admin_ids:
        try:
            msg = await bot.send_message(
                int(admin_id), text, reply_markup=reply_markup
            )
            sent.append(msg)
        except Exception:
            log.exception("admin action notify failed admin=%s", admin_id)
    return sent


def set_bot_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global bot_loop
    bot_loop = loop


def wake_renew_queue() -> bool:
    """Wake renew queue from any thread/loop."""
    rq = renew_queue
    if rq is None:
        return False
    loop = bot_loop
    try:
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(rq.wake)
        else:
            rq.wake()
        return True
    except Exception:
        log.exception("wake renew queue failed")
        return False


async def run_on_bot_loop(
    coro: Coroutine[Any, Any, T], *, timeout: float | None = 180.0
) -> T:
    """Await coro on the main bot loop (safe when called from admin-web loop)."""
    loop = bot_loop
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if loop is None or current is loop:
        return await coro
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    wrapped = asyncio.wrap_future(fut)
    if timeout is None:
        return await wrapped
    return await asyncio.wait_for(wrapped, timeout=timeout)
