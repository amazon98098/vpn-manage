"""Aiogram 3 admin bot setup."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup

from app.config import Settings

log = logging.getLogger(__name__)


def build_bot(settings: Settings) -> Bot:
    session = AiohttpSession(proxy=settings.bot_proxy, timeout=120) if settings.bot_proxy else AiohttpSession(timeout=120)
    return Bot(
        token=settings.admin_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher() -> Dispatcher:
    return Dispatcher()


async def notify_admins(
    bot: Bot,
    admin_ids: frozenset[int],
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception:
            log.exception("Could not notify admin %s", admin_id)
