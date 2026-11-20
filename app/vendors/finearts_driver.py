"""Drive FineArts (@FineArtsVPNBot) via the operator Telethon session."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from telethon import TelegramClient
from telethon.tl.custom.message import Message

from app.vendors.base import VendorCapabilities
from app.vendors.finearts_parser import parse_finearts
from app.vendors.netplus_driver import (
    POLL,
    DriveResult,
    PaymentInfo,
    buttons_of,
    find_data,
    find_product_callback_for_username,
    flat_buttons,
)

log = logging.getLogger(__name__)

FINEARTS_KEY = "finearts"

FINEARTS_CAPABILITIES = VendorCapabilities(
    key=FINEARTS_KEY,
    display_name="FineArts",
    bot_create=True,
    bot_renew=True,
    bot_change_link=True,
    bot_inventory_list=True,
    # Clash subscription-userinfo headers on /up/ (used/remain/total). Never scrape HTML page totals.
    url_userinfo=True,
)

# Our sell SKUs → discover live FineArts catalog buttons by GB in label.
FA_VOLUME_GB = {50, 100}
TETHERIAL_BOT = "Tetherial_bot"
TETHERIAL_PAY_CALLBACK = "iranpay3"  # «بات TetheRial»


def _is_status(msg: Message | None) -> bool:
    if not msg or not msg.message:
        return False
    t = msg.message
    return "وضعیت سرویس" in t and ("نام سرویس" in t or "نام کاربری" in t)


def _is_services_list(msg: Message | None) -> bool:
    if not msg:
        return False
    if _list_products(msg):
        return True
    text = msg.message or ""
    return "اشتراک های خریداری شده" in text or "سرویس‌های من" in text


def _list_products(msg: Message | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for b in flat_buttons(msg):
        d = b.get("callback_data") or ""
        if not d.startswith("product_"):
            continue
        username = (b.get("text") or "").strip("✨⭐ ").strip()
        if not username or "جستجو" in username or "بازگشت" in username:
            continue
        out.append(
            {
                "username": username,
                "id": d.split("_", 1)[1],
                "callback_data": d,
            }
        )
    return out


def _next_page(msg: Message | None) -> str | None:
    for b in flat_buttons(msg):
        t = b.get("text") or ""
        d = b.get("callback_data") or ""
        if "بعدی" in t and d.startswith("servicespage_"):
            return d
    return None


def _is_external_pay(msg: Message | None) -> bool:
    if not msg:
        return False
    text = msg.message or ""
    if "تراکنش شما ایجاد شد" in text and "پرداخت" in text:
        return True
    for b in flat_buttons(msg):
        url = b.get("url") or ""
        if "t.me/" in url and ("پرداخت" in (b.get("text") or "") or "pay" in url.lower()):
            return True
        # url buttons stored via Telethon as url attr — buttons_of may omit url
    # Telethon raw buttons
    if msg.buttons:
        for row in msg.buttons:
            for btn in row:
                url = getattr(btn, "url", None) or ""
                if "t.me/" in url:
                    return True
    return False


def _tetherial_url_from_msg(msg: Message | None) -> str | None:
    if not msg:
        return None
    if msg.buttons:
        for row in msg.buttons:
            for btn in row:
                url = getattr(btn, "url", None) or ""
                if "t.me/" in url and "tetherial" in url.lower():
                    return url
                if "t.me/" in url and ("پرداخت" in (getattr(btn, "text", None) or "") or "pay" in url.lower()):
                    return url
    text = msg.message or ""
    m = re.search(r"https?://t\.me/Tetherial_bot\S+", text, re.I)
    if m:
        return m.group(0).rstrip(").,]")
    m = re.search(r"https?://t\.me/\S+", text)
    return m.group(0).rstrip(").,]") if m else None


def _is_pay_methods(msg: Message | None) -> bool:
    if not msg:
        return False
    if find_data(msg, TETHERIAL_PAY_CALLBACK, exact=True):
        return True
    text = msg.message or ""
    return "روش‌های پرداخت" in text or "روش های پرداخت" in text


def _parse_tetherial_card(text: str, *, message_id: int | None = None) -> PaymentInfo | None:
    """Parse TetheRial «اطلاعات پرداخت» card + unique amount (rial)."""
    t = text or ""
    if "شماره کارت" not in t and "مبلغ واریز" not in t:
        return None
    card = None
    m = re.search(r"شماره کارت\s*:?\s*\n?\s*((?:\d[\d\s]{14,22}\d))", t)
    if m:
        card = re.sub(r"\s+", "", m.group(1))
    amount_rial = None
    m = re.search(r"مبلغ واریز\s*:?\s*([\d,]+)\s*ریال", t)
    if m:
        amount_rial = m.group(1).replace(",", "")
    holder = None
    m = re.search(r"دارنده کارت\s*:?\s*(.+)", t)
    if m:
        holder = m.group(1).strip()
    tracking = None
    m = re.search(r"کد پیگیری\s*:?\s*(\S+)", t)
    if m:
        tracking = m.group(1).strip()
    if not card or not amount_rial:
        return None
    # Show card-holder name to admin; keep tracking inside raw text for audits.
    display_holder = holder or tracking or "TetheRial"
    if holder and tracking and tracking not in t:
        t = f"{t}\n🔑 {tracking}"
    return PaymentInfo(
        amount_rial=amount_rial,
        card=card,
        holder=display_holder,
        receipt_callback=None,
        message_id=message_id,
        text=t,
    )


def _volume_gb_from_package(package_callback: str | None) -> int | None:
    raw = (package_callback or "").strip().lower()
    if not raw:
        return None
    if raw in ("fa_vol_50", "fa50", "50", "fa_50gb"):
        return 50
    if raw in ("fa_vol_100", "fa100", "100", "fa_100gb"):
        return 100
    m = re.search(r"(\d{2,3})", raw)
    if m:
        gb = int(m.group(1))
        if gb in FA_VOLUME_GB:
            return gb
    return None


def _find_volume_product_callback(msg: Message | None, gb: int) -> str | None:
    """Match catalog row like «🔶50 گیگ حجمی…» → prodcutservices_*."""
    if not msg:
        return None
    latin = str(int(gb))
    persian = latin.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    for b in flat_buttons(msg):
        text = b.get("text") or ""
        data = b.get("callback_data") or ""
        if not data:
            continue
        if latin not in text and persian not in text:
            continue
        if "گیگ" not in text:
            continue
        # Prefer حجمی rows; still accept if only one GB match exists.
        if "حجمی" in text or data.startswith("prodcutservices_") or data.startswith(
            "productservices_"
        ):
            return data
    return None


class FineArtsRenewDriver:
    """Concrete ``VendorBotDriver`` for FineArts."""

    vendor_key = FINEARTS_KEY
    capabilities = FINEARTS_CAPABILITIES

    def __init__(self, client: TelegramClient, bot_username: str) -> None:
        self.client = client
        self.bot_username = bot_username
        self.lock = asyncio.Lock()
        self._entity = None
        # Reuse NetPlus click/wait implementation via a thin delegate
        from app.vendors.netplus_driver import NetPlusRenewDriver

        self._ui = NetPlusRenewDriver(client, bot_username)
        # Share one lock so NetPlus + FineArts never interleave on same session
        # (RenewQueue serializes jobs; this is belt-and-suspenders for URL/status.)

    async def ensure_entity(self):
        if self._entity is None:
            self._entity = await self.client.get_entity(self.bot_username)
        self._ui._entity = self._entity
        return self._entity

    async def send_text(self, text: str) -> Message | None:
        await self.ensure_entity()
        return await self._ui.send_text(text)

    async def click_data(self, msg: Message | None, data: str) -> Message | None:
        await self.ensure_entity()
        return await self._ui.click_data(msg, data)

    async def _wait_bot(self, after_id: int, timeout: float = 15.0) -> Message | None:
        return await self._ui.wait_bot(after_id, timeout=timeout)

    async def _resolve_tetherial_payment(
        self, msg: Message, *, after_id: int
    ) -> DriveResult:
        """From FineArts pay-methods or pay-URL message → TetheRial card+unique amount.

        One iranpay3 click max, one Tetherial /start max — no loops that re-fire invoices.
        """
        cur = msg
        # Step A: choose TetheRial method if the chooser is showing.
        if _is_pay_methods(cur) and find_data(cur, TETHERIAL_PAY_CALLBACK, exact=True):
            nxt = await self.click_data(cur, TETHERIAL_PAY_CALLBACK)
            if not nxt:
                return DriveResult(False, "tetherial_method_click", error="FAIL_IRANPAY3")
            cur = nxt

        # Step B: FineArts «تراکنش ایجاد شد» with deep-link button.
        url = _tetherial_url_from_msg(cur)
        if not url and not _is_external_pay(cur):
            # Maybe still building the link
            nxt = await self._wait_bot(cur.id, timeout=12.0)
            if nxt:
                cur = nxt
                url = _tetherial_url_from_msg(cur)

        if not url:
            # Last chance: scan a few newer FineArts msgs once
            entity = await self.ensure_entity()
            for m in await self.client.get_messages(entity, limit=6):
                if m and not m.out and m.id > after_id:
                    if _is_pay_methods(m) and find_data(m, TETHERIAL_PAY_CALLBACK, exact=True):
                        cur = await self.click_data(m, TETHERIAL_PAY_CALLBACK) or m
                        url = _tetherial_url_from_msg(cur)
                        if url:
                            break
                    u = _tetherial_url_from_msg(m)
                    if u:
                        url = u
                        cur = m
                        break
        if not url:
            return DriveResult(False, "no_tetherial_url", error="FAIL_NO_TETHERIAL_URL")
        if int(getattr(cur, "id", 0) or 0) <= after_id and _is_external_pay(cur):
            # Stale pay bubble from an older job
            return DriveResult(False, "stale_external_pay", error="stale_pay")

        # Step C: open TetheRial once with start payload.
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        start_payload = (qs.get("start") or [None])[0]
        teth = await self.client.get_entity(f"@{TETHERIAL_BOT}")
        latest = (await self.client.get_messages(teth, limit=1))[0]
        before_tid = int(latest.id) if latest else 0
        await self.client.send_message(
            teth, f"/start {start_payload}" if start_payload else "/start"
        )

        # TetheRial often sends «در حال بررسی…» then the card card — wait for card fields.
        pay: PaymentInfo | None = None
        deadline = time.monotonic() + 35.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.7)
            for m in await self.client.get_messages(teth, limit=8):
                if not m or m.out or m.id <= before_tid:
                    continue
                parsed = _parse_tetherial_card(m.message or "", message_id=m.id)
                if parsed:
                    pay = parsed
                    break
            if pay:
                break
        if not pay:
            return DriveResult(False, "tetherial_no_card", error="FAIL_TETHERIAL_CARD")
        return DriveResult(True, "payment", payment=pay)

    async def _walk_finearts_to_tetherial(
        self, after: Message | None, *, after_id: int
    ) -> DriveResult:
        """Shared post-confirm walker for create/renew → TetheRial card payment."""
        cur = after
        for _ in range(14):
            if not cur:
                break
            if _is_pay_methods(cur) or _is_external_pay(cur) or _tetherial_url_from_msg(cur):
                return await self._resolve_tetherial_payment(cur, after_id=after_id)

            text = cur.message or ""
            # Pre-invoice / renew invoice confirm (once)
            confirm = None
            for b in flat_buttons(cur):
                d = b.get("callback_data") or ""
                t = b.get("text") or ""
                if not d:
                    continue
                if d in ("confirmandgetservice",) or any(
                    k in t for k in ("پرداخت و دریافت", "تایید", "پرداخت", "تمدید")
                ):
                    # Prefer explicit create confirm when present
                    if d == "confirmandgetservice" or "پرداخت و دریافت" in t:
                        confirm = d
                        break
                    if confirm is None:
                        confirm = d
            if confirm:
                nxt = await self.click_data(cur, confirm)
                cur = nxt or cur
                continue

            if "درحال ساخت لینک پرداخت" in text or "در حال ساخت" in text:
                cur = (await self._wait_bot(cur.id, timeout=12.0)) or cur
                continue

            if "فاکتور" in text or "پیش فاکتور" in text:
                nxt = await self._wait_bot(cur.id, timeout=8.0)
                if nxt and nxt.id != cur.id:
                    cur = nxt
                    continue
                # No confirm button found above
                return DriveResult(
                    False, "invoice_no_confirm", error="FAIL_INVOICE_NO_CONFIRM"
                )

            # Newer FineArts messages?
            entity = await self.ensure_entity()
            for m in await self.client.get_messages(entity, limit=6):
                if m and not m.out and m.id > after_id:
                    if (
                        _is_pay_methods(m)
                        or _is_external_pay(m)
                        or _tetherial_url_from_msg(m)
                    ):
                        return await self._resolve_tetherial_payment(
                            m, after_id=after_id
                        )
            return DriveResult(
                False, "unhandled_pay_walk", error=f"unhandled:{(text or '')[:80]}"
            )
        return DriveResult(False, "too_many_steps", error="walk_limit")

    async def open_status(self, username: str, service_id: str | None) -> Message | None:
        """Search from my-services list (FineArts has no /services shortcut)."""
        t0 = time.monotonic()
        menu = await self.send_text("/start")
        lst = None
        if menu and find_data(menu, "backorder", exact=True):
            lst = await self.click_data(menu, "backorder")
        if not lst:
            lst = await self.send_text("🛍 سرویس‌های من (تمدید و...)")
        if not lst:
            log.info("FA open_status %s no_list %.2fs", username, time.monotonic() - t0)
            return None

        search = None
        if find_data(lst, "searchservice", exact=True):
            search = await self.click_data(lst, "searchservice")
        typed = await self.send_text(username)
        msg = typed or search or lst

        product_cb = find_product_callback_for_username(
            msg, username, service_id=service_id
        )
        if product_cb:
            opened = await self.click_data(msg, product_cb)
            log.info(
                "FA open_status %s via exact product %.2fs",
                username,
                time.monotonic() - t0,
            )
            return opened

        if msg and _is_status(msg):
            parsed = parse_finearts(msg.message or "", buttons_of(msg))
            got = (parsed.username or "").strip().lower()
            want = (username or "").strip().lower()
            if got and got == want:
                log.info(
                    "FA open_status %s direct %.2fs", username, time.monotonic() - t0
                )
                return msg
            log.warning(
                "FA open_status refused direct card %r for request %r",
                parsed.username,
                username,
            )

        # Walk pages for exact product_ / service_id
        cur = lst if _is_services_list(lst) else msg
        if cur:
            for _ in range(20):
                if not cur:
                    break
                product_cb = find_product_callback_for_username(
                    cur, username, service_id=service_id
                )
                if product_cb:
                    opened = await self.click_data(cur, product_cb)
                    log.info(
                        "FA open_status %s via_page %.2fs",
                        username,
                        time.monotonic() - t0,
                    )
                    return opened
                nxt = _next_page(cur)
                if not nxt:
                    break
                cur = await self.click_data(cur, nxt)
        log.info("FA open_status %s miss %.2fs", username, time.monotonic() - t0)
        return None

    async def drive_renew(
        self,
        *,
        username: str,
        service_id: str | None,
        plan_mode: str,
        duration_callback: str | None = None,
        package_callback: str | None = None,
    ) -> DriveResult:
        """Extend current volume plan → TetheRial card + unique amount."""
        _ = (plan_mode, duration_callback, package_callback)
        async with self.lock:
            try:
                entity = await self.ensure_entity()
                latest = (await self.client.get_messages(entity, limit=1))[0]
                after_id = int(latest.id) if latest else 0

                status = await self.open_status(username, service_id)
                if not status or not find_data(status, "extend_"):
                    return DriveResult(False, "no_extend", error="FAIL_NO_EXTEND")

                extend = (
                    f"extend_{service_id}"
                    if service_id
                    and find_data(status, f"extend_{service_id}", exact=True)
                    else find_data(status, "extend_")
                )
                after = await self.click_data(status, extend)  # type: ignore[arg-type]
                if not after:
                    return DriveResult(False, "extend_failed", error="FAIL_EXTEND_CLICK")
                return await self._walk_finearts_to_tetherial(after, after_id=after_id)
            except Exception as exc:
                log.exception("FineArts drive_renew failed for %s", username)
                return DriveResult(False, "exception", error=str(exc))

    async def drive_create(
        self,
        *,
        username: str,
        duration_callback: str | None,
        package_callback: str | None,
    ) -> DriveResult:
        """Buy حجمی 50/100 GB → confirm → TetheRial card payment.

        ``package_callback``: fa_vol_50 / fa_vol_100 (or plan key fa_50gb…).
        ``duration_callback`` unused (time-unlimited).
        """
        _ = duration_callback
        gb = _volume_gb_from_package(package_callback)
        if gb is None:
            return DriveResult(
                False, "bad_package", error="FAIL_FA_PACKAGE_NEED_50_OR_100"
            )
        uname = (username or "").strip()
        if not uname or not re.fullmatch(r"[A-Za-z0-9_]+", uname) or uname.startswith(
            "_"
        ) or uname.endswith("_"):
            return DriveResult(False, "bad_username", error="FAIL_FA_USERNAME")

        async with self.lock:
            try:
                entity = await self.ensure_entity()
                latest = (await self.client.get_messages(entity, limit=1))[0]
                after_id = int(latest.id) if latest else 0

                menu = await self.send_text("/start")
                buy = None
                if menu and find_data(menu, "buy", exact=True):
                    buy = await self.click_data(menu, "buy")
                if not buy:
                    buy = await self.send_text("🔐 خرید اشتراک")
                if not buy:
                    return DriveResult(False, "no_buy", error="FAIL_BUY_OPEN")

                # Some accounts show اقتصادی/حجمی categories first.
                cat = None
                for b in flat_buttons(buy):
                    if "حجمی" in (b.get("text") or "") and b.get("callback_data"):
                        # Category button (not a concrete 50/100 product row)
                        data = b["callback_data"]
                        if not data.startswith("prodcutservices_") and not data.startswith(
                            "productservices_"
                        ):
                            cat = await self.click_data(buy, data)
                            break
                catalog = cat or buy

                product = _find_volume_product_callback(catalog, gb)
                if not product:
                    # Flat catalog may already be on buy
                    product = _find_volume_product_callback(buy, gb)
                if not product:
                    return DriveResult(
                        False, "no_product", error=f"FAIL_NO_FA_{gb}GB_PRODUCT"
                    )

                ask = await self.click_data(catalog if cat else buy, product)
                if not ask:
                    return DriveResult(False, "product_click", error="FAIL_PRODUCT_CLICK")

                # Username prompt (or still on catalog)
                if "نام کاربري" not in (ask.message or "") and "نام کاربری" not in (
                    ask.message or ""
                ):
                    # Maybe already asked elsewhere — still send username once.
                    pass

                before = (await self.client.get_messages(entity, limit=1))[0].id
                await self.client.send_message(entity, uname)
                after_user = await self._wait_bot(before, timeout=15.0)
                if not after_user:
                    after_user = (await self.client.get_messages(entity, limit=1))[0]

                text = after_user.message or ""
                if any(
                    k in text
                    for k in ("نامعتبر", "اشتباه", "قبلا", "وجود دارد", "تکراری")
                ):
                    return DriveResult(
                        False, "username_rejected", error=f"FAIL_USER:{(text or '')[:80]}"
                    )

                return await self._walk_finearts_to_tetherial(
                    after_user, after_id=after_id
                )
            except Exception as exc:
                log.exception("FineArts drive_create failed for %s", username)
                return DriveResult(False, "exception", error=str(exc))

    async def click_receipt(
        self, receipt_callback: str, payment_message_id: int | None
    ) -> bool:
        # External Tetherial pay — no in-bot receipt callback.
        _ = (receipt_callback, payment_message_id)
        return False

    async def send_receipt_photo(
        self, file_bytes: bytes, filename: str = "receipt.jpg"
    ) -> bool:
        # Payments happen in Tetherial bot; forwarding a photo here is unused.
        _ = (file_bytes, filename)
        return False

    async def click_back_to_list(self) -> None:
        # After renew/create success leave the vendor success message as last state.
        log.info("FA click_back_to_list skipped (keep success message)")
        return

    async def refresh_subscription_status(
        self, username: str, service_id: str | None
    ) -> dict[str, Any] | None:
        async with self.lock:
            status = await self.open_status(username, service_id)
            if not status:
                return None
            parsed = parse_finearts(status.message or "", buttons_of(status))
            data = parsed.as_dict()
            got = (data.get("username") or "").strip().lower()
            want = (username or "").strip().lower()
            if got and got != want:
                log.warning(
                    "FA refresh refused: opened %r while requesting %r",
                    data.get("username"),
                    username,
                )
                return None
            if not data.get("username"):
                data["username"] = username
            if service_id and not data.get("vendor_service_id"):
                data["vendor_service_id"] = service_id
            data["status_text"] = status.message or ""
            data["last_refresh_method"] = "provider_bot"
            return data

    async def _click_service_action(
        self, username: str, service_id: str | None, prefix: str
    ) -> Message | None:
        status = await self.open_status(username, service_id)
        if not status:
            return None
        data = find_data(status, prefix, exact=False)
        if not data and service_id:
            data = find_data(status, f"{prefix}{service_id}", exact=True)
        if not data:
            return None
        return await self.click_data(status, data)

    async def fetch_subscription_url(
        self, username: str, service_id: str | None
    ) -> str | None:
        async with self.lock:
            after = await self._click_service_action(
                username, service_id, "subscriptionurl_"
            )
            if not after or not after.message:
                return None
            m = re.search(r"https?://\S+", after.message)
            return m.group(0).rstrip(").,]") if m else None

    async def fetch_status_and_subscription_url(
        self, username: str, service_id: str | None
    ) -> tuple[dict[str, Any] | None, str | None]:
        async with self.lock:
            status = await self.open_status(username, service_id)
            if not status:
                return None, None
            parsed = parse_finearts(status.message or "", buttons_of(status))
            data = parsed.as_dict()
            if not data.get("username"):
                data["username"] = username
            if service_id and not data.get("vendor_service_id"):
                data["vendor_service_id"] = service_id
            data["status_text"] = status.message or ""
            url = None
            link_data = find_data(status, "subscriptionurl_", exact=False)
            if not link_data and service_id:
                link_data = find_data(
                    status, f"subscriptionurl_{service_id}", exact=True
                )
            if link_data:
                after = await self.click_data(status, link_data)
                if after and after.message:
                    m = re.search(r"https?://\S+", after.message)
                    if m:
                        url = m.group(0).rstrip(").,]")
            return data, url

    async def change_subscription_link(
        self, username: str, service_id: str | None = None
    ) -> str | None:
        async with self.lock:
            after = await self._click_service_action(username, service_id, "changelink_")
            if not after:
                return None
            text = after.message or ""
            m = re.search(r"https?://\S+", text)
            if m:
                return m.group(0).rstrip(").,]")
            url_msg = await self._click_service_action(
                username, service_id, "subscriptionurl_"
            )
            if url_msg and url_msg.message:
                m2 = re.search(r"https?://\S+", url_msg.message)
                return m2.group(0).rstrip(").,]") if m2 else None
            return None

    async def sync_from_recent_history(
        self, *, limit: int = 300
    ) -> list[dict[str, str]]:
        entity = await self.ensure_entity()
        msgs = await self.client.get_messages(entity, limit=limit)
        seen: dict[str, dict[str, str]] = {}
        for m in msgs:
            if not m or m.out:
                continue
            for p in _list_products(m):
                sid = p.get("id") or ""
                if sid and sid not in seen:
                    seen[sid] = p
        return list(seen.values())

    async def sync_service_inventory(self, max_pages: int = 50) -> list[dict[str, str]]:
        """Walk FineArts my-services list. Never use chat history for inventory."""
        async with self.lock:
            t0 = time.monotonic()
            menu = await self.send_text("/start")
            msg = None
            if menu and find_data(menu, "backorder", exact=True):
                msg = await self.click_data(menu, "backorder")
            if not msg or not _list_products(msg):
                preview = ((msg.message if msg else "") or "")[:160].replace("\n", " | ")
                raise RuntimeError(
                    "FineArts sync failed: service list did not open "
                    f"(chat history is not used). Preview: {preview or '—'}"
                )

            seen: set[str] = set()
            collected: list[dict[str, str]] = []
            for _page in range(max_pages):
                if not msg:
                    break
                for p in _list_products(msg):
                    sid = p.get("id") or ""
                    if sid and sid not in seen:
                        seen.add(sid)
                        p["source"] = "list"
                        collected.append(p)
                nxt = _next_page(msg)
                if not nxt:
                    break
                msg = await self.click_data(msg, nxt)
                await asyncio.sleep(0.3)
            log.info(
                "FA SYNC list count=%s pages<=%s in %.1fs",
                len(collected),
                max_pages,
                time.monotonic() - t0,
            )
            return collected
