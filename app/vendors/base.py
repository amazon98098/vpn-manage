"""Vendor adapter boundary (Phase 9 — light multi-provider readiness).

New providers plug in by implementing the same bot-driver surface and registering
capabilities. Concrete adapters today: NetPlus and FineArts.

Locked ownership decision: usable subscription usernames are **global** across
vendors (one live usable name in the whole shop inventory), not vendor-scoped.
Identity for ownership remains internal ``subscriptions.id`` (+ ``vendor_id``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class VendorCapabilities:
    """What this vendor can do in our stack (not a full second-provider impl)."""

    key: str
    display_name: str
    bot_create: bool = True
    bot_renew: bool = True
    bot_change_link: bool = True
    bot_inventory_list: bool = True
    # HTTP subscription-userinfo from stored URL (vs Telegram bot status).
    url_userinfo: bool = True


class VendorBotDriver(Protocol):
    """Minimal Telethon/UI driver surface used by renew queue + change-link."""

    vendor_key: str

    async def drive_renew(
        self,
        *,
        username: str,
        service_id: str | None,
        plan_mode: str,
        duration_callback: str | None = None,
        package_callback: str | None = None,
    ) -> Any: ...

    async def drive_create(
        self,
        *,
        username: str,
        duration_callback: str | None,
        package_callback: str | None,
    ) -> Any: ...

    async def change_subscription_link(
        self, username: str, service_id: str | None = None
    ) -> str | None: ...
