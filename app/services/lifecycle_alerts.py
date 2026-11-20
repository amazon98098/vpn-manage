"""Phase 6: local almost-out alerts + helpers for provider-delete notify."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.database import get_session
from app.database import repositories as repo
from app.services.subscription_refresh import refresh_subscription_userinfo
from app.telegram import customer_bridge
from app.telegram.ui_helpers import ltr
from app.utils.text import (
    expire_within_days,
    format_traffic_alert,
    is_expire_passed,
    remaining_total_seconds,
)

log = logging.getLogger(__name__)


def _cooldown_ok(last: datetime | None, cooldown_seconds: int) -> bool:
    if last is None:
        return True
    return datetime.utcnow() - last >= timedelta(seconds=cooldown_seconds)


def lifecycle_action_keyboard(username: str) -> InlineKeyboardMarkup:
    u = (username or "")[:40]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♻️ تمدید سرویس",
                    callback_data=f"cust:svcrenew:{u}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت به اطلاعات سرویس",
                    callback_data=f"cust:svc:{u}",
                )
            ],
        ]
    )


def scan_almost_out_alerts(session) -> list[dict[str, Any]]:
    """Return customer notifications to send (does not send). Updates alert timestamps."""
    days_th = repo.get_alert_days_threshold(session)
    traffic_th = repo.get_alert_traffic_mb_threshold(session)
    cooldown = repo.get_lifecycle_alert_cooldown_seconds(session)
    out: list[dict[str, Any]] = []

    for link, sub in repo.list_owned_usable_subscriptions(session):
        # Never remind customers about removed / deleted inventory.
        status = (sub.status or "").strip().lower()
        if status in {"removed_by_vendor", "deleted"} or not (sub.username or "").strip():
            continue

        # Best-effort TTL userinfo so thresholds use fresher local data.
        try:
            refresh_subscription_userinfo(session, subscription=sub, force=False)
        except Exception:
            log.exception("lifecycle userinfo refresh failed for %s", sub.username)

        # Refresh may have marked the row removed; skip again.
        status = (sub.status or "").strip().lower()
        if status in {"removed_by_vendor", "deleted"}:
            continue

        product = sub.product_name or "—"
        uname = sub.username

        # Days / expire — derived from expiration_date (full datetime)
        sec = remaining_total_seconds(sub.expiration_date)
        days_done = sub.status in ("expired",) or is_expire_passed(sub.expiration_date)
        days_low = (
            not days_done
            and sec is not None
            and expire_within_days(sub.expiration_date, days_th)
        )
        days_val = 0 if sec is None or sec <= 0 else max(1, (sec + 86399) // 86400)
        if (days_done or days_low) and _cooldown_ok(sub.last_days_alert_at, cooldown):
            if days_done:
                text = repo.get_bot_message(
                    session,
                    "alert_expired_days",
                    username=ltr(uname).lstrip("\u200e"),
                    days=0,
                    product=product,
                )
                kind = "expired"
                if sub.status not in ("expired", "volume_exhausted", "removed_by_vendor"):
                    sub.status = "expired"
            else:
                text = repo.get_bot_message(
                    session,
                    "alert_almost_out_days",
                    username=ltr(uname).lstrip("\u200e"),
                    days=days_val,
                    product=product,
                )
                kind = "days"
                # Keep inventory status alone; vendor-warnings use vendor_alert_at only.
            out.append(
                {
                    "telegram_id": int(link.telegram_id),
                    "username": uname,
                    "kind": kind,
                    "text": text,
                }
            )
            sub.last_days_alert_at = datetime.utcnow()

        # Traffic / exhausted
        traf = sub.remaining_traffic_mb
        traf_done = (
            sub.status == "volume_exhausted"
            or (traf is not None and traf <= 0)
        )
        traf_low = (
            traf is not None
            and 0 < traf <= traffic_th
            and not traf_done
        )
        if (traf_done or traf_low) and _cooldown_ok(
            sub.last_traffic_alert_at, cooldown
        ):
            if traf_done:
                text = repo.get_bot_message(
                    session,
                    "alert_volume_exhausted",
                    username=ltr(uname).lstrip("\u200e"),
                    traffic=format_traffic_alert(0 if traf is None else max(0.0, traf)),
                    product=product,
                )
                kind = "volume_exhausted"
                if sub.status not in ("expired", "removed_by_vendor"):
                    sub.status = "volume_exhausted"
            else:
                text = repo.get_bot_message(
                    session,
                    "alert_almost_out_traffic",
                    username=ltr(uname).lstrip("\u200e"),
                    traffic=format_traffic_alert(traf),
                    product=product,
                )
                kind = "traffic"
            out.append(
                {
                    "telegram_id": int(link.telegram_id),
                    "username": uname,
                    "kind": kind,
                    "text": text,
                }
            )
            sub.last_traffic_alert_at = datetime.utcnow()

        # Heal stale expired/exhausted when live data is healthy again (URL refresh).
        # Never clear vendor_alert_at here — only renew/create / mark-active does that.
        if sub.status in ("expired", "volume_exhausted", "warning") and not (
            days_done or days_low or traf_done or traf_low
        ):
            if sub.status != "removed_by_vendor":
                if getattr(sub, "vendor_alert_at", None) is not None:
                    sub.status = "warning"
                else:
                    sub.status = "active"

    session.flush()
    return out


def check_all_services_for_almost_out(
    session=None, *, force: bool = True
) -> dict[str, int]:
    """Refresh userinfo for every usable inventory row, then count almost-out.

    Used by admin panel / bot manual «check all» — does not send customer alerts.
    Uses one short DB session per service so SQLite is not locked across HTTP waits.
    ``session`` is optional (kept for call-site compatibility) and is not held open
    across the full scan.
    """
    from sqlalchemy import select

    from app.database.database import get_session
    from app.database.models import Subscription, USABLE_SUBSCRIPTION_STATUSES, Vendor
    from app.services.subscription_refresh import (
        apply_userinfo_fields,
        userinfo_ttl_expired,
    )
    from app.services.subscription_userinfo import (
        fetch_subscription_userinfo,
        url_username_matches,
    )
    from app.vendors.registry import vendor_supports_url_userinfo

    _ = session  # callers may still pass a session; we open our own short ones.
    with get_session() as s0:
        sub_ids = list(
            s0.scalars(
                select(Subscription.id)
                .where(Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)))
                .order_by(Subscription.id.asc())
            )
        )
        days_th = repo.get_alert_days_threshold(s0)
        traffic_th = repo.get_alert_traffic_mb_threshold(s0)
        ttl_default = repo.get_userinfo_ttl_seconds(s0)

    checked = 0
    failed = 0
    for sid in sub_ids:
        checked += 1
        uname = f"id:{sid}"
        try:
            # 1) Short read — release DB before HTTP.
            url = ""
            vendor_key = repo.DEFAULT_VENDOR_KEY
            strategy = "automatic"
            last_userinfo_at = None
            with get_session() as s1:
                sub = s1.get(Subscription, int(sid))
                if sub is None:
                    continue
                uname = sub.username or uname
                url = (sub.subscription_url or "").strip()
                last_userinfo_at = sub.last_userinfo_at
                if sub.vendor_id:
                    vendor = s1.get(Vendor, sub.vendor_id)
                    if vendor:
                        vendor_key = vendor.key or vendor_key
                        strategy = (
                            getattr(vendor, "info_refresh_strategy", None)
                            or "automatic"
                        )

            if not url.startswith("http"):
                continue
            if not vendor_supports_url_userinfo(
                vendor_key=vendor_key, info_refresh_strategy=strategy
            ):
                continue

            class _TtlProbe:
                pass

            probe = _TtlProbe()
            probe.last_userinfo_at = last_userinfo_at
            if not force and not userinfo_ttl_expired(
                probe, ttl_default  # type: ignore[arg-type]
            ):
                continue

            # 2) HTTP with no open write transaction.
            ui = fetch_subscription_userinfo(url, timeout=20.0)
            if not ui.ok:
                failed += 1
                continue
            if not url_username_matches(uname, ui.filename):
                with get_session() as s_clear:
                    sub = s_clear.get(Subscription, int(sid))
                    if sub is not None:
                        sub.subscription_url = None
                failed += 1
                continue

            # 3) Short write of fresh fields.
            with get_session() as s2:
                sub = s2.get(Subscription, int(sid))
                if sub is None:
                    continue
                apply_userinfo_fields(
                    sub,
                    ui.as_subscription_fields(),
                    session=s2,
                    days_th=days_th,
                    traffic_th=traffic_th,
                )
        except Exception:
            failed += 1
            log.exception("almost-out check refresh failed for %s", uname)

    with get_session() as s2:
        _items, almost_n = repo.list_local_almost_out(s2, offset=0, limit=1)
    return {
        "checked": int(checked),
        "failed": int(failed),
        "almost_out": int(almost_n or 0),
    }


async def send_customer_lifecycle_messages(messages: list[dict[str, Any]]) -> int:
    bot = customer_bridge.customer_bot
    if not bot or not messages:
        return 0
    sent = 0
    for item in messages:
        try:
            uname = str(item.get("username") or "")
            await bot.send_message(
                item["telegram_id"],
                item["text"],
                reply_markup=lifecycle_action_keyboard(uname) if uname else None,
            )
            sent += 1
        except Exception:
            log.exception(
                "lifecycle notify failed tg=%s user=%s",
                item.get("telegram_id"),
                item.get("username"),
            )
    return sent


class LifecycleWatcher:
    """Periodic local almost-out scan (no NetPlus bot)."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="lifecycle-watcher")

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
                closed_payload: list[dict[str, Any]] = []
                with get_session() as session:
                    interval = repo.get_lifecycle_scan_interval_seconds(session)
                    messages = scan_almost_out_alerts(session)
                    closed = repo.autoclose_stale_customer_tickets(session)
                    for ticket in closed:
                        closed_payload.append(
                            {
                                "telegram_id": int(ticket.telegram_id),
                                "text": repo.get_bot_message(
                                    session,
                                    "support_ticket_closed_timeout",
                                    id=ticket.id,
                                    subject=ticket.subject or "—",
                                ),
                            }
                        )
                if messages:
                    n = await send_customer_lifecycle_messages(messages)
                    log.info("Lifecycle almost-out sent=%s queued=%s", n, len(messages))
                if closed_payload:
                    bot = customer_bridge.customer_bot
                    sent = 0
                    if bot:
                        for item in closed_payload:
                            try:
                                await bot.send_message(
                                    item["telegram_id"], item["text"]
                                )
                                sent += 1
                            except Exception:
                                log.exception(
                                    "ticket autoclose notify failed tg=%s",
                                    item.get("telegram_id"),
                                )
                    log.info(
                        "Lifecycle autoclose tickets=%s notified=%s",
                        len(closed_payload),
                        sent,
                    )
            except Exception:
                log.exception("LifecycleWatcher tick failed")
                interval = 300
            await asyncio.sleep(interval)
