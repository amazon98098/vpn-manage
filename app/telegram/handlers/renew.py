"""Admin assisted renew/create UI (Phase 2/3)."""
from __future__ import annotations

import json
from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.database.database import get_session
from app.database import repositories as repo
from app.database.repositories import JOB_STATUS_FA, KIND_FA
from app.services.renew_queue import RenewQueue
from app.telegram.handlers.admin import is_admin, main_menu

router = Router(name="renew")


def _mode_keyboard(
    username: str,
    sub_id: int | None = None,
    *,
    vendor_key: str | None = None,
) -> InlineKeyboardMarkup:
    u = username[:40]
    back = f"subinfo:id:{sub_id}" if sub_id is not None else f"subinfo:{u}"
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="♻️ تمدید پلن فعلی",
                callback_data=f"renew:mode:current:{u}",
            )
        ]
    ]
    # FineArts volume plans: top-up only — no NetPlus-style duration change UI.
    if (vendor_key or "").lower() != "finearts":
        rows.append(
            [
                InlineKeyboardButton(
                    text="📦 تغییر پلن / حجم",
                    callback_data=f"renew:mode:change:{u}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _duration_keyboard(username: str, sub_id: int | None = None) -> InlineKeyboardMarkup:
    """Local sell-plan catalog (same packages customer bot uses) — no vendor fetch."""
    u = username[:40]
    ask = f"renew:ask:id:{sub_id}" if sub_id is not None else f"renew:ask:{u}"
    rows: list[list[InlineKeyboardButton]] = []
    with get_session() as session:
        plans = [
            p
            for p in repo.list_price_plans(session, enabled_only=True)
            if repo.vendor_key_for_price_plan(p) != "finearts"
            and int(p.duration_days or 0) > 0
        ]
        for plan in plans:
            days = int(plan.duration_days or 0)
            gb = int(plan.traffic_gb) if plan.traffic_gb else 0
            price = int(plan.price_toman or 0)
            label = plan.title or (
                f"{days} روز · {gb} گیگ" if gb else f"{days} روز"
            )
            if price:
                label = f"{label} — {price:,} ت"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label[:64],
                        callback_data=f"renew:plan:{plan.id}:{u}",
                    )
                ]
            )
    if not rows:
        # Fallback to classic month buttons if catalog empty.
        rows = [
            [
                InlineKeyboardButton(
                    text="📅 ۱ ماه",
                    callback_data=f"renew:dur:productextendmonths_31:{u}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 ۲ ماه",
                    callback_data=f"renew:dur:productextendmonths_61:{u}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 ۳ ماه",
                    callback_data=f"renew:dur:productextendmonths_91:{u}",
                )
            ],
        ]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=ask)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _create_plans_keyboard() -> InlineKeyboardMarkup:
    """Admin create: pick from local customer catalog prices/packages."""
    rows: list[list[InlineKeyboardButton]] = []
    with get_session() as session:
        plans = repo.list_price_plans(session, enabled_only=True)
        for plan in plans:
            days = int(plan.duration_days or 0)
            gb = int(plan.traffic_gb) if plan.traffic_gb else 0
            price = int(plan.price_toman or 0)
            vkey = repo.vendor_key_for_price_plan(plan)
            icon = "💎" if vkey == "finearts" or days <= 0 else "✨"
            if days <= 0:
                dur = "نامحدود"
            elif days >= 90:
                dur = "۳ ماه"
            elif days >= 60:
                dur = "۲ ماه"
            else:
                dur = "۱ ماه"
            gb_s = f"{gb} گیگ" if gb else (plan.title or "پلن")
            label = f"{icon} {gb_s} · {dur}"
            if price:
                label = f"{label} — {price:,} ت"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label[:64],
                        callback_data=f"create:plan:{plan.id}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="🚫 انصراف", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ui_edit(
    message,
    text: str,
    *,
    reply_markup=None,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
    await message.answer(text, reply_markup=reply_markup)


def _job_row_keyboard(job_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status in repo.ACTIVE_RENEW_STATUSES:
        rows.append(
            [InlineKeyboardButton(text="🚫 لغو", callback_data=f"renew:cancel:{job_id}")]
        )
    if status in ("failed", "cancelled", "completed"):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔁 تلاش دوباره",
                    callback_data=f"renew:retry:{job_id}",
                )
            ]
        )
    if status in ("receipt_window", "awaiting_receipt_confirm", "awaiting_auto_approve"):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧾 رسید",
                    callback_data=f"renew:receipt:{job_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="renew:queue")]])


