"""Customer payment timing: Variz verify poll + unique-amount + receipt fallback."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.database.database import get_session
from app.database import repositories as repo
from app.services.aban import (
    AbanError,
    aban_enabled,
    get_invoice,
    verify_invoice,
)
from app.services.variz import VarizError, variz_enabled, verify_payment
from app.telegram import customer_bridge

log = logging.getLogger(__name__)

NotifyAdmin = Callable[[str, InlineKeyboardMarkup | None], Awaitable[None]]


def _receipt_kb(
    order_id: int,
    *,
    amount_rial: str = "",
    card: str = "",
    checkout_url: str | None = None,
) -> InlineKeyboardMarkup:
    from aiogram.types import CopyTextButton

    rows: list[list[InlineKeyboardButton]] = []
    if checkout_url:
        rows.append([InlineKeyboardButton(text="🔗 صفحه پرداخت", url=checkout_url)])
    copy_row: list[InlineKeyboardButton] = []
    if card:
        copy_row.append(
            InlineKeyboardButton(
                text="💳 کپی کارت",
                copy_text=CopyTextButton(text=card),
            )
        )
    if amount_rial:
        copy_row.append(
            InlineKeyboardButton(
                text="📋 کپی مبلغ",
                copy_text=CopyTextButton(text=amount_rial),
            )
        )
    if copy_row:
        rows.append(copy_row)
    rows.append(
        [
            InlineKeyboardButton(
                text="📤 ارسال رسید پرداخت",
                callback_data=f"cust:receipt:{order_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="❌ لغو سفارش",
                callback_data=f"cust:cancel:{order_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


class CustomerPaymentWatcher:
    def __init__(self, settings: Settings, notify_admin: NotifyAdmin) -> None:
        self.settings = settings
        self.notify_admin = notify_admin
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop(), name="customer-pay-watch")
            with get_session() as session:
                delays = repo.get_reminder_delays_seconds(session)
                ttl = repo.get_order_ttl_seconds(session)
            log.info(
                "Customer payment watcher started (reminders=%s, ttl=%ss, provider=%s, aban=%s)",
                delays,
                ttl,
                self.settings.payment_provider,
                aban_enabled(self.settings),
            )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception:
                log.exception("customer payment watcher tick failed")
            await asyncio.sleep(15)

    async def _tick(self) -> None:
        await self._poll_aban()
        if (self.settings.payment_provider or "").lower() == "variz":
            await self._poll_variz()
        try:
            from app.services.vendor_direct_pay import maybe_forward_stale_receipts

            await maybe_forward_stale_receipts()
        except Exception:
            log.exception("vendor_direct receipt forward tick failed")
        await self._send_reminders()
        await self._expire_orders()

    async def _send_reminders(self) -> None:
        with get_session() as session:
            due = repo.list_customer_orders_due_reminder(session)
            nudged: list[tuple[int, int, str, InlineKeyboardMarkup]] = []
            now = datetime.utcnow()
            local_card, _ = repo.get_payment_card(session)
            for order in due:
                order.status = "receipt_window"
                order.receipt_reminded_at = now
                order.last_reminder_at = now
                order.reminder_count = int(order.reminder_count or 0) + 1
                repo.touch_customer_order(order)
                deadline = (
                    order.expires_at.strftime("%Y-%m-%d %H:%M")
                    if order.expires_at
                    else "—"
                )
                text = repo.get_bot_message(
                    session,
                    "payment_reminder",
                    order=order.id,
                    amount=order.amount_rial or "—",
                    deadline=deadline,
                )
                kb = _receipt_kb(
                    order.id,
                    amount_rial=order.amount_rial or "",
                    card=order.pay_card_number or local_card or "",
                    checkout_url=order.checkout_url,
                )
                nudged.append((order.id, order.telegram_id, text, kb))

        bot = customer_bridge.customer_bot
        if not bot:
            return
        for oid, tg, text, kb in nudged:
            try:
                await bot.send_message(tg, text, reply_markup=kb)
            except Exception:
                log.exception("receipt reminder failed for order %s", oid)

    async def _expire_orders(self) -> None:
        with get_session() as session:
            rows = repo.list_customer_orders_to_expire(session)
            expired: list[tuple[int, int, str]] = []
            for order in rows:
                # Never expire paid / in-progress fulfillment paths
                if order.status not in ("awaiting_payment", "receipt_window"):
                    continue
                if order.paid_at or order.status in (
                    "awaiting_review",
                    "approved",
                    "fulfilling",
                    "done",
                ):
                    continue
                repo.refund_wallet_applied(session, order)
                order.status = "expired"
                order.note = (order.note or "") + "|expired"
                repo.touch_customer_order(order)
                if order.kind == "create" and order.service_username:
                    repo.unlink_customer_service(
                        session, order.telegram_id, order.service_username
                    )
                msg = repo.get_bot_message(session, "order_expired", order=order.id)
                expired.append((order.id, order.telegram_id, msg))

        bot = customer_bridge.customer_bot
        if not bot:
            return
        for oid, tg, msg in expired:
            try:
                await bot.send_message(tg, msg)
            except Exception:
                log.exception("expire notify failed for order %s", oid)

    async def _poll_aban(self) -> None:
        if (self.settings.payment_provider or "").lower() != "aban":
            return
        if not aban_enabled(self.settings):
            return
        with get_session() as session:
            pending = repo.list_customer_orders_pending_gateway(session)
            items = [
                (o.id, o.gateway_invoice_id, o.amount_rial or "")
                for o in pending
                if o.gateway_invoice_id
            ]
        for oid, invoice_id, amount in items:
            try:
                inv = await get_invoice(self.settings, invoice_id)
            except AbanError as exc:
                log.debug("Aban get order=%s: %s", oid, exc)
                continue
            except Exception:
                log.exception("Aban get failed order=%s", oid)
                continue

            if inv.status == "paid":
                try:
                    verified = await verify_invoice(self.settings, invoice_id)
                except AbanError as exc:
                    # not_yet_paid shouldn't happen if status paid; skip retry next tick
                    log.warning("Aban verify order=%s: %s", oid, exc)
                    continue
                if verified.verified or verified.status == "paid":
                    pay_amount = str(amount or inv.payable_rial or inv.amount_rial)
                    result = await auto_confirm_by_amount(
                        pay_amount, source="aban_poll"
                    )
                    if not result.get("ok"):
                        await _confirm_order_id(
                            oid, source="aban_poll", amount=pay_amount
                        )
                    log.info(
                        "Aban confirmed order=%s invoice=%s amount=%s",
                        oid,
                        invoice_id,
                        pay_amount,
                    )
            elif inv.status in ("expired", "cancelled"):
                tg_notify: int | None = None
                with get_session() as session:
                    order = repo.get_customer_order(session, oid)
                    if order and order.status in (
                        "awaiting_payment",
                        "receipt_window",
                    ):
                        repo.refund_wallet_applied(session, order)
                        order.status = "cancelled"
                        order.note = (order.note or "") + f"|aban_{inv.status}"
                        repo.touch_customer_order(order)
                        if order.kind == "create" and order.service_username:
                            repo.unlink_customer_service(
                                session, order.telegram_id, order.service_username
                            )
                        tg_notify = order.telegram_id
                bot = customer_bridge.customer_bot
                if bot and tg_notify is not None:
                    try:
                        await bot.send_message(
                            tg_notify,
                            f"❌ پرداخت سفارش #{oid} منقضی/لغو شد.\n"
                            "در صورت کسر از کیف پول، مبلغ برگشت داده شد.\n"
                            "در صورت نیاز دوباره سفارش دهید.",
                        )
                    except Exception:
                        log.exception("notify customer aban terminal failed")

    async def _poll_variz(self) -> None:
        if not variz_enabled(self.settings):
            return
        with get_session() as session:
            pending = repo.list_customer_orders_pending_variz(session)
            items = [
                (o.id, o.variz_payment_id)
                for o in pending
                if o.variz_payment_id
            ]
        for oid, payment_id in items:
            try:
                pay = await verify_payment(self.settings, payment_id)
            except VarizError as exc:
                log.debug("Variz verify order=%s: %s", oid, exc)
                continue
            except Exception:
                log.exception("Variz verify failed order=%s", oid)
                continue
            if pay.verified and pay.status == "confirmed":
                amount = pay.amount_final or ""
                result = await auto_confirm_by_amount(
                    amount or "0", source="variz_poll"
                )
                if not result.get("ok"):
                    await _confirm_order_id(oid, source="variz_poll", amount=amount)
                log.info(
                    "Variz confirmed order=%s payment=%s amount=%s",
                    oid,
                    payment_id,
                    amount,
                )
            elif pay.status in ("expired", "canceled", "rejected"):
                tg_notify: int | None = None
                with get_session() as session:
                    order = repo.get_customer_order(session, oid)
                    if order and order.status in (
                        "awaiting_payment",
                        "receipt_window",
                    ):
                        repo.refund_wallet_applied(session, order)
                        order.status = "cancelled"
                        order.note = (order.note or "") + f"|variz_{pay.status}"
                        repo.touch_customer_order(order)
                        if order.kind == "create" and order.service_username:
                            repo.unlink_customer_service(
                                session, order.telegram_id, order.service_username
                            )
                        tg_notify = order.telegram_id
                bot = customer_bridge.customer_bot
                if bot and tg_notify is not None:
                    try:
                        await bot.send_message(
                            tg_notify,
                            f"❌ پرداخت سفارش #{oid} منقضی/لغو شد.\n"
                            "در صورت کسر از کیف پول، مبلغ برگشت داده شد.\n"
                            "در صورت نیاز دوباره سفارش دهید.",
                        )
                    except Exception:
                        log.exception("notify customer variz terminal failed")


def _kind_fa(kind: str) -> str:
    if kind == "create":
        return "خرید"
    if kind == "renew":
        return "تمدید"
    if kind == "wallet":
        return "شارژ کیف پول"
    return kind


async def _notify_manual_approval(
    *,
    oid: int,
    tg: int,
    uname: str,
    kind: str,
    price: int,
    amount_show: str,
    source: str,
) -> None:
    """Ping admins that a paid order needs explicit approve/reject."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kind_fa = _kind_fa(kind)
    text = (
        f"🔔 <b>تراکنش جهت تایید دستی</b>\n"
        f"سفارش #{oid} — {kind_fa}\n"
        f"یوزر: <code>{uname}</code>\n"
        f"مبلغ: {price:,} ت / <code>{amount_show}</code> ریال\n"
        f"منبع: {source}\n"
        f"مشتری: <code>{tg}</code>\n"
        f"نیاز به واکنش ادمین: تایید یا رد"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تایید و صف سرویس",
                    callback_data=f"corder:ok:{oid}",
                ),
                InlineKeyboardButton(
                    text="🚫 رد با دلیل",
                    callback_data=f"corder:no:{oid}",
                ),
            ]
        ]
    )
    if not customer_bridge.admin_bot:
        return
    from app.config import load_settings

    settings = load_settings(require_bot=True)
    for admin_id in settings.admin_ids:
        try:
            sent = await customer_bridge.admin_bot.send_message(
                admin_id, text, reply_markup=kb
            )
            if oid and sent and getattr(sent, "message_id", None):
                with get_session() as session:
                    repo.track_admin_order_ui_message(
                        session, oid, admin_id, int(sent.message_id)
                    )
        except Exception:
            log.exception("manual approval admin notify failed")


