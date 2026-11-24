"""Require Telegram channel membership before using the customer bot."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from app.database import repositories as repo
from app.database.database import get_session

log = logging.getLogger(__name__)

DEFAULT_REQUIRED_CHANNEL = "fxonevpn"


def get_required_channel() -> str:
    with get_session() as session:
        raw = (repo.get_state(session, "required_channel") or "").strip()
    raw = raw or DEFAULT_REQUIRED_CHANNEL
    return raw.lstrip("@")


def set_required_channel(channel: str) -> str:
    val = (channel or "").strip().lstrip("@") or DEFAULT_REQUIRED_CHANNEL
    with get_session() as session:
        repo.set_state(session, "required_channel", val)
    return val


def is_channel_gate_enabled() -> bool:
    with get_session() as session:
        raw = (repo.get_state(session, "required_channel_gate") or "1").strip().lower()
    if not raw:
        return True
    return raw in {"1", "true", "yes", "on"}


def set_channel_gate_enabled(enabled: bool) -> bool:
    with get_session() as session:
        repo.set_state(session, "required_channel_gate", "1" if enabled else "0")
    return bool(enabled)


async def user_is_channel_member(bot: Bot, user_id: int, channel: str) -> bool:
    try:
        member = await bot.get_chat_member(f"@{channel}", user_id)
    except Exception:
        log.exception("channel membership check failed channel=%s user=%s", channel, user_id)
        # Fail open only if bot cannot see the channel (misconfig); otherwise block.
        return False
    return member.status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.RESTRICTED,
    }


def join_channel_keyboard(channel: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 عضویت در کانال",
                    url=f"https://t.me/{channel}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ عضو شدم — ادامه",
                    callback_data="cust:channel:check",
                )
            ],
        ]
    )


class ChannelMembershipMiddleware(BaseMiddleware):
    """Block customer bot usage until the user joins the required channel."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not is_channel_gate_enabled():
            return await handler(event, data)

        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            # Always allow the re-check button itself.
            if (event.data or "") == "cust:channel:check":
                return await handler(event, data)
        if user is None or user.is_bot:
            return await handler(event, data)

        bot: Bot | None = data.get("bot")
        if bot is None:
            return await handler(event, data)

        channel = get_required_channel()
        if await user_is_channel_member(bot, user.id, channel):
            return await handler(event, data)

        text = (
            "برای استفاده از ربات، ابتدا در کانال ما عضو شوید:\n"
            f"https://t.me/{channel}\n\n"
            "بعد از عضویت، دکمه «عضو شدم» را بزنید."
        )
        kb = join_channel_keyboard(channel)
        if isinstance(event, CallbackQuery):
            await event.answer("عضویت در کانال لازم است", show_alert=True)
            try:
                await event.message.answer(text, reply_markup=kb)
            except Exception:
                pass
            return None
        if isinstance(event, Message):
            await event.answer(text, reply_markup=kb)
            return None
        return await handler(event, data)
