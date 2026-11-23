"""Listen to vendor private chats and push alerts / advance renew queue."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from aiogram.types import InlineKeyboardMarkup
from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from app.config import Settings
from app.database.database import get_session
from app.database import repositories as repo
from app.monitoring.notifications import (
    alert_keyboard,
    deleted_service_customer_keyboard,
    format_alert,
)
from app.vendors.finearts_parser import parse_finearts
from app.vendors.netplus_parser import parse_netplus

log = logging.getLogger(__name__)

NotifyFn = Callable[[str, InlineKeyboardMarkup | None], Awaitable[None]]


def _buttons(msg: Message) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    if not msg.buttons:
        return rows
    for row in msg.buttons:
        items = []
        for btn in row:
            item = {"text": btn.text or ""}
            if getattr(btn, "data", None) is not None:
                item["callback_data"] = btn.data.decode("utf-8", errors="replace")
            if getattr(btn, "url", None):
                item["url"] = btn.url
            items.append(item)
        rows.append(items)
    return rows


class VendorChatListener:
    """One Telethon handler set for a single vendor bot chat."""

    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        notify: NotifyFn,
        *,
        vendor_key: str,
        vendor_name: str,
        bot_username: str,
        parse_fn,
        renew_queue=None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.notify = notify
        self.renew_queue = renew_queue
        self.vendor_key = vendor_key
        self.vendor_name = vendor_name
        self.bot_username = bot_username
        self.parse_fn = parse_fn
        self._entity = None
        self._vendor_id: int | None = None

    async def start(self) -> None:
        self._entity = await self.client.get_entity(self.bot_username)
        with get_session() as session:
            vendor = repo.ensure_vendor(
                session,
                key=self.vendor_key,
                name=self.vendor_name,
                username=self.bot_username,
            )
            repo.set_vendor_chat_id(session, vendor, self._entity.id)
            self._vendor_id = vendor.id

        self.client.add_event_handler(
            self._on_new,
            events.NewMessage(chats=self._entity, incoming=True),
        )
        self.client.add_event_handler(
            self._on_edit,
            events.MessageEdited(chats=self._entity, incoming=True),
        )
        log.info(
            "Listening to %s chat id=%s (@%s)",
            self.vendor_name,
            self._entity.id,
            str(self.bot_username).lstrip("@"),
        )
        await self.backfill_recent()

    async def backfill_recent(self, limit: int = 80) -> None:
        assert self._entity is not None
        max_age = timedelta(hours=self.settings.alert_max_age_hours)
        now = datetime.now(timezone.utc)
        count = 0
        async for msg in self.client.iter_messages(self._entity, limit=limit):
            if msg.out or not msg.message:
                continue
            msg_date = msg.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            age_ok = (now - msg_date) <= max_age
            await self._process(msg, notify=age_ok)
            count += 1
        log.info(
            "Backfill scanned %s recent %s messages", count, self.vendor_name
        )

    async def _on_new(self, event: events.NewMessage.Event) -> None:
        await self._process(event.message, notify=True)

    async def _on_edit(self, event: events.MessageEdited.Event) -> None:
        await self._process(event.message, notify=False)

    async def _process(self, msg: Message, notify: bool) -> None:
        text = msg.message or ""
        if not text.strip():
            return
        parsed = self.parse_fn(text, _buttons(msg))
        received_at = msg.date.replace(tzinfo=None) if msg.date else datetime.utcnow()

        should_notify = False
        customer_delete_notice = None
        payload = parsed.as_dict()
        # Keep raw vendor text so renew queue can pick subscription URL from
        # create/renew success without navigating away from that message.
        payload["status_text"] = text
        # Normalize toman → rial when parser only filled amount_toman.
        if payload.get("amount_rial") is None and payload.get("amount_toman"):
            try:
                payload["amount_rial"] = int(payload["amount_toman"]) * 10
            except (TypeError, ValueError):
                pass

        with get_session() as session:
            vendor = repo.ensure_vendor(
                session,
                self.vendor_key,
                self.vendor_name,
                self.bot_username,
            )
            row = repo.save_incoming(
                session,
                vendor=vendor,
                telegram_message_id=msg.id,
                text=text,
                kind=parsed.kind,
                parsed=payload,
                received_at=received_at,
            )
            if row is None:
                return

            deleted_owner = None
            suppress_ending = False
            uname = (payload.get("username") or "").strip()
            if parsed.kind == "alert_deleted":
                if uname:
                    # Remove inventory/ownership locally, then notify owner.
                    deleted_owner = repo.remove_subscription_after_provider_delete(
                        session, uname
                    )
            elif parsed.kind != "unknown":
                if parsed.kind in {"alert_days", "alert_mb"} and uname:
                    # Vendor already deleted this username recently → hush.
                    suppress_h = repo.get_admin_vendor_deleted_suppress_hours(session)
                    if repo.vendor_username_deleted_recently(
                        session, uname, within_hours=suppress_h
                    ):
                        suppress_ending = True
                if not suppress_ending:
                    repo.upsert_subscription_from_alert(
                        session, vendor, payload, received_at
                    )

            admin_alert_kinds = {"alert_days", "alert_mb", "alert_deleted"}
            success_kinds = {"renew_ok", "create_ok"}
            if parsed.kind in admin_alert_kinds and notify and parsed.notify:
                should_notify = True
                if parsed.kind in {"alert_days", "alert_mb"}:
                    if suppress_ending or (
                        uname
                        and not repo.should_notify_admin_vendor_ending_alert(
                            session,
                            username=uname,
                            message_id=row.id,
                        )
                    ):
                        should_notify = False
            elif (
                parsed.kind in success_kinds
                and notify
                and self.settings.notify_success
            ):
                should_notify = True

            if should_notify:
                repo.mark_notified(session, row)

            customer_delete_notice = None
            if deleted_owner and notify:
                customer_delete_notice = {
                    "telegram_id": deleted_owner["telegram_id"],
                    "text": repo.get_bot_message(
                        session,
                        "alert_service_deleted",
                        username=deleted_owner["username"],
                    ),
                }

        if self.renew_queue and parsed.kind in {
            "payment_instructions",
            "tx_bot_ok",
            "renew_ok",
            "create_ok",
        }:
            try:
                await self.renew_queue.on_vendor_event(parsed.kind, payload)
            except Exception:
                log.exception(
                    "Failed to advance renew job from %s event", self.vendor_key
                )

        if parsed.kind in {"wallet_credit", "tx_bot_ok"}:
            try:
                from app.services.vendor_direct_pay import try_confirm_from_vendor_event

                await try_confirm_from_vendor_event(parsed.kind, payload)
            except Exception:
                log.exception(
                    "Failed vendor-direct confirm from %s event", self.vendor_key
                )

        if should_notify:
            try:
                await self.notify(
                    format_alert(payload, vendor_name=self.vendor_name),
                    alert_keyboard(
                        payload.get("username"), kind=parsed.kind
                    ),
                )
            except Exception:
                log.exception("Failed to push admin notification")

        if customer_delete_notice:
            try:
                from app.telegram import customer_bridge

                bot = customer_bridge.customer_bot
                if bot:
                    await bot.send_message(
                        customer_delete_notice["telegram_id"],
                        customer_delete_notice["text"],
                        reply_markup=deleted_service_customer_keyboard(),
                    )
            except Exception:
                log.exception(
                    "Failed to notify customer about deleted service %s",
                    payload.get("username"),
                )


class NetPlusListener(VendorChatListener):
    """Back-compat wrapper for NetPlus-only construction."""

    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        notify: NotifyFn,
        renew_queue=None,
    ) -> None:
        super().__init__(
            client,
            settings,
            notify,
            vendor_key="netplus",
            vendor_name="NetPlus",
            bot_username=settings.netplus_bot,
            parse_fn=parse_netplus,
            renew_queue=renew_queue,
        )


def make_finearts_listener(
    client: TelegramClient,
    settings: Settings,
    notify: NotifyFn,
    renew_queue=None,
) -> VendorChatListener:
    return VendorChatListener(
        client,
        settings,
        notify,
        vendor_key="finearts",
        vendor_name="FineArts",
        bot_username=settings.finearts_bot,
        parse_fn=parse_finearts,
        renew_queue=renew_queue,
    )
