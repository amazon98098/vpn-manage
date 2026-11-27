"""One-at-a-time assisted renew/create queue and payment timing (Phase 2/3)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Awaitable, Callable

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.config import Settings
from app.database.database import get_session
from app.database import repositories as repo
from app.database.models import RenewJob
from app.database.repositories import JOB_STATUS_FA, KIND_FA
from app.telegram import customer_bridge
from app.vendors.registry import DEFAULT_VENDOR_KEY, get_vendor_driver

log = logging.getLogger(__name__)

NotifyFn = Callable[[str, InlineKeyboardMarkup | None], Awaitable[None]]

PAYMENT_STATUSES = (
    "awaiting_payment",
    "awaiting_auto_approve",
    "receipt_window",
    "awaiting_receipt_confirm",
)


def payment_keyboard(
    job_id: int,
    *,
    amount_rial: str | None = None,
    card_number: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    card = str(card_number or "").strip()
    external = bool(card and ("t.me/" in card or card.startswith("http")))
    copy_row: list[InlineKeyboardButton] = []
    if card and not external:
        copy_row.append(
            InlineKeyboardButton(
                text="💳 کپی کارت",
                copy_text=CopyTextButton(text=card.replace(" ", "")),
            )
        )
    if amount_rial:
        copy_row.append(
            InlineKeyboardButton(
                text="📋 کپی مبلغ",
                copy_text=CopyTextButton(text=str(amount_rial).replace(",", "")),
            )
        )
    if copy_row:
        rows.append(copy_row)
    if external:
        rows.append(
            [InlineKeyboardButton(text="🔗 باز کردن لینک پرداخت", url=card)]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧾 زدن دکمه رسید NetPlus",
                    callback_data=f"renew:receipt:{job_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ انجام دستی و تایید",
                callback_data=f"renew:manual_ok:{job_id}",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"renew:cancel:{job_id}")]
    )
    rows.append(
        [InlineKeyboardButton(text="📋 صف سفارش‌ها", callback_data="renew:queue")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fail_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 تلاش دوباره",
                    callback_data=f"renew:retry:{job_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ انجام دستی و تایید",
                    callback_data=f"renew:manual_ok:{job_id}",
                )
            ],
            [InlineKeyboardButton(text="📋 صف", callback_data="renew:queue")],
        ]
    )


def tx_confirmed_keyboard(job_id: int) -> InlineKeyboardMarkup:
    """After bank TX confirm — no card/amount copy buttons."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 صف سفارش‌ها",
                    callback_data="renew:queue",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ لغو",
                    callback_data=f"renew:cancel:{job_id}",
                )
            ],
        ]
    )


def success_keyboard(username: str, sub_id: int | None = None) -> InlineKeyboardMarkup:
    u = (username or "")[:40]
    detail_cb = f"subinfo:id:{sub_id}" if sub_id is not None else f"subinfo:{u}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ مشاهده اطلاعات سرویس",
                    callback_data=detail_cb,
                )
            ],
            [InlineKeyboardButton(text="📋 صف سفارش‌ها", callback_data="renew:queue")],
        ]
    )


