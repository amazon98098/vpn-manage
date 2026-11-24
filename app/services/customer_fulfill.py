"""Enqueue vendor create/renew after a customer order is fully paid."""
from __future__ import annotations

import logging
from datetime import datetime

from app.database.database import get_session
from app.database import repositories as repo
from app.telegram import customer_bridge

log = logging.getLogger(__name__)

# Only these statuses may enter vendor fulfillment
FULFILL_ALLOWED = frozenset(
    ("awaiting_review", "approved", "fulfilling")
)
FULFILL_BLOCKED = frozenset(
    ("cancelled", "rejected", "expired", "awaiting_payment", "receipt_window", "draft")
)


def _duration_callback_for_days(days: int) -> str:
    if days >= 90:
        return "productmonth_91"
    if days >= 60:
        return "productmonth_61"
    return "productmonth_31"


async def enqueue_fulfillment(order_id: int, *, requested_by: int | None = None) -> dict:
    """
    Mark order fulfilling, link service, enqueue NetPlus create/renew.
    Idempotent if already fulfilling/done with a job.
    Refuses cancelled / rejected / expired / unpaid open statuses.
    """
    referral_info = None
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            return {"ok": False, "error": "missing"}
        if order.kind == "wallet":
            return {"ok": False, "error": "wallet_order"}
        if order.status in FULFILL_BLOCKED:
            return {"ok": False, "error": "bad_status", "status": order.status}
        if order.status == "done" and order.renew_job_id:
            return {"ok": True, "already": True, "job_id": order.renew_job_id}
        if order.status == "fulfilling" and order.renew_job_id:
            return {"ok": True, "already": True, "job_id": order.renew_job_id}

        # Full-wallet path may set status=fulfilling before paid_at; require money signal
        if order.status not in FULFILL_ALLOWED and not order.paid_at:
            return {"ok": False, "error": "not_paid", "status": order.status}
        if order.status not in FULFILL_ALLOWED and order.pay_source != "wallet":
            return {"ok": False, "error": "bad_status", "status": order.status}

        try:
            referral_info = repo.apply_referral_credit_for_order(session, order)
        except Exception:
            log.exception("referral credit on fulfill failed order=%s", order_id)

        uname = order.service_username
        kind = order.kind
        tg = order.telegram_id
        days = 31
        traffic_gb = None
        if order.plan_id:
            plan = repo.get_price_plan(session, order.plan_id)
            if plan:
                days = plan.duration_days
                traffic_gb = plan.traffic_gb

        repo.link_customer_service(session, tg, uname)
        sub = repo.get_subscription_by_username(session, uname)
        if order.plan_id:
            repo.set_subscription_product_from_plan(
                session, username=uname, subscription=sub, plan_id=order.plan_id
            )

        plan = repo.get_price_plan(session, order.plan_id) if order.plan_id else None
        plan_vendor_key = repo.vendor_key_for_price_plan(plan)
        vendor_id = sub.vendor_id if sub else None
        if vendor_id is None:
            vrow = repo.get_vendor_by_key(session, plan_vendor_key) or repo.get_vendor_by_key(
                session, repo.DEFAULT_VENDOR_KEY
            )
            vendor_id = vrow.id if vrow else None

        sell_plan_id = int(order.plan_id) if order.plan_id else None
        if kind == "create":
            fa_pkg = repo.finearts_package_callback_for_plan(plan)
            job = repo.create_renew_job(
                session,
                username=uname,
                vendor_service_id=None,
                subscription_id=None,
                plan_mode="create",
                requested_by=requested_by or tg,
                duration_callback=(
                    None
                    if plan_vendor_key == "finearts"
                    else _duration_callback_for_days(days)
                ),
                package_callback=fa_pkg,
                kind="create",
                vendor_id=vendor_id,
                plan_id=sell_plan_id,
            )
        else:
            renew_mode, renew_dur = repo.renew_job_args_from_order(session, order)
            fa_pkg = repo.finearts_package_callback_for_plan(plan)
            job = repo.create_renew_job(
                session,
                username=uname,
                vendor_service_id=sub.vendor_service_id if sub else None,
                subscription_id=sub.id if sub else None,
                plan_mode=renew_mode,
                requested_by=requested_by or tg,
                duration_callback=renew_dur,
                package_callback=fa_pkg,
                kind="renew",
                vendor_id=vendor_id,
                plan_id=sell_plan_id,
            )

        order.status = "fulfilling"
        order.renew_job_id = job.id
        if not order.paid_at:
            order.paid_at = datetime.utcnow()
        repo.touch_customer_order(order)
        jid = job.id
        # stash for notify
        _ = traffic_gb

    if referral_info and referral_info.get("ok") and customer_bridge.customer_bot:
        try:
            await customer_bridge.customer_bot.send_message(
                int(referral_info["referrer_id"]),
                f"🎁 کمیسیون مجموعه‌گیری\n"
                f"{int(referral_info.get('percent') or 0)}٪ از خرید زیرمجموعه‌تان "
                f"به کیف پول واریز شد.\n"
                f"مبلغ: <b>{int(referral_info['amount']):,}</b> تومان\n"
                f"موجودی جدید: <b>{int(referral_info.get('balance') or 0):,}</b> تومان",
            )
        except Exception:
            log.exception("referral notify on fulfill failed order=%s", order_id)

    if not customer_bridge.wake_renew_queue():
        log.warning("wake renew queue skipped (queue offline)")

    if customer_bridge.customer_bot:
        try:
            sent = await customer_bridge.customer_bot.send_message(
                tg,
                f"✅ پرداخت تایید شد.\n"
                f"سرویس <code>{uname}</code> در صف فعال‌سازی است.",
            )
            from app.telegram.handlers.customer import _track_order_ui_message

            _track_order_ui_message(order_id, sent.message_id if sent else None)
        except Exception:
            log.exception("notify customer fulfill failed")

    return {"ok": True, "job_id": jid, "order_id": order_id}


async def notify_fulfillment_failed(order_id: int, reason: str = "") -> None:
    """Notify admins only — customers are not messaged on vendor fulfill failure.

    Admin already tracks the renew queue; they can message the customer if needed.
    """
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            return
        tg = order.telegram_id
        uname = order.service_username
        oid = order.id
    if customer_bridge.admin_bot:
        from app.config import load_settings

        settings = load_settings(require_bot=True)
        for admin_id in settings.admin_ids:
            try:
                await customer_bridge.admin_bot.send_message(
                    admin_id,
                    f"❌ Fulfill failed order #{oid} user={uname} tg={tg}\n{reason[:200]}",
                )
            except Exception:
                pass
