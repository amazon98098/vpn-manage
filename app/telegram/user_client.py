"""Telethon client for the personal Telegram account that owns NetPlus."""
from __future__ import annotations

import logging

from telethon import TelegramClient

from app.config import Settings

log = logging.getLogger(__name__)


def build_user_client(settings: Settings) -> TelegramClient:
    client = TelegramClient(
        str(settings.user_session),
        settings.api_id,
        settings.api_hash,
    )
    client.flood_sleep_threshold = 120
    return client


async def ensure_authorized(client: TelegramClient) -> None:
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit(
            "User Telegram session is not authorized. "
            "Log in once with the probe tools, then point USER_SESSION at that file."
        )
    me = await client.get_me()
    log.info("User client ready as %s (%s)", me.username or me.first_name, me.id)


async def ensure_connected(client: TelegramClient) -> None:
    """Reconnect Telethon if the MTProto session dropped (common after net blips)."""
    try:
        connected = client.is_connected()
    except Exception:
        connected = False
    if connected:
        return
    log.warning("User Telegram client disconnected — reconnecting")
    await client.connect()
    if not client.is_connected():
        raise ConnectionError("telethon_reconnect_failed")
    if not await client.is_user_authorized():
        raise ConnectionError("telethon_session_unauthorized")
    log.info("User Telegram client reconnected")