def package_keyboard(job_id: int, packages: list[dict[str, str]]) -> InlineKeyboardMarkup:
    rows = []
    for i, pkg in enumerate(packages[:12]):
        label = (pkg.get("text") or f"بسته {i+1}")[:40]
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"renew:pkg:{job_id}:{i}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"renew:cancel:{job_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


class RenewQueue:
    def __init__(
        self,
        driver,
        settings: Settings,
        notify: NotifyFn,
    ) -> None:
        self.driver = driver
        self.settings = settings
        self.notify = notify
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._running = False
        self._receipt_notified: set[int] = set()
        self._mid_reminded: set[int] = set()
        self._next_job_after: float = 0.0
        self._sync_paused = False
        self._sync_running = False
        # job_id -> [(chat_id, message_id)] for payment screens that still show card details
        self._pay_admin_msgs: dict[int, list[tuple[int, int]]] = {}

    def start(self) -> None:
        if self._task is None:
            with get_session() as session:
                stuck = list(
                    session.scalars(select(RenewJob).where(RenewJob.status == "driving"))
                )
                for job in stuck:
                    job.status = "queued"
                    repo.touch_renew_job(job)
                    log.warning("Re-queued interrupted job #%s", job.id)
            self._running = True
            self._task = asyncio.create_task(self._loop(), name="renew-queue")
            log.info("Order queue started")

    def _driver_for_vendor_id(self, vendor_id: int | None):
        """Resolve registered adapter for job vendor; fall back to primary driver."""
        key = DEFAULT_VENDOR_KEY
        if vendor_id is not None:
            with get_session() as session:
                vendor = repo.get_vendor(session, vendor_id)
                if vendor and vendor.key:
                    key = vendor.key
        return get_vendor_driver(key) or self.driver

    def wake(self) -> None:
        """Thread-safe: admin web may call from another event loop/thread."""
        loop = customer_bridge.bot_loop
        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._wake.set)
            else:
                self._wake.set()
        except Exception:
            try:
                self._wake.set()
            except Exception:
                log.exception("renew queue wake failed")

    def _load_pay_admin_msgs(self, job_id: int) -> list[tuple[int, int]]:
        mem = self._pay_admin_msgs.get(job_id) or []
        with get_session() as session:
            db = repo.get_renew_pay_admin_messages(session, job_id)
        # Merge memory + DB (DB survives restarts).
        seen: set[tuple[int, int]] = set()
        out: list[tuple[int, int]] = []
        for pair in list(mem) + list(db):
            key = (int(pair[0]), int(pair[1]))
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    def _save_pay_admin_msgs(self, job_id: int, rows: list[tuple[int, int]]) -> None:
        cleaned = [(int(a), int(b)) for a, b in rows]
        if cleaned:
            self._pay_admin_msgs[job_id] = cleaned
        else:
            self._pay_admin_msgs.pop(job_id, None)
        with get_session() as session:
            repo.set_renew_pay_admin_messages(session, job_id, cleaned)

    def _pop_pay_admin_msgs(self, job_id: int) -> list[tuple[int, int]]:
        mem = self._pay_admin_msgs.pop(job_id, [])
        with get_session() as session:
            db = repo.pop_renew_pay_admin_messages(session, job_id)
        seen: set[tuple[int, int]] = set()
        out: list[tuple[int, int]] = []
        for pair in list(mem) + list(db):
            key = (int(pair[0]), int(pair[1]))
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    async def _notify_payment(
        self, job_id: int, text: str, markup: InlineKeyboardMarkup | None
    ) -> None:
        """Upsert payment UI: edit prior admin messages instead of stacking."""
        bot = customer_bridge.admin_bot
        if bot is None:
            await self.notify(text, markup)
            return
        tracked = self._load_pay_admin_msgs(job_id)
        if tracked:
            refreshed: list[tuple[int, int]] = []
            for chat_id, mid in tracked:
                try:
                    await bot.edit_message_text(
                        text, chat_id=chat_id, message_id=mid, reply_markup=markup
                    )
                    refreshed.append((chat_id, mid))
                except Exception:
                    try:
                        msg = await bot.send_message(
                            chat_id, text, reply_markup=markup
                        )
                        refreshed.append((chat_id, msg.message_id))
                        try:
                            await bot.delete_message(chat_id, mid)
                        except Exception:
                            pass
                    except Exception:
                        log.exception(
                            "Could not upsert payment UI for admin %s", chat_id
                        )
            if refreshed:
                self._save_pay_admin_msgs(job_id, refreshed)
            return
        new_tracked: list[tuple[int, int]] = []
        for admin_id in self.settings.admin_ids:
            try:
                msg = await bot.send_message(admin_id, text, reply_markup=markup)
                new_tracked.append((admin_id, msg.message_id))
            except Exception:
                log.exception("Could not notify admin %s (payment)", admin_id)
        if new_tracked:
            self._save_pay_admin_msgs(job_id, new_tracked)

    async def _replace_payment_ui(
        self,
        job_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None,
    ) -> None:
        """Delete card/amount payment chats, then send the final success/status notice."""
        bot = customer_bridge.admin_bot
        tracked = self._pop_pay_admin_msgs(job_id)
        if bot and tracked:
            for chat_id, mid in tracked:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception:
                    # If delete fails (too old / already gone), strip buttons at least.
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=chat_id, message_id=mid, reply_markup=None
                        )
                    except Exception:
                        log.debug(
                            "Could not delete/strip payment UI chat=%s mid=%s",
                            chat_id,
                            mid,
                        )
        # Always send a fresh final notice (never reuse the payment card message).
        await self.notify(text, markup)

    async def _upsert_job_notice(
        self, job_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        """Replace the last progress notice for this job (avoid admin message spam)."""
        await self._notify_payment(job_id, text, markup)

    async def stop(self) -> None:
        self._running = False
        self.wake()
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
                log.exception("Order queue tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def run_inventory_sync(
        self, *, max_pages: int = 50, vendor_key: str | None = None
    ) -> list[dict[str, str]]:
        """Pause new drives, run inventory sync under the vendor driver lock path."""
        if self._sync_running:
            raise RuntimeError("همگام‌سازی از قبل در حال اجراست")
        key = (vendor_key or DEFAULT_VENDOR_KEY).strip().lower()
        driver = get_vendor_driver(key) or self.driver
        self._sync_running = True
        self._sync_paused = True
        try:
            # Wait briefly if a drive is mid-flight (payment jobs may continue polling)
            for _ in range(60):
                with get_session() as session:
                    active = repo.get_driving_or_payment_job(session)
                    if not active or active.status not in ("driving", "awaiting_package"):
                        break
                await asyncio.sleep(1.0)
            return await driver.sync_service_inventory(max_pages=max_pages)
        finally:
            self._sync_paused = False
            self._sync_running = False
            self.wake()

    async def _tick(self) -> None:
        to_drive = None
        payment = None

        with get_session() as session:
            active = repo.get_driving_or_payment_job(session)
            if active and active.status in ("driving", "awaiting_package"):
                return

            if active and active.status in PAYMENT_STATUSES:
                payment = {
                    "job_id": active.id,
                    "instructions_at": active.instructions_at,
                    "amount": active.amount_rial,
                    "username": active.username,
                    "status": active.status,
                    "kind": active.kind or "renew",
                    "last_reminder_at": active.last_reminder_at,
                }
            else:
                # Do not start new NetPlus drives while inventory sync holds the UI
                if self._sync_paused:
                    queued = None
                # Only delay BETWEEN finished jobs — never inside a single NetPlus drive
                elif asyncio.get_event_loop().time() < self._next_job_after:
                    queued = None
                else:
                    queued = repo.next_queued_renew_job(session)
                if queued:
                    queued.status = "driving"
                    repo.touch_renew_job(queued)
                    to_drive = {
                        "job_id": queued.id,
                        "username": queued.username,
                        "service_id": queued.vendor_service_id,
                        "plan_mode": queued.plan_mode,
                        "duration": queued.duration_callback,
                        "package": queued.package_callback,
                        "kind": queued.kind or "renew",
                    }

        if to_drive:
            kind_fa = KIND_FA.get(to_drive["kind"], to_drive["kind"])
            await self._upsert_job_notice(
                to_drive["job_id"],
                f"▶️ شروع {kind_fa}\n<code>{to_drive['username']}</code>\n"
                f"حالت: {to_drive['plan_mode']}",
                None,
            )
            await self._drive_job(
                job_id=to_drive["job_id"],
                username=to_drive["username"],
                service_id=to_drive["service_id"],
                plan_mode=to_drive["plan_mode"] or "current",
                duration_callback=to_drive["duration"],
                package_callback=to_drive["package"],
                kind=to_drive["kind"],
            )
            return

        if not payment or not payment["instructions_at"]:
            return

        await self._advance_payment(payment)

    async def _advance_payment(self, payment: dict) -> None:
        job_id = payment["job_id"]
        username = payment["username"]
        amount = payment["amount"]
        status = payment["status"]
        kind = payment["kind"]
        kind_fa = KIND_FA.get(kind, kind)
        now = datetime.utcnow()
        age = (now - payment["instructions_at"]).total_seconds()
        timeout = self.settings.renew_payment_timeout_seconds
        receipt_after = self.settings.renew_receipt_after_seconds
        auto_window = self.settings.renew_auto_approve_seconds

        if age >= timeout:
            with get_session() as session:
                job = repo.get_renew_job(session, job_id)
                if job and job.status in PAYMENT_STATUSES:
                    repo.fail_renew_job(job, "payment_timeout")
                    cust_order = repo.find_customer_order_for_job(
                        session, renew_job_id=job_id, username=username
                    )
                    if cust_order:
                        cust_order.status = "rejected"
                        cust_order.note = (cust_order.note or "") + "|vendor_payment_timeout"
                        repo.touch_customer_order(cust_order)
                        oid_fail = cust_order.id
                    else:
                        oid_fail = None
            await self._replace_payment_ui(
                job_id,
                f"⏰ مهلت پرداخت تمام شد\n{kind_fa} <code>{username}</code>\n"
                f"مبلغ: {amount} ریال\nفاکتور منقضی؛ دوباره تلاش کنید.",
                fail_keyboard(job_id),
            )
            if oid_fail:
                from app.services.customer_fulfill import notify_fulfillment_failed

                await notify_fulfillment_failed(oid_fail, "payment_timeout")
            self._mid_reminded.discard(job_id)
            self._receipt_notified.discard(job_id)
            self._arm_queue_gap()
            self.wake()
            return

        if status == "awaiting_payment":
            with get_session() as session:
                job = repo.get_renew_job(session, job_id)
                if job and job.status == "awaiting_payment":
                    job.status = "awaiting_auto_approve"
                    repo.touch_renew_job(job)
            return

        if status == "awaiting_auto_approve":
            # No mid/receipt reminders to admin — the first card/amount message is enough.
            # Silently open receipt_window so the existing payment keyboard still works.
            _ = (auto_window, kind_fa)
            if age >= receipt_after:
                with get_session() as session:
                    job = repo.get_renew_job(session, job_id)
                    if job and job.status == "awaiting_auto_approve":
                        job.status = "receipt_window"
                        repo.touch_renew_job(job)
                        self._receipt_notified.add(job_id)

    async def _drive_job(
        self,
        *,
        job_id: int,
        username: str,
        service_id: str | None,
        plan_mode: str,
        duration_callback: str | None,
        package_callback: str | None,
        kind: str,
    ) -> None:
        vendor_id = None
        vendor_key = None
        with get_session() as session:
            job_row = repo.get_renew_job(session, job_id)
            if job_row:
                # Re-resolve from subscription so a stale/wrong job.vendor_id cannot
                # send NetPlus renews to FineArts (or reverse).
                vendor_id = repo.resolve_vendor_id_for_job(
                    session,
                    username=username,
                    subscription_id=job_row.subscription_id,
                    vendor_id=job_row.vendor_id,
                )
                if vendor_id and vendor_id != job_row.vendor_id:
                    job_row.vendor_id = vendor_id
                    repo.touch_renew_job(job_row)
                vendor = repo.get_vendor(session, vendor_id) if vendor_id else None
                vendor_key = (vendor.key or "").lower() if vendor else None
                # Drop FineArts-only package tokens on NetPlus jobs (and reverse).
                if package_callback and vendor_key != "finearts":
                    if str(package_callback).startswith("fa_") or "facb" in str(
                        package_callback
                    ).lower():
                        package_callback = None
                        job_row.package_callback = None
                        repo.touch_renew_job(job_row)
                if package_callback and vendor_key == "finearts":
                    if str(package_callback).startswith("product"):
                        package_callback = None
                        job_row.package_callback = None
                        repo.touch_renew_job(job_row)
        driver = self._driver_for_vendor_id(vendor_id)

        if kind == "create":
            result = await driver.drive_create(
                username=username,
                duration_callback=duration_callback,
                package_callback=package_callback,
            )
        else:
            result = await driver.drive_renew(
                username=username,
                service_id=service_id,
                plan_mode=plan_mode,
                duration_callback=duration_callback,
                package_callback=package_callback,
            )

        kind_fa = KIND_FA.get(kind, kind)

        if result.stage == "need_package" and result.packages:
            auto_cb = None
            with get_session() as session:
                job = repo.get_renew_job(session, job_id)
                if not job:
                    return
                # Auto-pick vendor package by sell-plan GB (order and/or job.plan_id).
                target_gb = None
                plan_ids: list[int] = []
                if job.plan_id:
                    plan_ids.append(int(job.plan_id))
                order = repo.find_customer_order_for_job(
                    session, renew_job_id=job_id, username=username
                )
                if order and order.plan_id:
                    pid = int(order.plan_id)
                    if pid not in plan_ids:
                        plan_ids.append(pid)
                for pid in plan_ids:
                    plan = repo.get_price_plan(session, pid)
                    if plan and plan.traffic_gb is not None:
                        target_gb = float(plan.traffic_gb)
                        break
                auto_cb = repo.match_vendor_package_callback(
                    result.packages, target_gb
                )
                if auto_cb:
                    job.package_callback = auto_cb
                    job.packages_json = json.dumps(result.packages, ensure_ascii=False)
                    job.status = "driving"
                    repo.touch_renew_job(job)
                    log.info(
                        "Auto-selected package for job %s gb=%s cb=%s",
                        job_id,
                        target_gb,
                        auto_cb,
                    )
                else:
                    job.status = "awaiting_package"
                    job.packages_json = json.dumps(result.packages, ensure_ascii=False)
                    repo.touch_renew_job(job)
                    log.warning(
                        "Package pick needed for job %s username=%s target_gb=%s packages=%s",
                        job_id,
                        username,
                        target_gb,
                        [
                            (p.get("text") or "")[:40]
                            for p in (result.packages or [])[:8]
                        ],
                    )
            if auto_cb:
                await self._drive_job(
                    job_id=job_id,
                    username=username,
                    service_id=service_id,
                    plan_mode=plan_mode,
                    duration_callback=duration_callback,
                    package_callback=auto_cb,
                    kind=kind,
                )
                return
            await self._upsert_job_notice(
                job_id,
                f"📦 بسته را انتخاب کنید\n{kind_fa} <code>{username}</code>",
                package_keyboard(job_id, result.packages),
            )
            return

        # Operator NetPlus wallet had balance — renew/create finished without card UI.
        if result.ok and result.stage == "vendor_wallet_settled" and not result.payment:
            await self.on_vendor_event(
                "renew_ok" if kind != "create" else "create_ok",
                {
                    "username": username,
                    "status_text": "",
                },
            )
            self._arm_queue_gap()
            self.wake()
            return

        if not result.ok or not result.payment:
            err = result.error or result.stage or "failed"
            oid_fail = None
            with get_session() as session:
                job = repo.get_renew_job(session, job_id)
                if job and job.status == "completed":
                    # Listener already completed (success race) — do not clobber UI.
                    return
                if job:
                    repo.fail_renew_job(job, err)
                    cust_order = repo.find_customer_order_for_job(
                        session, renew_job_id=job_id, username=username
                    )
                    if cust_order and cust_order.status == "fulfilling":
                        cust_order.note = (cust_order.note or "") + f"|vendor_fail:{err[:80]}"
                        repo.touch_customer_order(cust_order)
                        oid_fail = cust_order.id
            await self._upsert_job_notice(
                job_id,
                f"❌ {kind_fa} ناموفق\n<code>{username}</code>\n{err}",
                fail_keyboard(job_id),
            )
            if oid_fail:
                from app.services.customer_fulfill import notify_fulfillment_failed

                await notify_fulfillment_failed(oid_fail, str(err))
            self._arm_queue_gap()
            self.wake()
            return

        pay = result.payment
        with get_session() as session:
            job = repo.get_renew_job(session, job_id)
            if not job:
                return
            used = repo.renew_payment_message_already_used(
                session, pay.message_id, exclude_job_id=job_id
            )
            if used:
                log.warning(
                    "Rejecting reused payment msg %s (earlier job %s) for job %s",
                    pay.message_id,
                    used.id,
                    job_id,
                )
                repo.fail_renew_job(
                    job, f"stale_payment_message:{pay.message_id}"
                )
                await self._upsert_job_notice(
                    job_id,
                    f"❌ {kind_fa} ناموفق\n<code>{username}</code>\n"
                    "پرداخت قدیمی فروشنده دوباره استفاده شد؛ دوباره تمدید را بزنید.",
                    fail_keyboard(job_id),
                )
                self._arm_queue_gap()
                self.wake()
                return
            job.status = "awaiting_payment"
            job.amount_rial = pay.amount_rial
            job.card_number = pay.card
            job.card_holder = pay.holder
            job.receipt_callback = pay.receipt_callback
            job.payment_message_id = pay.message_id
            job.instructions_at = datetime.utcnow()
            job.last_reminder_at = None
            repo.touch_renew_job(job)

        self._mid_reminded.discard(job_id)
        self._receipt_notified.discard(job_id)
        # FineArts (and similar): card slot holds external t.me pay URL
        external = bool(pay.card and ("t.me/" in str(pay.card) or "http" in str(pay.card)))
        if external:
            await self._notify_payment(
                job_id,
                f"🔗 <b>پرداخت خارجی فروشنده — {kind_fa}</b>\n\n"
                f"👤 اشتراک: <code>{username}</code>\n"
                f"💵 مبلغ (ریال): <code>{pay.amount_rial or '—'}</code>\n"
                f"🏷 پیگیری: <b>{pay.holder or '—'}</b>\n"
                f"🔗 لینک: {pay.card}\n\n"
                "از لینک بالا در ربات پرداخت فروشنده واریز کنید؛ "
                "بعد از تایید، پیام موفقیت را اینجا می‌بینید.",
                payment_keyboard(job_id, amount_rial=pay.amount_rial),
            )
        else:
            await self._notify_payment(
                job_id,
                f"💳 <b>پرداخت کارت‌به‌کارت — {kind_fa}</b>\n\n"
                f"👤 اشتراک: <code>{username}</code>\n"
                f"💵 مبلغ دقیق: <code>{pay.amount_rial}</code> ریال\n"
                f"🏦 کارت: <code>{pay.card}</code>\n"
                f"📝 به نام: <b>{pay.holder or '—'}</b>\n\n"
                "⚡ همین مبلغ را واریز کنید (معمولاً تا ~۳۰ث تایید خودکار).\n"
                "از دکمه‌های کپی زیر استفاده کنید.",
                payment_keyboard(
                    job_id,
                    amount_rial=pay.amount_rial,
                    card_number=pay.card,
                ),
            )

    async def resume_after_package(self, job_id: int) -> None:
        with get_session() as session:
            job = repo.get_renew_job(session, job_id)
            if not job or job.status != "awaiting_package" or not job.package_callback:
                return
            job.status = "driving"
            repo.touch_renew_job(job)
            payload = {
                "job_id": job.id,
                "username": job.username,
                "service_id": job.vendor_service_id,
                "plan_mode": job.plan_mode,
                "duration": job.duration_callback,
                "package": job.package_callback,
                "kind": job.kind or "renew",
            }
        await self._drive_job(
            job_id=payload["job_id"],
            username=payload["username"],
            service_id=payload["service_id"],
            plan_mode=payload["plan_mode"],
            duration_callback=payload["duration"],
            package_callback=payload["package"],
            kind=payload["kind"],
        )

    async def on_vendor_event(self, kind: str, parsed: dict) -> None:
        amount = parsed.get("amount_rial")
        username = parsed.get("username")
        with get_session() as session:
            job = None
            if amount is not None:
                job = repo.match_pending_renew_by_amount(session, amount)
            if job is None and username:
                job = repo.match_pending_renew_by_username(session, username)
            if job is None:
                return
            job_id = job.id
            # Prefer vendor-reported username (FineArts may append _digits on create).
            # Always store lowercase — NetPlus and our shop treat casing as irrelevant.
            vendor_uname = repo.username_key(username)
            job_uname = repo.username_key(job.username)
            if (
                vendor_uname
                and job_uname
                and vendor_uname != job_uname
                and vendor_uname.startswith(job_uname + "_")
            ):
                job.username = vendor_uname
                repo.touch_renew_job(job)
                uname = vendor_uname
            else:
                uname = vendor_uname or job_uname
            amt = job.amount_rial
            job_kind = job.kind or "renew"
            job_vendor_id = job.vendor_id
            completed = False

            if kind == "tx_bot_ok" and job.status != "completed":
                if job.status != "awaiting_receipt_confirm":
                    job.status = "awaiting_auto_approve"
                repo.touch_renew_job(job)

            if kind in ("renew_ok", "create_ok"):
                repo.complete_renew_job(job)
                completed = True

        if kind == "payment_instructions":
            return

        kind_fa = KIND_FA.get(job_kind, job_kind)
        vendor_driver = self._driver_for_vendor_id(job_vendor_id)
        if kind == "tx_bot_ok":
            await self._replace_payment_ui(
                job_id,
                f"✅ <b>تراکنش تایید شد</b>\n"
                f"{kind_fa} <code>{uname}</code>\n"
                f"مبلغ: <code>{amt}</code> ریال\n"
                "جزئیات کارت دیگر لازم نیست.\n"
                "منتظر پیام موفقیت فروشنده…",
                tx_confirmed_keyboard(job_id),
            )
        if kind in ("renew_ok", "create_ok") and completed:
            await self._finalize_success(
                job_id=job_id,
                uname=uname,
                job_kind=job_kind,
                amt=amt,
                job_vendor_id=job_vendor_id,
                kind_fa=kind_fa,
                parsed=parsed if isinstance(parsed, dict) else {},
                vendor_driver=vendor_driver,
            )
            self._mid_reminded.discard(job_id)
            self._receipt_notified.discard(job_id)
            self._arm_queue_gap()
            self.wake()

    async def _finalize_success(
        self,
        *,
        job_id: int,
        uname: str,
        job_kind: str,
        amt: str | None,
        job_vendor_id: int | None,
        kind_fa: str,
        parsed: dict,
        vendor_driver=None,
    ) -> None:
        """Upsert inventory, refresh URL, notify admin + customer after vendor success."""
        import re

        _ = vendor_driver
        cust_tg = None
        cust_order_id = None
        sub_pk = None
        stored_url = ""
        plan_id = None
        job_plan_id = None

        with get_session() as session:
            job = repo.get_renew_job(session, job_id)
            if job and job.plan_id:
                job_plan_id = int(job.plan_id)
            vendor = None
            if job_vendor_id:
                vendor = repo.get_vendor(session, job_vendor_id)
            if vendor is None:
                vendor = repo.resolve_vendor_for_username(session, uname)
            if vendor is None:
                vendor = repo.ensure_vendor(
                    session, "netplus", "NetPlus", self.settings.netplus_bot
                )

            payload = dict(parsed or {})
            payload.setdefault("kind", "create_ok" if job_kind == "create" else "renew_ok")
            # Keep vendor username from the success message when present (lowercase).
            actual = repo.username_key(payload.get("username"))
            if actual:
                uname = actual
            uname = repo.username_key(uname) or uname
            if job and uname and (job.username or "") != uname:
                job.username = uname
                repo.touch_renew_job(job)
            payload["username"] = uname
            # Fold case-only duplicates (Maryam_X vs maryam_x) into one inventory row.
            existing = repo.get_subscription_by_username(session, uname)
            if existing is not None:
                repo.merge_username_case_duplicates(
                    session, keep=existing, canonical_username=uname
                )
            # Prefer structured URL from parser; else scrape status_text / raw.
            url_from_parsed = (payload.get("subscription_url") or "").strip()
            if not url_from_parsed:
                raw = (
                    payload.get("status_text")
                    or payload.get("raw_summary")
                    or ""
                )
                m = re.search(r"https?://\S+", str(raw))
                if m:
                    url_from_parsed = m.group(0).rstrip(").,]")
                    payload["subscription_url"] = url_from_parsed

            repo.upsert_subscription_from_alert(
                session, vendor, payload, datetime.utcnow()
            )

            order = repo.find_customer_order_for_job(
                session, renew_job_id=job_id, username=uname
            )
            if order:
                order.status = "done"
                order.completed_at = datetime.utcnow()
                if order.renew_job_id != job_id:
                    order.renew_job_id = job_id
                repo.touch_customer_order(order)
                cust_tg = order.telegram_id
                cust_order_id = order.id
                if order.plan_id:
                    plan_id = order.plan_id
            else:
                link = repo.get_link_for_username(session, uname)
                if link:
                    cust_tg = int(link.telegram_id)

            # Panel create may stash owner TG before the subscription existed.
            pending_owner = (repo.get_state(session, f"renew_job_owner:{job_id}") or "").strip()
            if pending_owner.isdigit() and len(pending_owner) >= 5:
                owner_id = int(pending_owner)
                if repo.get_customer_profile(session, owner_id):
                    try:
                        sub_for_owner = repo.get_subscription_by_username(session, uname)
                        repo.set_local_owner(
                            session,
                            to_telegram_id=owner_id,
                            initiated_by_telegram_id=0,
                            source="panel_create",
                            subscription=sub_for_owner,
                            service_username=uname,
                            note=f"job:{job_id}",
                        )
                        cust_tg = owner_id
                    except Exception:
                        log.exception(
                            "link pending owner %s for create job #%s failed",
                            owner_id,
                            job_id,
                        )
                repo.set_state(session, f"renew_job_owner:{job_id}", "")

            apply_plan = plan_id or job_plan_id
            if apply_plan:
                repo.set_subscription_product_from_plan(
                    session, username=uname, plan_id=apply_plan
                )
                plan_id = apply_plan

            sub = repo.get_subscription_by_username(session, uname)
            sub_pk = sub.id if sub else None
            stored_url = (sub.subscription_url or "").strip() if sub else ""
            if url_from_parsed and not stored_url:
                stored_url = url_from_parsed

        with get_session() as session:
            from app.services.subscription_refresh import (
                assign_subscription_url_if_matches,
                refresh_subscription_userinfo,
            )

            sub = repo.get_subscription_by_username(session, uname)
            if sub and stored_url:
                assign_subscription_url_if_matches(
                    session, sub, stored_url, apply_traffic=True
                )
                sub_pk = sub.id
                stored_url = (sub.subscription_url or stored_url or "").strip()
            try:
                refresh_subscription_userinfo(session, username=uname, force=True)
            except Exception:
                log.exception("post-create userinfo refresh failed for %s", uname)
            if plan_id is None:
                order_plan = repo.find_customer_order_for_job(
                    session, renew_job_id=job_id, username=uname
                )
                if order_plan and order_plan.plan_id:
                    plan_id = order_plan.plan_id
                elif job_plan_id:
                    plan_id = job_plan_id
            repo.clear_subscription_from_alert_lists_after_renew(
                session, username=uname, plan_id=plan_id
            )
            sub = repo.get_subscription_by_username(session, uname)
            if sub:
                sub_pk = sub.id
                if not stored_url:
                    stored_url = (sub.subscription_url or "").strip()
                # Keep job.subscription_id in sync so admin menus resolve the new row.
                job = repo.get_renew_job(session, job_id)
                if job and job.subscription_id != sub.id:
                    job.subscription_id = sub.id
                    repo.touch_renew_job(job)

        # Always push a clear admin success notice (no subscription link in admin bot).
        await self._replace_payment_ui(
            job_id,
            f"🎉 <b>{kind_fa} موفق</b>\n"
            f"سرویس: <code>{uname}</code>\n"
            f"مبلغ: <code>{amt or '—'}</code> ریال\n"
            "عملیات فروشنده تمام شد — موجودی ادمین به‌روز شد.",
            success_keyboard(uname, sub_pk),
        )

        if cust_tg and customer_bridge.customer_bot:
            try:
                if cust_order_id:
                    from app.telegram.handlers.customer import (
                        cleanup_customer_order_ui,
                    )

                    await cleanup_customer_order_ui(cust_tg, cust_order_id)
                await self._notify_customer_final(
                    telegram_id=cust_tg,
                    username=uname,
                    job_kind=job_kind,
                    order_id=cust_order_id,
                    sub_id=sub_pk,
                    url=stored_url,
                )
            except Exception:
                log.exception("notify customer create/renew ok failed")
        elif job_kind == "create":
            log.info(
                "create_ok job #%s username=%s: no customer owner to notify",
                job_id,
                uname,
            )

    async def _notify_customer_final(
        self,
        *,
        telegram_id: int,
        username: str,
        job_kind: str,
        order_id: int | None,
        sub_id: int | None,
        url: str,
    ) -> None:
        """Send create (QR caption) or renew success UI to the customer."""
        from app.telegram.handlers.customer import (
            send_create_success_to_customer,
            send_renew_success_to_customer,
        )
        from app.utils.text import format_jalali_date

        product = "—"
        duration_days = None
        expire_jalali = "—"
        final_url = (url or "").strip()
        with get_session() as session:
            sub = (
                repo.get_subscription(session, sub_id)
                if sub_id is not None
                else repo.get_subscription_by_username(session, username)
            )
            if sub:
                product = sub.product_name or product
                expire_jalali = format_jalali_date(
                    getattr(sub, "expiration_date", None)
                )
                if not final_url:
                    final_url = (sub.subscription_url or "").strip()
                if sub_id is None:
                    sub_id = sub.id
            if order_id:
                order = repo.get_customer_order(session, order_id)
                if order and order.plan_id:
                    plan = repo.get_price_plan(session, order.plan_id)
                    if plan:
                        duration_days = plan.duration_days
                        if product == "—" and plan.title:
                            product = plan.title

        if (job_kind or "renew") == "create":
            await send_create_success_to_customer(
                telegram_id,
                username=username,
                product=product,
                duration_days=duration_days,
                url=final_url,
            )
            return

        await send_renew_success_to_customer(
            telegram_id,
            username=username,
            product=product,
            expire_jalali=expire_jalali,
            sub_id=sub_id,
        )

    def _arm_queue_gap(self) -> None:
        gap = float(self.settings.queue_gap_seconds or 0)
        if gap > 0:
            self._next_job_after = asyncio.get_event_loop().time() + gap
            log.info("Next queue job after %.1fs gap", gap)

    async def mark_manual_complete(self, job_id: int) -> str:
        """Admin finished renew/create manually on vendor — mark done + notify customer."""
        with get_session() as session:
            job = repo.get_renew_job(session, job_id)
            if not job:
                return "یافت نشد"
            if job.status == "completed":
                return "قبلاً تمام شده"
            uname = job.username
            amt = job.amount_rial
            job_kind = job.kind or "renew"
            job_vendor_id = job.vendor_id
            order = repo.find_customer_order_for_job(
                session, renew_job_id=job_id, username=uname
            )
            if order:
                order.note = (order.note or "") + "|manual_admin_complete"
                repo.touch_customer_order(order)
            repo.complete_renew_job(job)

        kind_fa = KIND_FA.get(job_kind, job_kind)
        # Reuse the same inventory + owner + customer create/renew notify path.
        await self._finalize_success(
            job_id=job_id,
            uname=uname,
            job_kind=job_kind,
            amt=amt,
            job_vendor_id=job_vendor_id,
            kind_fa=f"{kind_fa} (دستی)",
            parsed={"kind": "create_ok" if job_kind == "create" else "renew_ok", "username": uname},
        )
        self._mid_reminded.discard(job_id)
        self._receipt_notified.discard(job_id)
        self._arm_queue_gap()
        self.wake()
        return "ok"

    async def click_receipt_for_job(self, job_id: int) -> str:
        with get_session() as session:
            job = repo.get_renew_job(session, job_id)
            if not job:
                return "یافت نشد"
            if not job.receipt_callback:
                return "دکمه رسید ذخیره نشده"
            cb = job.receipt_callback
            mid = job.payment_message_id
            uname = job.username
            vendor_id = job.vendor_id
            job.status = "awaiting_receipt_confirm"
            repo.touch_renew_job(job)
        driver = self._driver_for_vendor_id(vendor_id)
        ok = await driver.click_receipt(cb, mid)
        if ok:
            await self.notify(
                f"📤 دکمه رسید برای <code>{uname}</code> زده شد.\n"
                "الان <b>عکس رسید را همین‌جا در ربات ادمین</b> بفرستید تا به فروشنده فوروارد شود.",
                None,
            )
            return "ok"
        return "دکمه پیدا نشد"

    async def forward_receipt_photo(self, job_id: int, file_bytes: bytes) -> str:
        with get_session() as session:
            job = repo.get_renew_job(session, job_id)
            if not job:
                return "یافت نشد"
            if job.status not in ("receipt_window", "awaiting_receipt_confirm", "awaiting_auto_approve"):
                return f"وضعیت فعلی اجازه رسید نمی‌دهد ({JOB_STATUS_FA.get(job.status, job.status)})"
            uname = job.username
            vendor_id = job.vendor_id
            if job.status != "awaiting_receipt_confirm":
                job.status = "awaiting_receipt_confirm"
                repo.touch_renew_job(job)
        driver = self._driver_for_vendor_id(vendor_id)
        try:
            await driver.send_receipt_photo(file_bytes)
        except Exception as exc:
            log.exception("send receipt photo failed")
            return f"ارسال ناموفق: {exc}"
        await self.notify(
            f"🖼 رسید برای <code>{uname}</code> به فروشنده ارسال شد.\nمنتظر تایید فروشنده…",
            None,
        )
        return "ok"

    async def refresh_subscription(self, username: str) -> str:
        # Fast path: HTTP userinfo only when URL filename matches username exactly.
        with get_session() as session:
            from app.services.subscription_refresh import refresh_subscription_userinfo

            if refresh_subscription_userinfo(
                session, username=username, force=True
            ):
                return "ok"
            sub = repo.get_subscription_by_username(session, username)
            service_id = sub.vendor_service_id if sub else None
            vendor = repo.resolve_vendor_for_username(session, username)
            if vendor is None:
                vendor = repo.ensure_vendor(
                    session, "netplus", "NetPlus", self.settings.netplus_bot
                )
            vendor_id = vendor.id
            vendor_key = vendor.key
            if vendor_key == "finearts":
                repo.ensure_vendor(
                    session, "finearts", "FineArts", self.settings.finearts_bot
                )
        # Username-exact vendor UI only (never NetPlus id / chat history).
        driver = self._driver_for_vendor_id(vendor_id)
        data = await driver.refresh_subscription_status(username, service_id)
        if not data:
            return "نتوانست وضعیت را از فروشنده بگیرد"
        with get_session() as session:
            vendor = repo.get_vendor(session, vendor_id) or repo.resolve_vendor_for_username(
                session, username
            )
            if vendor is None:
                return "فروشنده یافت نشد"
            if data.get("kind") in (None, "unknown"):
                data["kind"] = "status_active"
            repo.upsert_subscription_from_alert(session, vendor, data, datetime.utcnow())
        return "ok"
