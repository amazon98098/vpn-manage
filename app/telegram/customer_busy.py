"""Serialize customer bot actions per user; show a short wait notice while busy."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatAction
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger(__name__)

_locks: dict[int, asyncio.Lock] = {}
_wait_msg: dict[int, tuple[int, int]] = {}  # uid -> (chat_id, message_id)


def _lock_for(uid: int) -> asyncio.Lock:
    lock = _locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[uid] = lock
    return lock


async def _delete_wait(bot: Bot | None, uid: int) -> None:
    pair = _wait_msg.pop(uid, None)
    if not pair or bot is None:
        return
    chat_id, message_id = pair
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def _notify_busy(event: TelegramObject, bot: Bot | None, uid: int) -> None:
    if bot is None:
        return
    try:
        if isinstance(event, CallbackQuery):
            await event.answer("لطفاً صبر نمایید…", show_alert=False)
            chat = event.message.chat if event.message else None
            if chat is None:
                return
            try:
                await bot.send_chat_action(chat.id, ChatAction.TYPING)
            except Exception:
                pass
            if uid in _wait_msg:
                return
            sent = await bot.send_message(chat.id, "⏳ لطفاً صبر نمایید…")
            _wait_msg[uid] = (chat.id, sent.message_id)
            return
        if isinstance(event, Message):
            try:
                await bot.send_chat_action(event.chat.id, ChatAction.TYPING)
            except Exception:
                pass
            if uid in _wait_msg:
                return
            sent = await event.answer("⏳ لطفاً صبر نمایید…")
            _wait_msg[uid] = (event.chat.id, sent.message_id)
    except Exception:
        log.debug("busy notify failed uid=%s", uid, exc_info=True)


class CustomerBusyMiddleware(BaseMiddleware):
    """One in-flight handler per customer; ignore stacked taps with a wait notice."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        if user is None or user.is_bot:
            return await handler(event, data)

        uid = int(user.id)
        lock = _lock_for(uid)
        bot: Bot | None = data.get("bot")
        if lock.locked():
            await _notify_busy(event, bot, uid)
            return None

        async with lock:
            try:
                if bot is not None:
                    try:
                        chat_id = None
                        if isinstance(event, Message):
                            chat_id = event.chat.id
                        elif isinstance(event, CallbackQuery) and event.message:
                            chat_id = event.message.chat.id
                        if chat_id is not None:
                            await bot.send_chat_action(chat_id, ChatAction.TYPING)
                    except Exception:
                        pass
                return await handler(event, data)
            finally:
                await _delete_wait(bot, uid)