@router.callback_query(F.data.startswith("renew:ask:"))
async def cb_renew_ask(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    data = callback.data or ""
    sub_id: int | None = None
    # renew:ask:id:123 or renew:ask:exact_username
    vendor_key = None
    if data.startswith("renew:ask:id:"):
        sub_id = int(data.rsplit(":", 1)[1])
        with get_session() as session:
            sub = repo.get_subscription(session, sub_id)
            username = sub.username if sub else None
            if sub:
                vendor = (
                    repo.get_vendor(session, sub.vendor_id)
                    if sub.vendor_id
                    else repo.resolve_vendor_for_username(session, sub.username)
                )
                vendor_key = vendor.key if vendor else None
        if not username:
            await callback.answer("یافت نشد", show_alert=True)
            return
    else:
        username = data.split(":", 2)[2]
        with get_session() as session:
            sub = repo.get_subscription_by_username(session, username)
            if not sub:
                await callback.answer("یافت نشد (نام دقیق لازم است)", show_alert=True)
                return
            username = sub.username
            sub_id = sub.id
            vendor = (
                repo.get_vendor(session, sub.vendor_id)
                if sub.vendor_id
                else repo.resolve_vendor_for_username(session, username)
            )
            vendor_key = vendor.key if vendor else None
    hint = (
        "تمدید حجم (زمان نامحدود FineArts) — فقط تاپ‌آپ پلن فعلی."
        if (vendor_key or "").lower() == "finearts"
        else "کدام مسیر؟"
    )
    await _ui_edit(
        callback.message,
        f"تمدید <code>{username}</code>\n{hint}",
        reply_markup=_mode_keyboard(username, sub_id, vendor_key=vendor_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("renew:mode:current:"))
async def cb_mode_current(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    username = (callback.data or "").split(":", 3)[3]
    await _enqueue(
        callback, renew_queue, username, "current", None, kind="renew"
    )


@router.callback_query(F.data.startswith("renew:mode:change:"))
async def cb_mode_change(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    username = (callback.data or "").split(":", 3)[3]
    sub_id = None
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        if sub:
            sub_id = sub.id
            username = sub.username
    await _ui_edit(
        callback.message,
        f"مدت تمدید برای <code>{username}</code> را انتخاب کنید:",
        reply_markup=_duration_keyboard(username, sub_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("renew:dur:"))
async def cb_duration(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    parts = (callback.data or "").split(":", 3)
    if len(parts) < 4:
        await callback.answer("داده نامعتبر", show_alert=True)
        return
    await _enqueue(
        callback, renew_queue, parts[3], "change", parts[2], kind="renew"
    )


@router.callback_query(F.data == "create:owner:skip")
async def cb_create_owner_skip(
    callback: CallbackQuery, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id
    with get_session() as session:
        username = repo.get_state(session, f"admin_create_user:{uid}")
        if not username:
            await callback.answer("نام کاربری ذخیره نشده", show_alert=True)
            return
        repo.set_state(session, f"admin_create_owner:{uid}", "")
        repo.set_state(session, f"admin_flow:{uid}", "create_wait_duration")
    await _ui_edit(
        callback.message,
        f"خرید برای <code>{username}</code>\nبدون مالک مشتری\n"
        "پلن را از کاتالوگ مشتری انتخاب کنید:",
        reply_markup=_create_plans_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("create:plan:"))
async def cb_create_plan(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    """Admin create from local sell catalog (same plans as customer bot)."""
    if not is_admin(callback.from_user.id, settings):
        return
    try:
        plan_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("پلن نامعتبر", show_alert=True)
        return
    uid = callback.from_user.id
    with get_session() as session:
        username = repo.get_state(session, f"admin_create_user:{uid}")
        owner = (repo.get_state(session, f"admin_create_owner:{uid}") or "").strip()
        plan = repo.get_price_plan(session, plan_id)
        if not plan or not plan.enabled:
            await callback.answer("پلن یافت نشد", show_alert=True)
            return
        duration_cb, package_cb, vendor_id = repo.create_job_args_from_plan(
            session, plan
        )
        if vendor_id is None and repo.vendor_key_for_price_plan(plan) == "finearts":
            fa = repo.ensure_vendor(
                session, "finearts", "FineArts", settings.finearts_bot
            )
            vendor_id = fa.id
            package_cb = package_cb or repo.finearts_package_callback_for_plan(plan)
        repo.set_state(session, f"admin_flow:{uid}", "")
        repo.set_state(session, f"admin_create_user:{uid}", "")
        repo.set_state(session, f"admin_create_owner:{uid}", "")
        plan_title = plan.title or str(plan_id)
    if not username:
        await callback.answer(
            "نام کاربری ذخیره نشده؛ دوباره از منو شروع کنید", show_alert=True
        )
        return
    await _enqueue(
        callback,
        renew_queue,
        username,
        "create",
        duration_cb,
        kind="create",
        package_callback=package_cb,
        vendor_id=vendor_id,
        owner_tg=owner,
        plan_id=plan_id,
    )
    _ = plan_title


@router.callback_query(F.data.startswith("create:dur:"))
async def cb_create_duration(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    """Legacy create callbacks kept for older admin messages."""
    if not is_admin(callback.from_user.id, settings):
        return
    duration = (callback.data or "").split(":", 2)[2]
    uid = callback.from_user.id
    with get_session() as session:
        username = repo.get_state(session, f"admin_create_user:{uid}")
        owner = (repo.get_state(session, f"admin_create_owner:{uid}") or "").strip()
        repo.set_state(session, f"admin_flow:{uid}", "")
        repo.set_state(session, f"admin_create_user:{uid}", "")
        repo.set_state(session, f"admin_create_owner:{uid}", "")
    if not username:
        await callback.answer("نام کاربری ذخیره نشده؛ دوباره از منو شروع کنید", show_alert=True)
        return
    await _enqueue(
        callback,
        renew_queue,
        username,
        "create",
        duration,
        kind="create",
        owner_tg=owner,
    )


@router.callback_query(F.data.startswith("create:fa:"))
async def cb_create_finearts(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    """Legacy FineArts create callbacks kept for older admin messages."""
    if not is_admin(callback.from_user.id, settings):
        return
    gb = (callback.data or "").rsplit(":", 1)[-1]
    if gb not in {"50", "100"}:
        await callback.answer("پلن نامعتبر", show_alert=True)
        return
    uid = callback.from_user.id
    with get_session() as session:
        username = repo.get_state(session, f"admin_create_user:{uid}")
        owner = (repo.get_state(session, f"admin_create_owner:{uid}") or "").strip()
        repo.set_state(session, f"admin_flow:{uid}", "")
        repo.set_state(session, f"admin_create_user:{uid}", "")
        repo.set_state(session, f"admin_create_owner:{uid}", "")
        fa = repo.ensure_vendor(
            session, "finearts", "FineArts", settings.finearts_bot
        )
        vendor_id = fa.id
    if not username:
        await callback.answer("نام کاربری ذخیره نشده؛ دوباره از منو شروع کنید", show_alert=True)
        return
    await _enqueue(
        callback,
        renew_queue,
        username,
        "create",
        None,
        kind="create",
        package_callback=f"fa_vol_{gb}",
        vendor_id=vendor_id,
        owner_tg=owner,
    )


@router.callback_query(F.data.startswith("renew:plan:"))
async def cb_renew_plan(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    """Admin renew change-plan using a local sell catalog plan id."""
    if not is_admin(callback.from_user.id, settings):
        return
    parts = (callback.data or "").split(":")
    # renew:plan:{plan_id}:{username}
    if len(parts) < 4:
        await callback.answer("داده نامعتبر", show_alert=True)
        return
    try:
        plan_id = int(parts[2])
    except ValueError:
        await callback.answer("پلن نامعتبر", show_alert=True)
        return
    username = parts[3]
    with get_session() as session:
        plan = repo.get_price_plan(session, plan_id)
        if not plan or not plan.enabled:
            await callback.answer("پلن یافت نشد", show_alert=True)
            return
        days = int(plan.duration_days) if plan.duration_days is not None else 31
        duration_cb = repo.renew_extend_callback_for_days(days) or None
    await _enqueue(
        callback,
        renew_queue,
        username,
        "change",
        duration_cb,
        kind="renew",
        plan_id=plan_id,
    )


async def _enqueue(
    callback: CallbackQuery,
    renew_queue: RenewQueue,
    username: str,
    plan_mode: str,
    duration_callback: str | None,
    *,
    kind: str,
    package_callback: str | None = None,
    vendor_id: int | None = None,
    owner_tg: str | None = None,
    plan_id: int | None = None,
) -> None:
    from app.utils.text import sanitize_service_username

    cleaned = sanitize_service_username(username)
    if not cleaned:
        await callback.answer(
            "نام کاربری نامعتبر است (حروف انگلیسی/عدد/_)",
            show_alert=True,
        )
        return
    username = cleaned
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        active = [j for j in repo.list_active_renew_jobs(session) if j.username == username]
        if active:
            await callback.answer("این اشتراک همین الان در صف است", show_alert=True)
            return
        job = repo.create_renew_job(
            session,
            username=username,
            vendor_service_id=sub.vendor_service_id if sub else None,
            subscription_id=sub.id if sub else None,
            plan_mode=plan_mode,
            requested_by=callback.from_user.id if callback.from_user else None,
            duration_callback=duration_callback,
            package_callback=package_callback,
            kind=kind,
            vendor_id=vendor_id,
            plan_id=plan_id,
        )
        job_id = job.id
        sub_id = sub.id if sub else None
        digits = "".join(ch for ch in (owner_tg or "") if ch.isdigit())
        if kind == "create" and digits and repo.get_customer_profile(session, int(digits)):
            repo.set_state(session, f"renew_job_owner:{job_id}", digits)

    kind_fa = KIND_FA.get(kind, kind)
    back = f"subinfo:id:{sub_id}" if sub_id is not None else "admin:svcback"
    extra = f"\nبسته: {package_callback}" if package_callback else ""
    await _ui_edit(
        callback.message,
        f"✅ در صف ثبت شد #{job_id}\n"
        f"{kind_fa}: <code>{username}</code>\n"
        f"حالت: {plan_mode}{extra}\n"
        "وقتی نوبتش برسد، دستور پرداخت می‌آید.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 صف سفارش‌ها", callback_data="renew:queue")],
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)],
            ]
        ),
    )
    await callback.answer("به صف اضافه شد")
    renew_queue.wake()


def _queue_action_rows(job) -> list[list[InlineKeyboardButton]]:
    """Compact per-job actions for a single queue message."""
    rows: list[list[InlineKeyboardButton]] = []
    jid = job.id
    if job.status in repo.ACTIVE_RENEW_STATUSES:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🚫 لغو #{jid}",
                    callback_data=f"renew:cancel:{jid}",
                )
            ]
        )
    if job.status in ("failed", "cancelled", "completed"):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔁 تلاش #{jid}",
                    callback_data=f"renew:retry:{jid}",
                )
            ]
        )
    if job.status in (
        "receipt_window",
        "awaiting_receipt_confirm",
        "awaiting_auto_approve",
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🧾 رسید #{jid}",
                    callback_data=f"renew:receipt:{jid}",
                )
            ]
        )
    return rows