async def _notify_after_confirm(
    *,
    oid: int,
    tg: int,
    uname: str,
    kind: str,
    price: int,
    amount_show: str,
    source: str,
    wallet_balance: int | None = None,
) -> None:
    # Wallet customer UI is handled by notify_wallet_credited (cleanup + success text).
    # create/renew: customer notify comes from enqueue_fulfillment / renew queue.

    if source in ("admin_manual", "admin_web"):
        # Admin bot / web panel already shows its own confirmation UI.
        return

    if kind == "wallet":
        kb = None
        text = (
            f"⚡ <b>شارژ کیف پول (خودکار)</b>\n"
            f"سفارش #{oid}\n"
            f"مبلغ: {price:,} ت / <code>{amount_show}</code> ریال\n"
            f"موجودی جدید: <code>{wallet_balance or 0:,}</code> ت\n"
            f"منبع: {source}\n"
            f"مشتری: <code>{tg}</code>"
        )
    else:
        # Fulfillment is auto-enqueued; info-only notice (no second approve button).
        kb = None
        text = (
            f"⚡ <b>پرداخت مشتری تایید شد</b>\n"
            f"سفارش #{oid} — {_kind_fa(kind)}\n"
            f"یوزر: <code>{uname}</code>\n"
            f"مبلغ: {price:,} ت / <code>{amount_show}</code> ریال\n"
            f"منبع: {source}\n"
            f"مشتری: <code>{tg}</code>\n"
            "فعال‌سازی به‌صورت خودکار در صف قرار گرفت."
        )

    if customer_bridge.admin_bot:
        from app.config import load_settings

        settings = load_settings(require_bot=True)
        for admin_id in settings.admin_ids:
            try:
                await customer_bridge.admin_bot.send_message(
                    admin_id, text, reply_markup=kb
                )
            except Exception:
                log.exception("notify admin after confirm failed")


