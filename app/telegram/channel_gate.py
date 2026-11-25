"""Require Telegram channel membership before using the customer bot."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Literal

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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

# Avoid spamming logs / admins when the bot is not a channel admin.
_last_misconfig_log_at = 0.0
_MISCONFIG_LOG_EVERY_SEC = 300.0

MembershipResult = Literal["member", "not_member", "check_failed"]


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


def _note_misconfig(channel: str, detail: str) -> None:
    global _last_misconfig_log_at
    now = time.monotonic()
    if now - _last_misconfig_log_at < _MISCONFIG_LOG_EVERY_SEC:
        return
    _last_misconfig_log_at = now
    log.warning(
        "Channel gate misconfigured for @%s (%s). "
        "Add the customer bot as a channel admin so membership can be checked. "
        "Failing open until fixed.",
        channel,
        detail,
    )


async def check_channel_membership(
    bot: Bot, user_id: int, channel: str
) -> MembershipResult:
    """Return member / not_member / check_failed (bot cannot read members)."""
    chat = f"@{channel}"
    try:
        member = await bot.get_chat_member(chat, user_id)
    except TelegramBadRequest as exc:
        msg = (getattr(exc, "message", None) or str(exc)).lower()
        if (
            "member list is inaccessible" in msg
            or "chat not found" in msg
            or "chat_admin_required" in msg
            or "not enough rights" in msg
        ):
            _note_misconfig(channel, msg)
            return "check_failed"
        log.warning(
            "channel membership bad request channel=%s user=%s: %s",
            channel,
            user_id,
            exc,
        )
        return "not_member"
    except TelegramForbiddenError as exc:
        _note_misconfig(channel, str(exc))
        return "check_failed"
    except Exception:
        log.exception(
            "channel membership check failed channel=%s user=%s", channel, user_id
        )
        # Unknown failure — fail open so a Telegram glitch does not lock everyone out.
        return "check_failed"

    if member.status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.RESTRICTED,
    }:
        return "member"
    return "not_member"


async def user_is_channel_member(bot: Bot, user_id: int, channel: str) -> bool:
    return await check_channel_membership(bot, user_id, channel) == "member"


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

        # Panel/admins always bypass the customer channel gate.
        settings = data.get("settings")
        admin_ids = getattr(settings, "admin_ids", None) if settings else None
        if admin_ids and user.id in admin_ids:
            return await handler(event, data)

        bot: Bot | None = data.get("bot")
        if bot is None:
            return await handler(event, data)

        channel = get_required_channel()
        result = await check_channel_membership(bot, user.id, channel)
        if result in {"member", "check_failed"}:
            # check_failed = bot is not channel admin / cannot read members → fail open.
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