async def _render_queue_message(message) -> None:
    with get_session() as session:
        active = repo.list_active_renew_jobs(session, limit=30)
        recent = repo.list_recent_terminal_jobs(session, limit=6)

    if not active and not recent:
        await _ui_edit(
            message,
            "صف سفارش‌ها خالی است.",
            reply_markup=main_menu(),
        )
        return

    now = datetime.utcnow()
    lines: list[str] = []
    kb_rows: list[list[InlineKeyboardButton]] = []

    paying = next(
        (
            j
            for j in active
            if j.status
            in (
                "awaiting_payment",
                "awaiting_auto_approve",
                "receipt_window",
                "awaiting_receipt_confirm",
            )
        ),
        None,
    )
    if paying and paying.instructions_at:
        age = int((now - paying.instructions_at).total_seconds())
        lines.append(
            "🔥 <b>در حال پرداخت</b>\n"
            f"#{paying.id} {KIND_FA.get(paying.kind or 'renew')} "
            f"<code>{paying.username}</code>\n"
            f"وضعیت: {JOB_STATUS_FA.get(paying.status, paying.status)}\n"
            f"مبلغ: <code>{paying.amount_rial or '—'}</code> ریال\n"
            f"کارت: <code>{paying.card_number or '—'}</code>\n"
            f"گذشته از فاکتور: {age} ثانیه"
        )
        kb_rows.extend(_queue_action_rows(paying))

    if active:
        lines.append("📋 <b>صف فعال</b>")
        for j in active[:10]:
            lines.append(
                f"#{j.id} {KIND_FA.get(j.kind or 'renew')} "
                f"<code>{j.username}</code> — {JOB_STATUS_FA.get(j.status, j.status)}"
                + (f" — {j.amount_rial} ریال" if j.amount_rial else "")
            )
            kb_rows.extend(_queue_action_rows(j))

    if recent:
        lines.append("🗂 <b>اخیر</b>")
        for j in recent[:6]:
            err = f" — {j.error[:60]}" if j.error and j.status == "failed" else ""
            lines.append(
                f"#{j.id} {KIND_FA.get(j.kind or 'renew')} "
                f"<code>{j.username}</code> — {JOB_STATUS_FA.get(j.status, j.status)}{err}"
            )
            kb_rows.extend(_queue_action_rows(j))

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🗑 خالی کردن صف",
                callback_data="renew:clear_ask",
            )
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu:home")]
    )
    await _ui_edit(
        message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows[:35]),
    )


