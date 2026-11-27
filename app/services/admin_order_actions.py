"""Shared customer-order approve/reject used by admin bot and web panel."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.database.database import get_session
from app.database import repositories as repo
from app.telegram import customer_bridge

log = logging.getLogger(__name__)


async def _notify_admins_panel_action(text: str) -> None:
    bot = customer_bridge.admin_bot
    if not bot:
        return
    from app.config import load_settings

    settings = load_settings(require_bot=False)
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.exception("admin panel-action notify failed tg=%s", admin_id)


async def cleanup_tracked_admin_order_ui(
    order_id: int, *, summary: str | None = None
) -> None:
    """Delete receipt/approve chats; strip buttons on shared requests lists."""
    bot = customer_bridge.admin_bot
    if not bot:
        return
    with get_session() as session:
        entries = repo.pop_admin_order_ui_messages(session, order_id)
    seen: set[tuple[int, int]] = set()
    for chat_id, mid, delete_on_done in entries:
        key = (int(chat_id), int(mid))
        if key in seen:
            continue
        seen.add(key)
        if delete_on_done:
            try:
                await bot.delete_message(chat_id, mid)
                continue
            except Exception:
                log.debug(
                    "admin order ui delete failed order=%s chat=%s mid=%s",
                    order_id,
                    chat_id,
                    mid,
                )
        # Shared list / fallback: strip buttons; optionally rewrite text.
        text = summary or "✅/❌ این سفارش بسته شد."
        updated = False
        try:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=mid, caption=text, reply_markup=None
            )
            updated = True
        except Exception:
            pass
        if not updated:
            try:
                await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=mid, reply_markup=None
                )
                updated = True
            except Exception:
                pass
        if not updated:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=mid, reply_markup=None
                )
            except Exception:
                log.debug(
                    "admin order ui strip failed order=%s chat=%s mid=%s",
                    order_id,
                    chat_id,
                    mid,
                )


# Backward-compatible alias used by older call sites
async def _update_tracked_admin_order_ui(order_id: int, text: str) -> None:
    await cleanup_tracked_admin_order_ui(order_id, summary=text)


async def approve_customer_order(
    order_id: int, *, source: str = "admin_web", requested_by: int | None = 0
) -> dict[str, Any]:
    """Approve wallet / card / review orders and notify customer + admin bots."""
    from app.services.customer_fulfill import enqueue_fulfillment
    from app.services.customer_payments import _apply_paid_order
    from app.telegram.handlers.customer import notify_wallet_credited

    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            return {"ok": False, "error": "missing"}
        if order.status in ("done", "rejected", "cancelled", "expired"):
            return {"ok": False, "error": "final", "status": order.status}
        status0 = order.status
        amount = order.amount_rial or ""
        kind = (order.kind or "").strip()
        uname = order.service_username or "—"
        price = int(order.price_toman or 0)
        cust_tg = int(order.telegram_id)
        already_job = order.renew_job_id

        if kind == "wallet":
            bal, credited = repo.credit_wallet_once_for_order(
                session,
                cust_tg,
                price,
                order_id=order.id,
                note=source,
            )
            order.status = "done"
            order.completed_at = datetime.utcnow()
            order.paid_at = order.paid_at or datetime.utcnow()
            order.pay_source = order.pay_source or source
            repo.touch_customer_order(order)
        else:
            bal = 0
            credited = False

    if kind == "wallet":
        if credited:
            try:
                await notify_wallet_credited(cust_tg, price, order_id=order_id)
            except Exception:
                log.exception("wallet credit notify failed order=%s", order_id)
        summary = (
            f"✅ شارژ کیف پول از پلن · سفارش #{order_id}\n"
            f"+{price:,} ت → موجودی <code>{bal:,}</code> ت"
            + ("" if credited else "\n(قبلاً شارژ شده بود)")
        )
        await _update_tracked_admin_order_ui(order_id, summary)
        if source.startswith("admin_web"):
            await _notify_admins_panel_action(summary)
        return {
            "ok": True,
            "kind": "wallet",
            "credited": credited,
            "balance": bal,
        }

    if status0 in ("awaiting_payment", "receipt_window"):
        result = await _apply_paid_order(order_id, source=source, amount=amount)
        if not result.get("ok"):
            return result
        job_id = result.get("job_id")
        summary = (
            f"✅ سفارش #{order_id} تایید شد — سرویس شروع شد\n"
            f"{kind} · <code>{uname}</code> · مبلغ <code>{amount or '—'}</code>\n"
            + (
                f"صف فروشنده: #{job_id}"
                if job_id
                else "فعال‌سازی در صف است."
            )
        )
        await _update_tracked_admin_order_ui(order_id, summary)
        # Fresh chat ping so Telegram shows a new notification (not only UI cleanup).
        await _notify_admins_panel_action(summary)
        return {"ok": True, "kind": kind, "via": "paid", **result}

    if status0 in ("fulfilling", "approved") and already_job:
        return {"ok": True, "already": True, "job_id": already_job}

    result = await enqueue_fulfillment(order_id, requested_by=requested_by)
    if not result.get("ok"):
        return result
    summary = (
        f"✅ سفارش #{order_id} تایید شد — سرویس شروع شد\n"
        f"{kind} · <code>{uname}</code> → صف #{result.get('job_id')}"
    )
    await _update_tracked_admin_order_ui(order_id, summary)
    await _notify_admins_panel_action(summary)
    return {"ok": True, "kind": kind, "via": "fulfill", **result}


async def reject_customer_order(
    order_id: int,
    *,
    reason: str = "سفارش از پلن ادمین رد شد.",
    admin_id: int = 0,
    source: str = "admin_web",
) -> dict[str, Any]:
    """Reject open order, notify customer, open rejection ticket, update admin UI."""
    reason = (reason or "").strip() or "سفارش از پلن ادمین رد شد."
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            return {"ok": False, "error": "missing"}
        if order.status in ("done", "fulfilling", "expired", "cancelled"):
            return {"ok": False, "error": "bad_status", "status": order.status}
        repo.refund_wallet_applied(session, order)
        order.status = "rejected"
        order.note = (order.note or "") + f"|reject:{source}:{reason[:200]}"
        repo.touch_customer_order(order)
        cust_tg = int(order.telegram_id)
        uname = order.service_username
        kind = order.kind
        if kind == "create" and uname:
            repo.unlink_customer_service(session, cust_tg, uname)
        ticket = repo.create_support_ticket(
            session,
            telegram_id=cust_tg,
            order_id=order_id,
            kind="rejection",
            subject=f"رد سفارش #{order_id}",
            status="awaiting_customer",
        )
        repo.add_support_message(
            session,
            ticket.id,
            sender="admin",
            body=reason,
            admin_id=admin_id,
        )
        notice = repo.get_bot_message(
            session, "reject_customer_notice", order=order_id, reason=reason
        )
        repo.set_state(session, f"cust_flow:{cust_tg}", f"ticket:{ticket.id}")
        ticket_id = ticket.id

    try:
        from app.telegram.handlers.customer import cleanup_customer_order_ui

        await cleanup_customer_order_ui(cust_tg, order_id)
    except Exception:
        log.exception("cleanup after reject failed order=%s", order_id)

    if customer_bridge.customer_bot:
        try:
            await customer_bridge.customer_bot.send_message(cust_tg, notice)
        except Exception:
            log.exception("reject customer notify failed order=%s", order_id)

    summary = (
        f"❌ سفارش #{order_id} از پلن رد شد\n"
        f"تیکت #{ticket_id}\n"
        f"دلیل: {reason[:180]}"
    )
    await _update_tracked_admin_order_ui(order_id, summary)
    if source.startswith("admin_web"):
        await _notify_admins_panel_action(summary)
    return {"ok": True, "ticket_id": ticket_id}