async def _apply_paid_order(
    order_id: int, *, source: str, amount: str = ""
) -> dict:
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            return {"ok": False, "error": "missing"}
        if order.status in ("fulfilling", "done", "approved"):
            return {"ok": True, "already": True, "order_id": order.id}
        if order.status not in (
            "awaiting_payment",
            "receipt_window",
            "awaiting_review",
        ):
            return {"ok": False, "error": "bad_status", "status": order.status}

        order.paid_at = datetime.utcnow()
        order.pay_source = source
        if amount:
            order.amount_rial = amount
        order.note = (order.note or "") + f"|auto:{source}"
        oid = order.id
        tg = order.telegram_id
        uname = order.service_username
        kind = order.kind
        price = order.price_toman
        amount_show = order.amount_rial or amount
        wallet_balance = None
        fulfill = False
        referral_info = None

        if kind == "wallet":
            wallet_balance, credited = repo.credit_wallet_once_for_order(
                session,
                tg,
                price,
                order_id=oid,
                note=source,
            )
            order.status = "done"
            order.completed_at = datetime.utcnow()
            repo.touch_customer_order(order)
            if not credited:
                # Already credited earlier — still notify as success/idempotent
                pass
        else:
            # Paid create/renew — auto-fulfill unless admin enabled manual approval.
            order.status = "awaiting_review"
            manual = repo.is_orders_manual_approval(session)
            if manual:
                order.note = (order.note or "") + "|manual_approval"
                fulfill = False
            else:
                order.note = (order.note or "") + "|ready_fulfill"
                fulfill = True
            repo.touch_customer_order(order)
            try:
                referral_info = repo.apply_referral_credit_for_order(session, order)
            except Exception:
                log.exception("referral credit failed order=%s", oid)
                referral_info = None

    manual_needed = False
    with get_session() as session:
        manual_needed = bool(
            kind != "wallet" and repo.is_orders_manual_approval(session) and not fulfill
        )

    if referral_info and referral_info.get("ok"):
        try:
            bot = customer_bridge.customer_bot
            ref_tg = int(referral_info["referrer_id"])
            amt = int(referral_info["amount"])
            bal = int(referral_info.get("balance") or 0)
            pct = int(referral_info.get("percent") or 0)
            if bot:
                await bot.send_message(
                    ref_tg,
                    f"🎁 کمیسیون مجموعه‌گیری\n"
                    f"{pct}٪ از خرید زیرمجموعه‌تان به کیف پول واریز شد.\n"
                    f"مبلغ: <b>{amt:,}</b> تومان\n"
                    f"موجودی جدید: <b>{bal:,}</b> تومان",
                )
        except Exception:
            log.exception("referral notify failed order=%s", oid)

    if kind == "wallet":
        try:
            from app.telegram.handlers.customer import notify_wallet_credited

            await notify_wallet_credited(tg, price, order_id=oid)
        except Exception:
            log.exception("wallet credit customer notify failed order=%s", oid)
    else:
        # Remove prior invoice / card / amount messages from the customer chat.
        try:
            from app.telegram.handlers.customer import cleanup_customer_order_ui

            await cleanup_customer_order_ui(tg, oid)
        except Exception:
            log.exception("cleanup customer pay UI failed order=%s", oid)

    if manual_needed:
        await _notify_manual_approval(
            oid=oid,
            tg=tg,
            uname=uname,
            kind=kind,
            price=price,
            amount_show=amount_show,
            source=source,
        )
        if customer_bridge.customer_bot:
            try:
                await customer_bridge.customer_bot.send_message(
                    tg,
                    f"✅ پرداخت ثبت شد و برای تایید ادمین ارسال شد.\n"
                    f"سفارش #{oid} — سرویس <code>{uname}</code>",
                )
            except Exception:
                log.exception("manual-approval customer notice failed order=%s", oid)
    else:
        # Drop prior admin approve/reject receipt chats once payment is settled.
        if source not in ("admin_manual", "admin_web"):
            try:
                from app.services.admin_order_actions import (
                    cleanup_tracked_admin_order_ui,
                )

                await cleanup_tracked_admin_order_ui(
                    oid,
                    summary=f"✅ سفارش #{oid} تایید شد ({source})",
                )
            except Exception:
                log.exception("cleanup admin review UI failed order=%s", oid)
        await _notify_after_confirm(
            oid=oid,
            tg=tg,
            uname=uname,
            kind=kind,
            price=price,
            amount_show=amount_show,
            source=source,
            wallet_balance=wallet_balance,
        )
    if fulfill:
        from app.services.customer_fulfill import enqueue_fulfillment

        await enqueue_fulfillment(oid)
    return {"ok": True, "order_id": oid, "amount": amount_show}


async def _confirm_order_id(
    order_id: int, *, source: str, amount: str = ""
) -> dict:
    return await _apply_paid_order(order_id, source=source, amount=amount)


async def auto_confirm_by_amount(amount_rial: str, *, source: str = "auto_amount") -> dict:
    """
    Match a unique rial deposit to an open customer order and mark it paid.

    Call this from:
    - bank SMS/parser bridge
    - payment provider webhook / poll
    - admin command «تایید مبلغ …»
    """
    amount = str(amount_rial).replace(",", "").replace(" ", "").strip()
    with get_session() as session:
        order = repo.find_customer_order_by_amount(session, amount)
        if not order:
            return {"ok": False, "error": "no_matching_order", "amount": amount}
        oid = order.id
    return await _apply_paid_order(oid, source=source, amount=amount)
