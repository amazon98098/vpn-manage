"""Drive NetPlus renew UI via the operator Telethon session (Phase 2).

Registered as the ``netplus`` vendor bot driver (Phase 9 adapter seam).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any  # noqa: F401 — used by refresh return type

import time

from telethon import TelegramClient, events
from telethon.errors import DataInvalidError, FloodWaitError
from telethon.tl.custom.message import Message

from app.vendors.base import VendorCapabilities
from app.vendors.registry import DEFAULT_VENDOR_KEY

log = logging.getLogger(__name__)

# Keep polls gentle — sub-100ms get_messages loops trigger Telegram flood-waits.
POLL = 0.25
SETTLE = 0.2
CURRENT_PLAN = "exntedagei"
CONFIRM = "confirmserivce"
CARD = "cart_to_offline"

NETPLUS_CAPABILITIES = VendorCapabilities(
    key=DEFAULT_VENDOR_KEY,
    display_name="NetPlus",
    bot_create=True,
    bot_renew=True,
    bot_change_link=True,
    bot_inventory_list=True,
    url_userinfo=True,
)


def is_wallet_insufficient(text: str | None) -> bool:
    t = text or ""
    return (
        "موجودی کیف پول کافی نیست" in t
        or "موجودی حساب شما کافی نیست" in t
        or ("موجودی" in t and "کافی نیست" in t)
        or "روش پرداخت" in t
        or "روش‌های پرداخت" in t
        or "روش های پرداخت" in t
    )


def find_card_button(msg: Message | None) -> str | None:
    if find_data(msg, CARD, exact=True):
        return CARD
    for b in flat_buttons(msg):
        t = b.get("text") or ""
        d = b.get("callback_data") or ""
        if "کارت به کارت" in t or "کارت‌به‌کارت" in t or d == CARD:
            return d or CARD
    return None


@dataclass
class PaymentInfo:
    amount_rial: str | None
    card: str | None
    holder: str | None
    receipt_callback: str | None
    message_id: int | None
    text: str


@dataclass
class DriveResult:
    ok: bool
    stage: str
    payment: PaymentInfo | None = None
    packages: list[dict[str, str]] | None = None
    error: str | None = None


def buttons_of(msg: Message | None) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    if not msg or not msg.buttons:
        return rows
    for row in msg.buttons:
        items = []
        for btn in row:
            item: dict[str, str] = {"text": btn.text or ""}
            if getattr(btn, "data", None) is not None:
                item["callback_data"] = btn.data.decode("utf-8", errors="replace")
            items.append(item)
        rows.append(items)
    return rows


def flat_buttons(msg: Message | None) -> list[dict[str, str]]:
    return [b for row in buttons_of(msg) for b in row]


def find_data(msg: Message | None, needle: str, exact: bool = False) -> str | None:
    for b in flat_buttons(msg):
        d = b.get("callback_data") or ""
        if exact and d == needle:
            return d
        if not exact and d.startswith(needle):
            return d
    return None


def _button_username(text: str | None) -> str:
    """Strip list icons / decorations from a product button label."""
    t = (text or "").strip()
    for ch in ("✨", "💎", "♾️", "🎨", "⚠️", "🪫"):
        t = t.replace(ch, "")
    return t.strip()


def find_product_callback_for_username(
    msg: Message | None, username: str, *, service_id: str | None = None
) -> str | None:
    """Resolve product_ callback by exact username only (never NetPlus id / substring).

    ``service_id`` is ignored — usernames are unique enough; id matching caused
    wrong-service opens for similarly named accounts.
    """
    _ = service_id
    if not msg:
        return None
    want = (username or "").strip().lower()
    if not want:
        return None
    for b in flat_buttons(msg):
        d = b.get("callback_data") or ""
        if not d.startswith("product_"):
            continue
        label = _button_username(b.get("text")).lower()
        if label == want:
            return d
    return None


def is_payment(msg: Message | None) -> bool:
    if not msg or not msg.message:
        return False
    t = msg.message
    return ("شماره کارت" in t and "ریال" in t) or (
        "مبلغ" in t and "ریال" in t and ("کارت" in t or find_data(msg, "sendresidcart"))
    )


def extract_pay(text: str) -> dict[str, str | None]:
    amount = None
    card = None
    holder = None
    m = re.search(r"مبلغ(?:\s*دقیق)?\s*:\s*([\d,]+)\s*ریال", text or "")
    if m:
        amount = m.group(1).replace(",", "")
    m = re.search(r"شماره کارت\s*:\s*(\d{16})", text or "")
    if m:
        card = m.group(1)
    if not card:
        m = re.search(r"\b(\d{16})\b", text or "")
        card = m.group(1) if m else None
    m = re.search(r"به نام:\s*(.+)", text or "")
    if m:
        holder = m.group(1).strip()
    return {"amount_rial": amount, "card": card, "holder": holder}


def package_buttons(msg: Message | None, *, create: bool = False) -> list[dict[str, str]]:
    out = []
    for b in flat_buttons(msg):
        d = b.get("callback_data") or ""
        if create:
            if d.startswith("prodcutservices_") or d.startswith("productservices_"):
                out.append({"text": (b.get("text") or "").strip(), "callback_data": d})
        elif d.startswith("serviceextendselect_"):
            out.append({"text": (b.get("text") or "").strip(), "callback_data": d})
    return out


VENDOR_ERROR_SNIPPETS = (
    ("نام کاربری قبلا", "username_taken"),
    ("فرمت نام کاربری", "username_invalid"),
    ("امکان تمدید وجود ندارد", "renew_not_possible"),
    ("ربات خاموش", "bot_off"),
    ("سفارش یافت نشد", "order_not_found"),
)


def detect_vendor_error(text: str | None) -> str | None:
    t = text or ""
    for needle, code in VENDOR_ERROR_SNIPPETS:
        if needle in t:
            return code
    return None


class NetPlusRenewDriver:
    """Concrete ``VendorBotDriver`` for NetPlus."""

    vendor_key = DEFAULT_VENDOR_KEY
    capabilities = NETPLUS_CAPABILITIES

    def __init__(self, client: TelegramClient, bot_username: str) -> None:
        self.client = client
        self.bot_username = bot_username
        self.lock = asyncio.Lock()
        self._entity = None

    async def ensure_entity(self):
        from app.telegram.user_client import ensure_connected

        await ensure_connected(self.client)
        if self._entity is None:
            self._entity = await self.client.get_entity(self.bot_username)
        return self._entity

    async def _poll_newer_message(
        self, entity, after_id: int
    ) -> Message | None:
        msgs = await self.client.get_messages(entity, limit=8)
        for m in msgs:
            if m and m.id > after_id and not m.out:
                return m
        return None

    async def _message_changed(
        self,
        entity,
        msg_id: int,
        old_text: str,
        old_buttons: list[list[dict[str, str]]],
    ) -> Message | None:
        edited = await self.client.get_messages(entity, ids=msg_id)
        if not edited:
            return None
        if (edited.message or "") != old_text or buttons_of(edited) != old_buttons:
            return edited
        return None

    async def _refresh_msg(self, entity, msg: Message | None) -> Message | None:
        if not msg:
            return None
        return await self.client.get_messages(entity, ids=msg.id) or msg

    async def _await_stable(
        self, msg: Message | None, *, timeout: float = 2.0
    ) -> Message | None:
        """NetPlus often edits text first, then attaches buttons — brief settle.

        Uses a few spaced refreshes (not a tight poll loop) to avoid flood-waits.
        """
        if not msg:
            return None
        entity = await self.ensure_entity()
        cur = msg
        baseline_id = msg.id
        # First pause: give NetPlus time to attach inline buttons after text.
        await asyncio.sleep(min(0.35, timeout))
        newer = await self._poll_newer_message(entity, baseline_id)
        if newer:
            cur = newer
            baseline_id = newer.id
        else:
            cur = await self._refresh_msg(entity, cur) or cur
        first_text = cur.message or ""
        first_btns = buttons_of(cur)
        await asyncio.sleep(SETTLE)
        newer = await self._poll_newer_message(entity, baseline_id)
        if newer:
            return newer
        again = await self._refresh_msg(entity, cur) or cur
        if (again.message or "") != first_text or buttons_of(again) != first_btns:
            await asyncio.sleep(SETTLE)
            newer = await self._poll_newer_message(entity, again.id)
            if newer:
                return newer
            return await self._refresh_msg(entity, again) or again
        return again

    async def _wait_card_ready(
        self, msg: Message | None, *, timeout: float = 8.0
    ) -> Message | None:
        """Stay on wallet/pay-method screen until کارت به کارت exists (or payment)."""
        entity = await self.ensure_entity()
        deadline = time.monotonic() + timeout
        cur = msg
        while time.monotonic() < deadline:
            if cur and is_payment(cur):
                return cur
            if cur and find_card_button(cur):
                return await self._await_stable(cur, timeout=1.2)
            if cur:
                cur = await self._refresh_msg(entity, cur) or cur
                if is_payment(cur) or find_card_button(cur):
                    continue
            recent = await self.find_wallet_or_card_recent()
            if recent:
                cur = recent
                continue
            await asyncio.sleep(POLL)
        return cur

    async def wait_bot(self, after_id: int, timeout: float = 8.0) -> Message | None:
        """Wait for next inbound NetPlus message (events + aggressive poll).

        Telethon 1.45+ has no ``TelegramClient.wait_event``; use add_event_handler.
        """
        entity = await self.ensure_entity()
        fut: asyncio.Future = self.client.loop.create_future()

        async def on_new(event):
            msg = event.message
            if msg and msg.id > after_id and not event.out and not fut.done():
                fut.set_result(msg)

        self.client.add_event_handler(
            on_new, events.NewMessage(chats=entity, incoming=True)
        )
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if fut.done():
                    return await self._await_stable(fut.result(), timeout=1.0)
                try:
                    got = await asyncio.wait_for(
                        asyncio.shield(fut), timeout=POLL
                    )
                    return await self._await_stable(got, timeout=1.0)
                except asyncio.TimeoutError:
                    polled = await self._poll_newer_message(entity, after_id)
                    if polled:
                        return await self._await_stable(polled, timeout=1.0)
            if fut.done():
                return await self._await_stable(fut.result(), timeout=1.0)
            polled = await self._poll_newer_message(entity, after_id)
            return await self._await_stable(polled, timeout=1.0) if polled else None
        finally:
            self.client.remove_event_handler(on_new)

    async def wait_payment(
        self, timeout: float = 10.0, *, after_id: int | None = None
    ) -> Message | None:
        entity = await self.ensure_entity()
        deadline = time.monotonic() + timeout
        pay = await self.find_payment_recent(after_id=after_id)
        if pay:
            return await self._await_stable(pay, timeout=1.2)
        fut: asyncio.Future = self.client.loop.create_future()

        async def on_new(event):
            msg = event.message
            if not msg or event.out or not is_payment(msg) or fut.done():
                return
            if after_id is not None and msg.id <= after_id:
                return
            fut.set_result(msg)

        self.client.add_event_handler(
            on_new, events.NewMessage(chats=entity, incoming=True)
        )
        try:
            while time.monotonic() < deadline:
                if fut.done():
                    return await self._await_stable(fut.result(), timeout=1.2)
                pay = await self.find_payment_recent(after_id=after_id)
                if pay:
                    return await self._await_stable(pay, timeout=1.2)
                # Payment may arrive as an edit of the wallet/card message.
                recent = await self.find_wallet_or_card_recent(after_id=after_id)
                if recent and is_payment(recent):
                    return await self._await_stable(recent, timeout=1.2)
                try:
                    got = await asyncio.wait_for(
                        asyncio.shield(fut), timeout=POLL
                    )
                    return await self._await_stable(got, timeout=1.2)
                except asyncio.TimeoutError:
                    continue
            if fut.done():
                return await self._await_stable(fut.result(), timeout=1.2)
            return await self.find_payment_recent(after_id=after_id)
        finally:
            self.client.remove_event_handler(on_new)

    async def send_text(self, text: str) -> Message | None:
        entity = await self.ensure_entity()
        while True:
            try:
                before = (await self.client.get_messages(entity, limit=1))[0].id
                t0 = time.monotonic()
                await self.client.send_message(entity, text)
                msg = await self.wait_bot(before, timeout=8.0)
                log.info("STEP send %r -> %.2fs", text[:40], time.monotonic() - t0)
                return msg
            except FloodWaitError as exc:
                await asyncio.sleep(int(exc.seconds) + 1)

    async def _fire_button_click(self, btn) -> None:
        """Press callback button without blocking on bot callback-answer RPC.

        NetPlus updates the chat quickly but often never answers
        ``messages.getBotCallbackAnswer``. Telethon's ``btn.click()`` waits on
        that answer (~20–26s), which was our renew bottleneck — not NetPlus UI.
        """

        async def _click() -> None:
            try:
                await btn.click()
            except (asyncio.CancelledError, FloodWaitError):
                raise
            except Exception as exc:
                log.debug("callback answer ignored: %s", exc)

        task = asyncio.create_task(_click())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.35)
        except asyncio.TimeoutError:
            # Request already sent; leave task running so Telethon can finish RPC.
            log.debug("callback answer still pending — continuing on message poll")

    async def click_data(self, msg: Message | None, data: str) -> Message | None:
        entity = await self.ensure_entity()
        if not msg or not msg.buttons:
            return None
        for row in msg.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                if not raw or raw.decode("utf-8", errors="replace") != data:
                    continue
                while True:
                    try:
                        before = (await self.client.get_messages(entity, limit=1))[0].id
                        old_text = msg.message or ""
                        old_buttons = buttons_of(msg)
                        msg_id = msg.id
                        t0 = time.monotonic()

                        new_ev = self.client.loop.create_future()
                        edit_ev = self.client.loop.create_future()

                        async def on_new(event):
                            if event.message and event.message.id > before and not event.out:
                                if not new_ev.done():
                                    new_ev.set_result(event.message)

                        async def on_edit(event):
                            if not event.message or event.message.id != msg_id:
                                return
                            changed = (event.message.message or "") != old_text or (
                                buttons_of(event.message) != old_buttons
                            )
                            if changed and not edit_ev.done():
                                edit_ev.set_result(event.message)

                        self.client.add_event_handler(
                            on_new, events.NewMessage(chats=entity, incoming=True)
                        )
                        self.client.add_event_handler(
                            on_edit, events.MessageEdited(chats=entity, incoming=True)
                        )
                        try:
                            # Handlers first, then fire click without waiting for answer.
                            await self._fire_button_click(btn)

                            deadline = time.monotonic() + 12.0
                            result: Message | None = None
                            how = ""
                            while time.monotonic() < deadline:
                                if new_ev.done():
                                    result = new_ev.result()
                                    how = "new"
                                    break
                                if edit_ev.done():
                                    result = edit_ev.result()
                                    how = "edit"
                                    break
                                # Prefer events; poll only every POLL to avoid flood-wait.
                                await asyncio.sleep(POLL)
                                if new_ev.done():
                                    result = new_ev.result()
                                    how = "new"
                                    break
                                if edit_ev.done():
                                    result = edit_ev.result()
                                    how = "edit"
                                    break
                                edited = await self._message_changed(
                                    entity, msg_id, old_text, old_buttons
                                )
                                if edited:
                                    result = edited
                                    how = "edit poll"
                                    break
                                newer = await self._poll_newer_message(entity, before)
                                if newer:
                                    result = newer
                                    how = "new poll"
                                    break
                            if result is not None:
                                settled = await self._await_stable(result)
                                log.info(
                                    "STEP click %s (%s) -> %.2fs",
                                    data[:40],
                                    how,
                                    time.monotonic() - t0,
                                )
                                return settled or result
                        finally:
                            self.client.remove_event_handler(on_new)
                            self.client.remove_event_handler(on_edit)

                        # Last chance: screen may have changed without a clean event.
                        edited = await self._message_changed(
                            entity, msg_id, old_text, old_buttons
                        )
                        if edited:
                            settled = await self._await_stable(edited)
                            log.info(
                                "STEP click %s (late edit) -> %.2fs",
                                data[:40],
                                time.monotonic() - t0,
                            )
                            return settled or edited
                        newer = await self._poll_newer_message(entity, before)
                        if newer:
                            settled = await self._await_stable(newer)
                            log.info(
                                "STEP click %s (late new) -> %.2fs",
                                data[:40],
                                time.monotonic() - t0,
                            )
                            return settled or newer
                        log.info(
                            "STEP click %s (timeout) -> %.2fs",
                            data[:40],
                            time.monotonic() - t0,
                        )
                        return None
                    except FloodWaitError as exc:
                        await asyncio.sleep(int(exc.seconds) + 1)
                    except DataInvalidError:
                        return None
        return None

    async def find_payment_recent(self, *, after_id: int | None = None) -> Message | None:
        entity = await self.ensure_entity()
        recent = [m for m in await self.client.get_messages(entity, limit=8) if m and not m.out]
        for m in recent:
            if after_id is not None and m.id <= after_id:
                continue
            if is_payment(m):
                return m
        return None

    async def find_wallet_or_card_recent(
        self, *, after_id: int | None = None
    ) -> Message | None:
        entity = await self.ensure_entity()
        recent = [m for m in await self.client.get_messages(entity, limit=8) if m and not m.out]
        for m in recent:
            if after_id is not None and m.id <= after_id:
                continue
            if is_payment(m):
                return m
            if find_card_button(m) or is_wallet_insufficient(m.message):
                return m
        return None

    async def find_renew_or_create_success_recent(
        self, *, after_id: int | None = None, username: str | None = None
    ) -> Message | None:
        """Detect NetPlus operator-wallet settlement (renew/create success, no card UI)."""
        from app.vendors.netplus_parser import parse_netplus

        entity = await self.ensure_entity()
        recent = [
            m for m in await self.client.get_messages(entity, limit=12) if m and not m.out
        ]
        want = (username or "").strip().lower()
        for m in recent:
            if after_id is not None and m.id <= after_id:
                continue
            parsed = parse_netplus(m.message or "", buttons_of(m))
            if not parsed or parsed.kind not in ("renew_ok", "create_ok"):
                continue
            got = (parsed.username or "").strip().lower()
            if want and got and got != want:
                continue
            return m
        return None

    def _payment_from_msg(self, msg: Message) -> PaymentInfo:
        pay = extract_pay(msg.message or "")
        receipt = find_data(msg, "sendresidcart")
        return PaymentInfo(
            amount_rial=pay.get("amount_rial"),
            card=pay.get("card"),
            holder=pay.get("holder"),
            receipt_callback=receipt,
            message_id=msg.id,
            text=msg.message or "",
        )

    async def open_status(self, username: str, service_id: str | None) -> Message | None:
        """Mike-style fast path: /services → search → type username."""
        t0 = time.monotonic()
        timings: list[tuple[str, float]] = []

        def _mark(label: str, started: float) -> None:
            timings.append((label, time.monotonic() - started))

        s = time.monotonic()
        msg = await self.send_text("/services")
        _mark("send_/services", s)
        if msg and msg.message and "عبارت جستجو" in (msg.message or ""):
            # Already in search prompt — type username directly
            s = time.monotonic()
            msg = await self.send_text(username)
            _mark("type_username_direct", s)
        else:
            if msg and find_data(msg, "searchservice", exact=True):
                s = time.monotonic()
                msg = await self.click_data(msg, "searchservice")
                _mark("click_searchservice", s)
            s = time.monotonic()
            msg = await self.send_text(username)
            _mark("type_username", s)

        if not msg:
            log.info(
                "TIMING open_status %s FAIL total=%.2fs steps=%s",
                username,
                time.monotonic() - t0,
                timings,
            )
            return None

        # Prefer exact product match (by service_id / exact username) over a
        # status card that may belong to a similarly named sibling service.
        product_cb = find_product_callback_for_username(
            msg, username, service_id=service_id
        )
        if product_cb:
            s = time.monotonic()
            opened = await self.click_data(msg, product_cb)
            _mark("click_product_exact", s)
            log.info(
                "TIMING open_status %s via exact product total=%.2fs steps=%s",
                username,
                time.monotonic() - t0,
                timings,
            )
            return opened

        if find_data(msg, "extend_") and self._status_username_matches(msg, username):
            log.info(
                "TIMING open_status %s direct total=%.2fs steps=%s",
                username,
                time.monotonic() - t0,
                timings,
            )
            return msg

        log.info(
            "TIMING open_status %s end total=%.2fs steps=%s",
            username,
            time.monotonic() - t0,
            timings,
        )
        return None

    async def _click_card_to_payment(
        self, msg: Message | None, *, after_id: int | None = None
    ) -> DriveResult:
        """Click کارت به کارت then wait for rial/card instructions."""
        t0 = time.monotonic()
        ready = await self._wait_card_ready(msg, timeout=8.0)
        if ready and is_payment(ready):
            if after_id is None or ready.id > after_id:
                log.info("TIMING card_path already_payment %.2fs", time.monotonic() - t0)
                return DriveResult(True, "payment", payment=self._payment_from_msg(ready))
        card = find_card_button(ready) if ready else None
        msg = ready or msg
        if not card:
            log.info("TIMING card_path no_button %.2fs", time.monotonic() - t0)
            return DriveResult(False, "pay_method_stuck", error="no_card_button")
        before_card = getattr(msg, "id", None) or after_id
        log.info("Clicking card-to-card (%s)", card)
        s = time.monotonic()
        after_click = await self.click_data(msg, card)
        click_s = time.monotonic() - s
        if after_click and is_payment(after_click):
            log.info(
                "TIMING card_path click=%.2fs inline_payment total=%.2fs ok=True",
                click_s,
                time.monotonic() - t0,
            )
            return DriveResult(True, "payment", payment=self._payment_from_msg(after_click))
        s = time.monotonic()
        pay_msg = await self.wait_payment(timeout=15.0, after_id=before_card)
        wait_s = time.monotonic() - s
        log.info(
            "TIMING card_path click=%.2fs wait_payment=%.2fs total=%.2fs ok=%s",
            click_s,
            wait_s,
            time.monotonic() - t0,
            bool(pay_msg),
        )
        if pay_msg:
            return DriveResult(True, "payment", payment=self._payment_from_msg(pay_msg))
        return DriveResult(False, "no_payment_after_card", error="no_payment_after_card")

    def _bump_water(self, water: int, msg: Message | None) -> int:
        if msg is None:
            return water
        return max(water, int(msg.id))

    def _accept_payment_msg(
        self, msg: Message | None, *, water: int, allow: bool
    ) -> PaymentInfo | None:
        """Only accept a payment screen created after this renew's watermark."""
        if not allow or not msg or not is_payment(msg):
            return None
        if int(msg.id) <= water:
            log.info(
                "Ignoring stale NetPlus payment msg id=%s water=%s",
                msg.id,
                water,
            )
            return None
        return self._payment_from_msg(msg)

    async def _poll_post_confirm(
        self,
        *,
        water: int,
        timeout: float = 12.0,
        username: str | None = None,
    ) -> tuple[str, Message | None, PaymentInfo | None]:
        """Wait for wallet/card/payment/success after invoice confirm.

        NetPlus often lags 2–6s before the insufficient-wallet / card screen.
        """
        deadline = time.monotonic() + max(2.0, timeout)
        while time.monotonic() < deadline:
            pay_msg = await self.find_payment_recent(after_id=water)
            accepted = self._accept_payment_msg(pay_msg, water=water, allow=True)
            if accepted:
                return "payment", pay_msg, accepted
            success = await self.find_renew_or_create_success_recent(
                after_id=water, username=username
            )
            if success:
                return "vendor_wallet_settled", success, None
            wallet = await self.find_wallet_or_card_recent(after_id=water)
            if wallet:
                return "wallet", wallet, None
            await asyncio.sleep(max(POLL, 0.45))
        return "timeout", None, None

    async def walk_to_payment(
        self,
        msg: Message | None,
        *,
        after_id: int | None = None,
        username: str | None = None,
    ) -> DriveResult:
        """Walk invoice → wallet/card → payment for THIS renew only.

        Leftover card-to-card messages from a previous renew must never count.
        We keep a watermark and only accept payment after this walk confirms
        the invoice (and usually clicks کارت به کارت).
        """
        walk_t0 = time.monotonic()
        water = int(after_id or 0)
        # Invoice/plan screen itself raises the gate above any older payment msgs.
        water = self._bump_water(water, msg)
        confirmed = False
        want_user = (username or "").strip() or None
        log.info("TIMING walk start water=%s msg_id=%s", water, getattr(msg, "id", None))

        for i in range(16):
            step_t0 = time.monotonic()
            if not msg:
                if confirmed:
                    kind, found, pay = await self._poll_post_confirm(
                        water=water, timeout=12.0, username=want_user
                    )
                    if kind == "payment" and pay:
                        log.info(
                            "TIMING walk step=%s payment_after_confirm %.2fs",
                            i,
                            time.monotonic() - step_t0,
                        )
                        return DriveResult(True, "payment", payment=pay)
                    if kind == "vendor_wallet_settled":
                        log.info(
                            "TIMING walk step=%s vendor_wallet_settled %.2fs",
                            i,
                            time.monotonic() - step_t0,
                        )
                        return DriveResult(True, "vendor_wallet_settled")
                    if kind == "wallet" and found:
                        msg = found
                    else:
                        return DriveResult(False, f"empty_{i}", error="empty_message")
                else:
                    return DriveResult(False, f"empty_{i}", error="empty_before_confirm")

            text = msg.message or ""
            err = detect_vendor_error(text)
            if err:
                return DriveResult(False, "vendor_error", error=err)

            datas = [b.get("callback_data") or "" for b in flat_buttons(msg)]
            card_btn = find_card_button(msg)
            on_invoice = ("فاکتور" in text) or (CONFIRM in datas) or (
                "confirmandgetservice" in datas
            )

            # Never short-circuit on leftover payment while still on invoice /
            # before this renew has confirmed.
            if confirmed and not on_invoice:
                accepted = self._accept_payment_msg(
                    msg, water=water, allow=True
                )
                if accepted:
                    log.info(
                        "TIMING walk step=%s payment_msg %.2fs total=%.2fs",
                        i,
                        time.monotonic() - step_t0,
                        time.monotonic() - walk_t0,
                    )
                    return DriveResult(True, "payment", payment=accepted)
                pay_msg = await self.find_payment_recent(after_id=water)
                accepted = self._accept_payment_msg(
                    pay_msg, water=water, allow=True
                )
                if accepted:
                    log.info(
                        "TIMING walk step=%s payment_recent %.2fs total=%.2fs",
                        i,
                        time.monotonic() - step_t0,
                        time.monotonic() - walk_t0,
                    )
                    return DriveResult(True, "payment", payment=accepted)

            # Wallet / pay methods → click کارت به کارت for a fresh amount
            if card_btn or is_wallet_insufficient(text) or CARD in datas:
                if not confirmed:
                    # Reaching wallet implies invoice already accepted by NetPlus.
                    confirmed = True
                    water = self._bump_water(water, msg)
                log.info(
                    "TIMING walk step=%s card_branch water=%s", i, water
                )
                result = await self._click_card_to_payment(msg, after_id=water)
                if result.ok and result.payment:
                    if int(result.payment.message_id or 0) <= water:
                        log.warning(
                            "Rejecting stale payment id=%s water=%s — continue",
                            result.payment.message_id,
                            water,
                        )
                        water = self._bump_water(water, msg)
                        msg = None
                        continue
                    log.info(
                        "TIMING walk done step=%s total=%.2fs ok=True",
                        i,
                        time.monotonic() - walk_t0,
                    )
                    return result
                if result.ok or result.error != "no_card_button":
                    return result
                waited = await self._wait_card_ready(msg, timeout=3.0)
                if waited and find_card_button(waited):
                    msg = waited
                    continue
                return result

            if on_invoice:
                clicked = None
                for cand in (CONFIRM, "confirmandgetservice"):
                    if cand in datas:
                        clicked = cand
                        break
                if not clicked:
                    for b in flat_buttons(msg):
                        t = b.get("text") or ""
                        d = b.get("callback_data")
                        if (
                            d
                            and any(k in t for k in ("تایید", "پرداخت", "تمدید"))
                            and "بازگشت" not in t
                            and "کارت" not in t
                        ):
                            clicked = d
                            break
                if not clicked:
                    settled = await self._await_stable(msg, timeout=2.0)
                    if settled and (
                        find_data(settled, CONFIRM, exact=True)
                        or find_data(settled, "confirmandgetservice", exact=True)
                        or any(
                            any(k in (b.get("text") or "") for k in ("تایید", "پرداخت", "تمدید"))
                            for b in flat_buttons(settled)
                        )
                    ):
                        msg = settled
                        continue
                    return DriveResult(False, "invoice_stuck", error="no_confirm_button")

                # Gate above this invoice before confirm — old card msgs stay below.
                water = self._bump_water(water, msg)
                gate_before_confirm = water
                s = time.monotonic()
                msg = await self.click_data(msg, clicked)
                confirmed = True
                log.info(
                    "TIMING walk step=%s click_confirm=%s %.2fs water=%s",
                    i,
                    clicked,
                    time.monotonic() - s,
                    gate_before_confirm,
                )
                if msg and is_payment(msg) and int(msg.id) > gate_before_confirm:
                    return DriveResult(
                        True, "payment", payment=self._payment_from_msg(msg)
                    )
                if msg and (
                    is_wallet_insufficient(msg.message) or find_card_button(msg)
                ):
                    water = self._bump_water(gate_before_confirm, msg)
                    continue
                water = gate_before_confirm
                # NetPlus often needs a few seconds before wallet/card UI appears.
                kind, found, pay = await self._poll_post_confirm(
                    water=water, timeout=12.0, username=want_user
                )
                if kind == "payment" and pay:
                    log.info(
                        "TIMING walk step=%s payment_after_confirm_poll %.2fs",
                        i,
                        time.monotonic() - step_t0,
                    )
                    return DriveResult(True, "payment", payment=pay)
                if kind == "vendor_wallet_settled":
                    log.info(
                        "TIMING walk step=%s vendor_wallet_settled_after_confirm",
                        i,
                    )
                    return DriveResult(True, "vendor_wallet_settled")
                if kind == "wallet" and found:
                    msg = found
                    continue
                msg = None
                continue

            if CONFIRM in datas:
                water = self._bump_water(water, msg)
                msg = await self.click_data(msg, CONFIRM)
                confirmed = True
                water = self._bump_water(water, msg)
                continue

            # After confirm, chase wallet/card screens only (not stale payments).
            if confirmed:
                wallet = await self.find_wallet_or_card_recent(after_id=water)
                if wallet and wallet.id != getattr(msg, "id", None):
                    msg = wallet
                    continue

            return DriveResult(
                False, f"unhandled_{i}", error=f"unhandled:{(text or '')[:80]}"
            )

        return DriveResult(False, "too_many_steps", error="walk_limit")

    @staticmethod
    def _pair_month_aliases(callback: str | None, *, prefix: str) -> list[str]:
        """NetPlus flips between N and N-1 day codes (31/30, 61/60, 91/90)."""
        if not callback:
            return [
                f"{prefix}31",
                f"{prefix}30",
                f"{prefix}61",
                f"{prefix}60",
                f"{prefix}91",
                f"{prefix}90",
            ]
        cb = str(callback).strip()
        # Map extend ↔ buy prefixes when admin/panel reused the wrong family.
        other = (
            "productmonth_"
            if prefix.startswith("productextendmonths_")
            else "productextendmonths_"
        )
        if cb.startswith("productextendmonths_"):
            stem = cb[len("productextendmonths_") :]
        elif cb.startswith("productmonth_"):
            stem = cb[len("productmonth_") :]
        else:
            return [cb]
        pairs = {
            "31": ("31", "30"),
            "30": ("30", "31"),
            "61": ("61", "60"),
            "60": ("60", "61"),
            "91": ("91", "90"),
            "90": ("90", "91"),
        }
        stems = pairs.get(stem, (stem,))
        out: list[str] = []
        for s in stems:
            out.append(f"{prefix}{s}")
            out.append(f"{other}{s}")
        # de-dupe preserve order
        seen: set[str] = set()
        uniq: list[str] = []
        for item in out:
            if item not in seen:
                seen.add(item)
                uniq.append(item)
        return uniq

    @classmethod
    def _duration_aliases(cls, callback: str | None) -> list[str]:
        """Renew/extend month callbacks (productextendmonths_*)."""
        return cls._pair_month_aliases(callback, prefix="productextendmonths_")

    @classmethod
    def _buy_duration_aliases(cls, callback: str | None) -> list[str]:
        """Create/buy month callbacks (productmonth_*)."""
        return cls._pair_month_aliases(callback, prefix="productmonth_")

    def _resolve_month_duration(
        self,
        menu,
        duration_callback: str | None,
        *,
        prefix: str,
    ) -> str | None:
        aliases = (
            self._duration_aliases(duration_callback)
            if prefix.startswith("productextendmonths_")
            else self._buy_duration_aliases(duration_callback)
        )
        for cand in aliases:
            if find_data(menu, cand, exact=True):
                return cand
        want = duration_callback or ""
        prefer_one = "31" in want or "30" in want or not want
        prefer_two = "61" in want or "60" in want
        prefer_three = "91" in want or "90" in want
        for b in flat_buttons(menu):
            d = b.get("callback_data") or ""
            t = b.get("text") or ""
            if not d.startswith(prefix):
                continue
            if prefer_one and (
                "یک ماه" in t or d.endswith("_31") or d.endswith("_30")
            ):
                return d
            if prefer_two and (
                "دو ماه" in t or d.endswith("_61") or d.endswith("_60")
            ):
                return d
            if prefer_three and (
                "سه ماه" in t or d.endswith("_91") or d.endswith("_90")
            ):
                return d
        for b in flat_buttons(menu):
            d = b.get("callback_data") or ""
            if d.startswith(prefix):
                return d
        return None

    def _resolve_extend_duration(
        self, menu, duration_callback: str | None
    ) -> str | None:
        return self._resolve_month_duration(
            menu, duration_callback, prefix="productextendmonths_"
        )

    def _resolve_buy_duration(
        self, menu, duration_callback: str | None
    ) -> str | None:
        return self._resolve_month_duration(
            menu, duration_callback, prefix="productmonth_"
        )

    async def drive_renew(
        self,
        *,
        username: str,
        service_id: str | None,
        plan_mode: str,
        duration_callback: str | None = None,
        package_callback: str | None = None,
    ) -> DriveResult:
        async with self.lock:
            drive_t0 = time.monotonic()
            timings: list[tuple[str, float]] = []

            def _mark(label: str, started: float) -> None:
                timings.append((label, time.monotonic() - started))

            try:
                entity = await self.ensure_entity()
                latest = (await self.client.get_messages(entity, limit=1))[0]
                after_id = int(latest.id) if latest else 0

                s = time.monotonic()
                status = await self.open_status(username, service_id)
                _mark("open_status", s)
                if not status or not find_data(status, "extend_"):
                    log.info(
                        "TIMING drive_renew %s FAIL_NO_EXTEND total=%.2fs steps=%s",
                        username,
                        time.monotonic() - drive_t0,
                        timings,
                    )
                    return DriveResult(False, "no_extend", error="FAIL_NO_EXTEND")

                extend = (
                    f"extend_{service_id}"
                    if service_id and find_data(status, f"extend_{service_id}", exact=True)
                    else find_data(status, "extend_")
                )
                s = time.monotonic()
                menu = await self.click_data(status, extend)  # type: ignore[arg-type]
                _mark("click_extend", s)
                if not menu:
                    log.info(
                        "TIMING drive_renew %s FAIL_EXTEND total=%.2fs steps=%s",
                        username,
                        time.monotonic() - drive_t0,
                        timings,
                    )
                    return DriveResult(False, "extend_failed", error="FAIL_EXTEND_CLICK")

                if plan_mode == "current":
                    same = find_data(menu, CURRENT_PLAN, exact=True)
                    if not same:
                        for b in flat_buttons(menu):
                            if "تمدید پلن فعلی" in (b.get("text") or "") and b.get("callback_data"):
                                same = b["callback_data"]
                                break
                    if not same:
                        return DriveResult(False, "no_current_plan", error="FAIL_NO_CURRENT_PLAN")
                    s = time.monotonic()
                    after = await self.click_data(menu, same)
                    _mark("click_current_plan", s)
                    # Raise gate to this invoice so a previous card-to-card msg cannot win.
                    gate = max(after_id, int(after.id) if after else 0)
                    s = time.monotonic()
                    result = await self.walk_to_payment(
                        after, after_id=gate, username=username
                    )
                    _mark("walk_to_payment", s)
                    log.info(
                        "TIMING drive_renew %s total=%.2fs ok=%s stage=%s steps=%s",
                        username,
                        time.monotonic() - drive_t0,
                        result.ok,
                        result.stage,
                        timings,
                    )
                    return result

                # change plan — NetPlus alternates 31/61/91 vs 30/60/90 callbacks
                dur = self._resolve_extend_duration(menu, duration_callback)
                if not dur:
                    # Some accounts skip duration and show packages (+ current) immediately.
                    packages_direct = package_buttons(menu)
                    if package_callback and find_data(menu, package_callback, exact=True):
                        after_pkg = await self.click_data(menu, package_callback)
                        gate = max(after_id, int(after_pkg.id) if after_pkg else 0)
                        return await self.walk_to_payment(
                            after_pkg, after_id=gate, username=username
                        )
                    if packages_direct and not package_callback:
                        return DriveResult(True, "need_package", packages=packages_direct)
                    # Last resort: current plan button (same traffic top-up for many renews)
                    same = find_data(menu, CURRENT_PLAN, exact=True)
                    if not same:
                        for b in flat_buttons(menu):
                            if "تمدید پلن فعلی" in (b.get("text") or "") and b.get(
                                "callback_data"
                            ):
                                same = b["callback_data"]
                                break
                    if same:
                        log.warning(
                            "drive_renew %s: no duration buttons — falling back to current plan",
                            username,
                        )
                        after = await self.click_data(menu, same)
                        gate = max(after_id, int(after.id) if after else 0)
                        return await self.walk_to_payment(
                            after, after_id=gate, username=username
                        )
                    log.info(
                        "drive_renew %s FAIL_NO_DURATION menu_buttons=%s",
                        username,
                        [b.get("callback_data") for b in flat_buttons(menu)],
                    )
                    return DriveResult(False, "no_duration", error="FAIL_NO_DURATION")

                after_dur = await self.click_data(menu, dur)
                packages = package_buttons(after_dur)
                if not package_callback:
                    if not packages:
                        return DriveResult(False, "no_packages", error="FAIL_NO_PACKAGES")
                    return DriveResult(True, "need_package", packages=packages)

                if not find_data(after_dur, package_callback, exact=True):
                    # package screen may still be current
                    pkgs = package_buttons(after_dur) or packages
                    return DriveResult(
                        False,
                        "package_missing",
                        packages=pkgs,
                        error="FAIL_PACKAGE_NOT_ON_SCREEN",
                    )

                after_pkg = await self.click_data(after_dur, package_callback)
                gate = max(after_id, int(after_pkg.id) if after_pkg else 0)
                result = await self.walk_to_payment(
                    after_pkg, after_id=gate, username=username
                )
                log.info(
                    "DRIVE renew %s total %.2fs ok=%s stage=%s",
                    username,
                    time.monotonic() - drive_t0,
                    result.ok,
                    result.stage,
                )
                return result
            except Exception as exc:
                log.exception("drive_renew failed for %s", username)
                return DriveResult(False, "exception", error=str(exc))

    async def drive_create(
        self,
        *,
        username: str,
        duration_callback: str | None,
        package_callback: str | None,
    ) -> DriveResult:
        async with self.lock:
            try:
                entity = await self.ensure_entity()
                latest = (await self.client.get_messages(entity, limit=1))[0]
                after_id = int(latest.id) if latest else 0

                msg = await self.send_text("🔐 خرید اشتراک")
                if not msg:
                    msg = await self.send_text("/buy")
                if not msg:
                    return DriveResult(False, "no_buy", error="FAIL_BUY_OPEN")
                # NetPlus often posts text first, then attaches month buttons.
                msg = await self._await_stable(msg, timeout=2.5) or msg

                dur = self._resolve_buy_duration(msg, duration_callback)
                if not dur:
                    # Log which month buttons NetPlus actually showed (helps future drift).
                    shown = [
                        (b.get("text") or "", b.get("callback_data") or "")
                        for b in flat_buttons(msg)
                        if (b.get("callback_data") or "").startswith("productmonth_")
                        or (b.get("callback_data") or "").startswith("productextendmonths_")
                    ]
                    log.warning(
                        "drive_create %s: no buy duration match want=%r shown=%s",
                        username,
                        duration_callback,
                        shown,
                    )
                    return DriveResult(False, "no_duration", error="FAIL_NO_BUY_DURATION")

                after_dur = await self.click_data(msg, dur)
                packages = package_buttons(after_dur, create=True)
                if not package_callback:
                    if not packages:
                        return DriveResult(False, "no_packages", error="FAIL_NO_BUY_PACKAGES")
                    return DriveResult(True, "need_package", packages=packages)

                if not find_data(after_dur, package_callback, exact=True):
                    return DriveResult(
                        False,
                        "package_missing",
                        packages=packages,
                        error="FAIL_PACKAGE_NOT_ON_SCREEN",
                    )

                after_pkg = await self.click_data(after_dur, package_callback)
                # NetPlus asks for username
                before = (await self.client.get_messages(entity, limit=1))[0].id
                await self.client.send_message(entity, username)
                after_user = await self.wait_bot(before)
                if after_user and detect_vendor_error(after_user.message):
                    return DriveResult(
                        False, "vendor_error", error=detect_vendor_error(after_user.message)
                    )
                gate = max(after_id, int(after_user.id) if after_user else 0)
                return await self.walk_to_payment(
                    after_user, after_id=gate, username=username
                )
            except Exception as exc:
                log.exception("drive_create failed for %s", username)
                return DriveResult(False, "exception", error=str(exc))

    async def drive_wallet_topup(self, amount_toman: int) -> DriveResult:
        """Open NetPlus wallet → افزایش موجودی → enter toman → return card/amount.

        Does **not** click «پرداخت کردم»; caller shows card to the customer.
        """
        amount = max(0, int(amount_toman or 0))
        if amount < 10000:
            return DriveResult(
                False,
                "amount_too_low",
                error="مبلغ افزایش موجودی NetPlus حداقل ۱۰٬۰۰۰ تومان است.",
            )
        async with self.lock:
            try:
                entity = await self.ensure_entity()
                latest = (await self.client.get_messages(entity, limit=1))[0]
                water = int(latest.id) if latest else 0

                await self.send_text("/start")
                wallet = None
                for label in (
                    "🏦 کیف پول + شارژ",
                    "کیف پول + شارژ",
                    "💰 کیف پول",
                    "کیف پول",
                ):
                    wallet = await self.send_text(label)
                    if wallet and (
                        find_data(wallet, "Add_Balance", exact=True)
                        or "افزایش موجودی" in (wallet.message or "")
                        or find_card_button(wallet)
                        or is_payment(wallet)
                    ):
                        break
                if not wallet:
                    return DriveResult(False, "no_wallet", error="FAIL_WALLET_OPEN")
                wallet = await self._await_stable(wallet, timeout=2.0) or wallet

                if is_payment(wallet):
                    return DriveResult(
                        True, "payment", payment=self._payment_from_msg(wallet)
                    )

                add = wallet
                if find_data(wallet, "Add_Balance", exact=True):
                    add = await self.click_data(wallet, "Add_Balance") or wallet
                else:
                    # Fallback: any button mentioning top-up
                    clicked = None
                    for b in flat_buttons(wallet):
                        t = b.get("text") or ""
                        d = b.get("callback_data") or ""
                        if "افزایش" in t or "Add_Balance" in d or "add_balance" in d.lower():
                            clicked = d
                            break
                    if clicked:
                        add = await self.click_data(wallet, clicked) or wallet
                add = await self._await_stable(add, timeout=2.0) or add

                before = (await self.client.get_messages(entity, limit=1))[0].id
                await self.client.send_message(entity, str(amount))
                after_amt = await self.wait_bot(before, timeout=18.0)
                after_amt = await self._await_stable(after_amt, timeout=2.5) or after_amt
                if after_amt and detect_vendor_error(after_amt.message):
                    return DriveResult(
                        False,
                        "vendor_error",
                        error=detect_vendor_error(after_amt.message),
                    )

                gate = max(water, int(after_amt.id) if after_amt else 0)
                # Some flows land straight on payment; others need کارت به کارت.
                if after_amt and is_payment(after_amt):
                    return DriveResult(
                        True, "payment", payment=self._payment_from_msg(after_amt)
                    )
                if after_amt and find_card_button(after_amt):
                    return await self._click_card_to_payment(after_amt, after_id=gate)
                # Poll a bit for payment / methods screen
                kind, found, pay = await self._poll_post_confirm(
                    water=gate, timeout=12.0, username=None
                )
                if kind == "payment" and pay:
                    return DriveResult(True, "payment", payment=pay)
                if found and is_payment(found):
                    return DriveResult(
                        True, "payment", payment=self._payment_from_msg(found)
                    )
                if found and find_card_button(found):
                    return await self._click_card_to_payment(found, after_id=gate)
                return DriveResult(
                    False,
                    "no_payment_after_topup",
                    error="بعد از وارد کردن مبلغ، اطلاعات کارت NetPlus نیامد.",
                )
            except Exception as exc:
                log.exception("drive_wallet_topup failed amount=%s", amount)
                return DriveResult(False, "exception", error=str(exc))

    async def click_receipt(self, receipt_callback: str, payment_message_id: int | None) -> bool:
        async with self.lock:
            entity = await self.ensure_entity()
            msg = None
            if payment_message_id:
                msg = await self.client.get_messages(entity, ids=payment_message_id)
            if not msg or not find_data(msg, receipt_callback, exact=True):
                recent = [
                    m for m in await self.client.get_messages(entity, limit=8) if m and not m.out
                ]
                msg = next(
                    (m for m in recent if find_data(m, receipt_callback, exact=True)),
                    None,
                )
            if not msg:
                return False
            await self.click_data(msg, receipt_callback)
            return True

    async def send_receipt_photo(self, file_bytes: bytes, filename: str = "receipt.jpg") -> bool:
        async with self.lock:
            entity = await self.ensure_entity()
            from io import BytesIO

            bio = BytesIO(file_bytes)
            bio.name = filename
            await self.client.send_file(entity, bio)
            return True

    async def click_back_to_list(self) -> None:
        # After renew/create success leave the vendor success message as last state.
        log.info("NetPlus click_back_to_list skipped (keep success message)")
        return

    @staticmethod
    def _status_username_matches(msg: Message | None, username: str) -> bool:
        if not msg or not msg.message:
            return False
        from app.vendors.netplus_parser import parse_netplus

        parsed = parse_netplus(msg.message or "", buttons_of(msg))
        got = (parsed.username or "").strip().lower()
        want = (username or "").strip().lower()
        return bool(got) and got == want

    async def refresh_subscription_status(
        self, username: str, service_id: str | None
    ) -> dict[str, Any] | None:
        async with self.lock:
            status = await self.open_status(username, service_id)
            if not status:
                return None
            from app.vendors.netplus_parser import parse_netplus

            parsed = parse_netplus(status.message or "", buttons_of(status))
            data = parsed.as_dict()
            got = (data.get("username") or "").strip().lower()
            want = (username or "").strip().lower()
            if got and got != want:
                log.warning(
                    "NetPlus refresh refused: opened %r while requesting %r",
                    data.get("username"),
                    username,
                )
                return None
            if not data.get("username"):
                data["username"] = username
            if service_id and not data.get("vendor_service_id"):
                data["vendor_service_id"] = service_id
            # Keep raw status text for customer formatting (caller strips branding)
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
        """Click «لینک اشتراک» and return the https URL from the reply."""
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
        """One lock: open status, parse fields, then click subscription URL if present."""
        async with self.lock:
            status = await self.open_status(username, service_id)
            if not status:
                return None, None
            from app.vendors.netplus_parser import parse_netplus

            parsed = parse_netplus(status.message or "", buttons_of(status))
            data = parsed.as_dict()
            got = (data.get("username") or "").strip().lower()
            want = (username or "").strip().lower()
            if got and got != want:
                log.warning(
                    "NetPlus status+url refused: opened %r while requesting %r",
                    data.get("username"),
                    username,
                )
                return None, None
            if not data.get("username"):
                data["username"] = username
            if service_id and not data.get("vendor_service_id"):
                data["vendor_service_id"] = service_id
            data["status_text"] = status.message or ""
            data["last_refresh_method"] = "provider_bot"

            url: str | None = None
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
        self, username: str, service_id: str | None
    ) -> str | None:
        """Click «تغییر لینک» and return the new subscription URL if present."""
        async with self.lock:
            after = await self._click_service_action(username, service_id, "changelink_")
            if not after:
                return None
            text = after.message or ""
            m = re.search(r"https?://\S+", text)
            if m:
                return m.group(0).rstrip(").,]")
            # Some panels confirm first then need subscriptionurl again
            url_msg = await self._click_service_action(
                username, service_id, "subscriptionurl_"
            )
            if url_msg and url_msg.message:
                m2 = re.search(r"https?://\S+", url_msg.message)
                return m2.group(0).rstrip(").,]") if m2 else None
            return None

    def _list_products(self, msg: Message | None) -> list[dict[str, str]]:
        out = []
        for b in flat_buttons(msg):
            d = b.get("callback_data") or ""
            if not d.startswith("product_"):
                continue
            username = (b.get("text") or "").strip("✨ ").strip()
            # Skip navigation / non-username labels that sometimes reuse product_ ids.
            if not username or "بازگشت" in username or username.startswith("🏠"):
                continue
            if " " in username and not any(ch.isalnum() for ch in username):
                continue
            out.append(
                {
                    "username": username,
                    "id": d.split("_", 1)[1],
                    "callback_data": d,
                }
            )
        return out

    def _find_next_page(self, msg: Message | None) -> str | None:
        for b in flat_buttons(msg):
            t = b.get("text") or ""
            d = b.get("callback_data") or ""
            if "بعدی" in t and d:
                return d
        return None

    def _is_services_list(self, msg: Message | None) -> bool:
        if not msg:
            return False
        if self._list_products(msg):
            return True
        text = msg.message or ""
        return "اشتراک های خریداری شده" in text or "سرویس های خریداری شده" in text

    def _is_search_prompt(self, msg: Message | None) -> bool:
        if not msg:
            return False
        text = msg.message or ""
        return "عبارت جستجو" in text

    async def _escape_search_to_list(self, msg: Message | None) -> Message | None:
        """Leave quick-search mode and open the purchased-services list."""
        # Prefer explicit list menu labels used by the operator / probes
        for label in (
            "🛍 مشاهده سرویس های خریداری شده من",
            "مشاهده سرویس های خریداری شده من",
            "سرویس های من",
        ):
            nxt = await self.send_text(label)
            if nxt and self._is_services_list(nxt):
                return nxt
            if nxt and not self._is_search_prompt(nxt) and self._list_products(nxt):
                return nxt
        # Cancel / back buttons on the search screen if present
        if msg and msg.buttons:
            for b in flat_buttons(msg):
                t = b.get("text") or ""
                d = b.get("callback_data") or ""
                if d and any(x in t for x in ("انصراف", "لغو", "بازگشت", "منو")):
                    clicked = await self.click_data(msg, d)
                    if clicked and self._is_services_list(clicked):
                        return clicked
        return None

    async def _open_services_list(self, *, force_reopen: bool = False) -> Message | None:
        """Open purchased-services list (not quick-search).

        Root cause of sync failures: `/services` often opens search («عبارت جستجو»)
        instead of the paginated product list. Probes recover by sending the
        «مشاهده سرویس های خریداری شده من» reply-keyboard label.

        force_reopen=True skips reusing the latest chat message (it may be a
        middle/last inventory page and would under-count).
        """
        entity = await self.ensure_entity()
        if not force_reopen:
            latest = (await self.client.get_messages(entity, limit=1))[0]
            if latest and not latest.out and self._is_services_list(latest):
                return latest

        msg = await self.send_text("/services")
        if msg and self._is_services_list(msg):
            return msg
        if msg and self._is_search_prompt(msg):
            recovered = await self._escape_search_to_list(msg)
            if recovered:
                return recovered
        # Soft reset then list label
        await self.send_text("/start")
        for label in (
            "🛍 مشاهده سرویس های خریداری شده من",
            "مشاهده سرویس های خریداری شده من",
            "سرویس های من",
        ):
            msg = await self.send_text(label)
            if msg and self._is_services_list(msg):
                return msg
        return msg

    async def sync_from_recent_history(
        self, *, limit: int = 300
    ) -> list[dict[str, str]]:
        """Collect product_ buttons from recent NetPlus chat history (no UI walk)."""
        entity = await self.ensure_entity()
        msgs = await self.client.get_messages(entity, limit=limit)
        seen: dict[str, dict[str, str]] = {}
        for m in msgs:
            if not m or m.out:
                continue
            for p in self._list_products(m):
                sid = p.get("id") or ""
                if sid and sid not in seen:
                    seen[sid] = p
        return list(seen.values())

    async def sync_service_inventory(self, max_pages: int = 50) -> list[dict[str, str]]:
        """Walk NetPlus purchased-services pages. Never use chat history for inventory."""
        async with self.lock:
            t0 = time.monotonic()
            msg = await self._open_services_list(force_reopen=True)
            products0 = self._list_products(msg) if msg else []
            if not products0:
                preview = ((msg.message if msg else "") or "")[:160].replace("\n", " | ")
                raise RuntimeError(
                    "همگام‌سازی ناموفق: لیست سرویس فروشنده باز نشد "
                    f"(تاریخچه چت استفاده نمی‌شود). Preview: {preview or '—'}"
                )

            seen: set[str] = set()
            collected: list[dict[str, str]] = []
            for page in range(max_pages):
                if not msg:
                    break
                products = self._list_products(msg)
                new_on_page = 0
                for p in products:
                    if not p.get("id") or p["id"] in seen:
                        continue
                    seen.add(p["id"])
                    p = dict(p)
                    p["source"] = "live"
                    collected.append(p)
                    new_on_page += 1
                log.info(
                    "SYNC page=%s products=%s new=%s total=%s",
                    page + 1,
                    len(products),
                    new_on_page,
                    len(collected),
                )
                nxt = self._find_next_page(msg)
                if not nxt:
                    break
                before_ids = {p["id"] for p in products}
                msg = await self.click_data(msg, nxt)
                msg = await self._wait_list_products(msg, expect_change_from=before_ids)
                if msg and {p["id"] for p in self._list_products(msg)} == before_ids:
                    log.info("SYNC pagination repeated page — stop")
                    break
            log.info(
                "SYNC inventory done count=%s in %.1fs",
                len(collected),
                time.monotonic() - t0,
            )
            return collected

    async def _wait_list_products(
        self,
        msg: Message | None,
        *,
        expect_change_from: set[str] | None = None,
        attempts: int = 8,
        delay: float = 0.8,
    ) -> Message | None:
        """Poll message until product buttons appear (pagination can lag)."""
        if not msg:
            return None
        entity = await self.ensure_entity()
        cur = msg
        for i in range(attempts):
            products = self._list_products(cur)
            ids = {p["id"] for p in products if p.get("id")}
            if products and (
                expect_change_from is None or ids != expect_change_from
            ):
                return cur
            await asyncio.sleep(delay)
            refreshed = await self.client.get_messages(entity, ids=cur.id)
            if refreshed:
                cur = refreshed
            else:
                # Pagination sometimes posts a new message instead of editing.
                latest = (await self.client.get_messages(entity, limit=3))[0]
                if latest and not latest.out and self._list_products(latest):
                    cur = latest
            log.info(
                "SYNC wait products attempt=%s count=%s",
                i + 1,
                len(self._list_products(cur)),
            )
        return cur