@router.callback_query(F.data == "renew:queue")
async def cb_queue(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await _render_queue_message(callback.message)
    await callback.answer()


@router.callback_query(F.data == "renew:clear_ask")
async def cb_clear_ask(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        active_n = len(repo.list_active_renew_jobs(session, limit=10_000))
        recent_n = len(repo.list_recent_terminal_jobs(session, limit=10_000))
    await _ui_edit(
        callback.message,
        "🗑 <b>خالی کردن صف سفارش‌ها</b>\n"
        f"فعال: <b>{active_n}</b> · اخیر: <b>{recent_n}</b>\n"
        "همهٔ سفارش‌های فعال لغو و کل لیست صف از دیتابیس پاک می‌شود.\n"
        "ادامه؟",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ بله، خالی کن",
                        callback_data="renew:clear_ok",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🚫 انصراف",
                        callback_data="renew:queue",
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "renew:clear_ok")
async def cb_clear_ok(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        stats = repo.clear_renew_queue(session)
    renew_queue.wake()
    await callback.answer(
        f"پاک شد · لغو {stats['cancelled_active']} · حذف {stats['deleted_rows']}",
        show_alert=True,
    )
    await _render_queue_message(callback.message)


@router.callback_query(F.data.startswith("renew:retry:"))
async def cb_retry(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    job_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        old = repo.get_renew_job(session, job_id)
        if not old:
            await callback.answer("یافت نشد", show_alert=True)
            return
        if old.status in repo.ACTIVE_RENEW_STATUSES:
            await callback.answer("هنوز فعال است", show_alert=True)
            return
        active_same = [
            j
            for j in repo.list_active_renew_jobs(session)
            if j.username == old.username
        ]
        if active_same:
            await callback.answer("همین یوزر در صف است", show_alert=True)
            return
        repo.retry_renew_job(
            session, old, callback.from_user.id if callback.from_user else None
        )
    renew_queue.wake()
    await callback.answer("دوباره صف شد")
    await _render_queue_message(callback.message)


@router.callback_query(F.data.startswith("renew:cancel:"))
async def cb_cancel(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    job_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        job = repo.get_renew_job(session, job_id)
        if not job:
            await callback.answer("یافت نشد", show_alert=True)
            return
        if job.status == "completed":
            await callback.answer("قبلاً تمام شده", show_alert=True)
            return
        repo.cancel_renew_job(job, "cancelled_by_admin")
    renew_queue.wake()
    await callback.answer("لغو شد")
    await _render_queue_message(callback.message)


@router.callback_query(F.data.startswith("renew:receipt:"))
async def cb_receipt(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    job_id = int((callback.data or "").rsplit(":", 1)[1])
    result = await renew_queue.click_receipt_for_job(job_id)
    if result != "ok":
        await callback.answer(f"رسید: {result}", show_alert=True)
        return
    await callback.answer("رسید زده شد")
    await _render_queue_message(callback.message)


@router.callback_query(F.data.startswith("renew:manual_ok:"))
async def cb_manual_ok(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    """Admin completed renew/create on the vendor side manually."""
    if not is_admin(callback.from_user.id, settings):
        return
    job_id = int((callback.data or "").rsplit(":", 1)[1])
    result = await renew_queue.mark_manual_complete(job_id)
    if result != "ok":
        await callback.answer(result, show_alert=True)
        return
    await callback.answer("تایید دستی ثبت شد")
    await _render_queue_message(callback.message)


@router.callback_query(F.data.startswith("renew:pkg:"))
async def cb_package(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("نامعتبر", show_alert=True)
        return
    job_id = int(parts[2])
    idx = int(parts[3])
    with get_session() as session:
        job = repo.get_renew_job(session, job_id)
        if not job or job.status != "awaiting_package" or not job.packages_json:
            await callback.answer("این انتخاب دیگر معتبر نیست", show_alert=True)
            return
        packages = json.loads(job.packages_json)
        if idx < 0 or idx >= len(packages):
            await callback.answer("بسته نامعتبر", show_alert=True)
            return
        job.package_callback = packages[idx]["callback_data"]
        repo.touch_renew_job(job)
        label = packages[idx].get("text") or job.package_callback

    await callback.answer("بسته انتخاب شد")
    await callback.message.answer(
        f"بسته انتخاب شد برای #{job_id}:\n{label}\nادامه می‌دهیم…"
    )
    await renew_queue.resume_after_package(job_id)
