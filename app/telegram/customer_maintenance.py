"""Block customer-bot traffic while maintenance mode is on."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.database import repositories as repo
from app.database.database import get_session

log = logging.getLogger(__name__)


class CustomerMaintenanceMiddleware(BaseMiddleware):
    """When the customer bot is disabled, reply with the offline message and stop."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        with get_session() as session:
            enabled = repo.is_customer_bot_enabled(session)
            offline_msg = repo.get_customer_bot_disabled_message(session)
        if enabled:
            return await handler(event, data)

        try:
            if isinstance(event, CallbackQuery):
                await event.answer(offline_msg[:180], show_alert=True)
                if event.message:
                    await event.message.answer(offline_msg)
            elif isinstance(event, Message):
                await event.answer(offline_msg)
        except Exception:
            log.exception("failed to send customer maintenance notice")
        return None
