"""Persian admin-only Telegram bot menus (Phase 1 + 3 UX)."""
from __future__ import annotations

import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings
from app.database.database import get_session
from app.database import repositories as repo
from app.services.renew_queue import RenewQueue
from app.telegram.keyboards import (
    BTN_CARD,
    BTN_CREATE,
    BTN_HELP,
    BTN_LOCAL_ALERTS,
    BTN_PRICES,
    BTN_QUEUE,
    BTN_REQUESTS,
    BTN_SEARCH,
    BTN_SERVICES,
    BTN_SETTINGS,
    BTN_START,
    BTN_STATUS,
    BTN_OWNERSHIP,
    BTN_SYNC,
    BTN_VENDOR,
    BTN_WARNINGS,
    admin_reply_menu,
)
from app.telegram.menu_config import labels_for as admin_menu_labels
from app.telegram import customer_bridge
from app.telegram.ui_helpers import (
    ltr,
    ltr_html,
    pagination_row,
    safe_answer,
    status_fa,
    total_pages,
)
from app.services.customer_payments import auto_confirm_by_amount
from app.utils.duration import (
    format_seconds_fa,
    format_seconds_list_fa,
    minutes_to_seconds,
)
from app.utils.text import format_mb

router = Router(name="admin")
PAGE_SIZE = 10


def is_admin(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.admin_ids


def main_menu() -> InlineKeyboardMarkup:
    """Lightweight ops shortcuts — configurable items live under Settings."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 وضعیت کلی", callback_data="menu:status")],
            [
                InlineKeyboardButton(
                    text="⚠️ هشدار فروشنده", callback_data="menu:warnings"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪫 نزدیک اتمام (محلی)", callback_data="menu:local_alerts"
                )
            ],
            [InlineKeyboardButton(text="📥 درخواست‌ها", callback_data="menu:requests")],
            [InlineKeyboardButton(text="📋 صف سفارش‌ها", callback_data="renew:queue")],
            [InlineKeyboardButton(text="⚙️ تنظیمات", callback_data="menu:settings")],
            [InlineKeyboardButton(text="ℹ️ راهنما", callback_data="menu:help")],
        ]
    )


def settings_menu() -> InlineKeyboardMarkup:
    from app.telegram.menu_config import build_admin_inline_menu

    return build_admin_inline_menu("settings")


def requests_menu() -> InlineKeyboardMarkup:
    from app.telegram.menu_config import build_admin_inline_menu

    return build_admin_inline_menu("requests")


async def _ui_show(
    message: Message,
    text: str,
    *,
    reply_markup=None,
    edit: bool = False,
) -> Message | None:
    """Edit menus in place; fall back to a new message if edit fails."""
    if edit and message is not None:
        try:
            await message.edit_text(text, reply_markup=reply_markup)
            return message
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return message
    try:
        return await message.answer(text, reply_markup=reply_markup)
    except Exception:
        return None


def sub_actions(
    username: str,
    sub_id: int | None = None,
    *,
    back_cb: str = "admin:svcback",
    hide_alert_src: str | None = None,
) -> InlineKeyboardMarkup:
    """Detail actions — one clear job per row.

    hide_alert_src: ``local_alerts`` | ``warnings`` → show matching hide button.
    """
    u = username[:40]
    if sub_id is not None:
        renew_cb = f"renew:ask:id:{sub_id}"
        refresh_cb = f"sub:refresh:id:{sub_id}"
    else:
        renew_cb = f"renew:ask:{u}"
        refresh_cb = f"sub:refresh:{u}"
    rows = [
        [InlineKeyboardButton(text="♻️ تمدید اشتراک", callback_data=renew_cb)],
        [
            InlineKeyboardButton(
                text="🔗 لینک اشتراک",
                callback_data=f"admin:svclink:{u}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🔄 بروزرسانی از لینک",
                callback_data=refresh_cb,
            )
        ],
    ]
    if sub_id is not None:
        # Ownership assign only from inventory/search — not from almost-out/warnings.
        if hide_alert_src not in {"local_alerts", "warnings"}:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="👤 تعیین / جابه‌جایی مالک",
                        callback_data=f"own:as:{sub_id}",
                    )
                ]
            )
        if hide_alert_src == "local_alerts":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="👁‍🗨 پنهان از نزدیک اتمام",
                        callback_data=f"local:hide:{sub_id}",
                    )
                ]
            )
        elif hide_alert_src == "warnings":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="👁‍🗨 پنهان از هشدار فروشنده",
                        callback_data=f"warn:hide:{sub_id}",
                    )
                ]
            )
    rows.append(
        [InlineKeyboardButton(text="⬅️ بازگشت به لیست", callback_data=back_cb)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_back_cb(session, admin_uid: int) -> str:
    src = (repo.get_state(session, f"admin_detail_from:{admin_uid}") or "").strip()
    if src == "warnings":
        raw = (repo.get_state(session, f"admin_warn_page:{admin_uid}") or "0").strip()
        try:
            page = max(0, int(raw))
        except ValueError:
            page = 0
        return f"warn:pages:{page}"
    if src == "local_alerts":
        raw = (repo.get_state(session, f"admin_local_page:{admin_uid}") or "0").strip()
        try:
            page = max(0, int(raw))
        except ValueError:
            page = 0
        return f"local:pages:{page}"
    return "admin:svcback"


def _alert_reasons_for_sub(session, sub) -> frozenset[str]:
    from app.utils.text import expire_within_days, is_expire_passed

    days_th = repo.get_alert_days_threshold(session)
    traffic_th = repo.get_alert_traffic_mb_threshold(session)
    reasons: set[str] = set()
    if sub.status in ("expired",) or is_expire_passed(sub.expiration_date):
        reasons.add("expired")
    if sub.status == "volume_exhausted":
        reasons.add("traffic")
    if "expired" not in reasons and expire_within_days(sub.expiration_date, days_th):
        reasons.add("days")
    if (
        sub.remaining_traffic_mb is not None
        and sub.remaining_traffic_mb <= traffic_th
    ):
        reasons.add("traffic")
    return frozenset(reasons)


def _warning_icon(reasons: frozenset[str] | set[str], sub=None) -> str:
    traf = getattr(sub, "remaining_traffic_mb", None) if sub is not None else None
    status = getattr(sub, "status", None) if sub is not None else None
    if status == "volume_exhausted" or (traf is not None and traf <= 0):
        return "🚫"
    if status == "expired" or "expired" in reasons:
        return "🔚"
    has_days = "days" in reasons
    has_traffic = "traffic" in reasons
    if has_days and has_traffic:
        return "📅📶"
    if has_days:
        return "📅"
    if has_traffic:
        return "📶"
    return "⚠️"


def _warning_button_text(
    sub, reasons: frozenset[str] | set[str], *, vendor_icon: str = "✨"
) -> str:
    """LTR: name | remaining | traffic — English compact values for admin lists."""
    from app.utils.text import format_remaining_compact_en, format_traffic_short_en

    icon = _warning_icon(reasons, sub)
    name = (sub.username or "?").strip()
    if sub.status == "expired":
        remain = "0d"
    else:
        remain = format_remaining_compact_en(getattr(sub, "expiration_date", None))
    traf = format_traffic_short_en(sub.remaining_traffic_mb)
    label = f"{ltr(name)} | {remain} | {traf}"
    # Telegram button text soft limit ~64 chars
    if len(label) > 52:
        label = label[:49] + "…"
    return f"{vendor_icon}{icon} {label} {vendor_icon}"


def _vendor_for_sub(session, sub):
    vendor = None
    if getattr(sub, "vendor_id", None):
        vendor = repo.get_vendor(session, sub.vendor_id)
    if vendor is None:
        vendor = repo.resolve_vendor_for_username(session, sub.username)
    return vendor


def _vendor_display_name(session, sub) -> str:
    vendor = _vendor_for_sub(session, sub)
    if not vendor:
        return "—"
    return vendor.name or vendor.key or "—"


def _vendor_list_icon(session, sub) -> str:
    """Plan icon: ✨ economy · 💎 time-unlimited volume (both sides of username)."""
    name = (getattr(sub, "product_name", None) or "").strip()
    if "نامحدود" in name:
        return "💎"
    vendor = _vendor_for_sub(session, sub)
    key = (vendor.key if vendor else "") or ""
    if key == "finearts":
        return "💎"
    return "✨"


def _format_sub_detail(session, sub, *, reasons: frozenset[str] | set[str] | None = None) -> str:
    owner = _owner_line(session, sub)
    reason_set = reasons if reasons is not None else _alert_reasons_for_sub(session, sub)
    badges: list[str] = []
    if "expired" in reason_set or sub.status == "expired":
        badges.append("🔚 پایان زمان سرویس")
    if "days" in reason_set and sub.status != "expired":
        badges.append("📅 روز کم")
    if sub.status == "volume_exhausted" or (
        sub.remaining_traffic_mb is not None and sub.remaining_traffic_mb <= 0
    ):
        badges.append("🚫 پایان حجم")
    elif "traffic" in reason_set:
        badges.append("📶 حجم کم")
    badge_line = (" · ".join(badges) + "\n") if badges else ""
    vendor_name = _vendor_display_name(session, sub)
    from app.utils.text import format_expire_display

    expire_line = format_expire_display(getattr(sub, "expiration_date", None))
    return (
        f"{_vendor_list_icon(session, sub)} <b>{ltr(sub.username)}</b> {_vendor_list_icon(session, sub)}\n"
        f"{badge_line}"
        f"{owner}\n"
        f"🏪 فروشنده: <b>{vendor_name}</b>\n"
        f"📡 وضعیت: <b>{status_fa(sub.status)}</b>\n"
        f"{_vendor_list_icon(session, sub)} محصول: {sub.product_name or '—'}\n"
        f"📅 تاریخ اتمام : {expire_line}\n"
        f"📶 حجم باقی‌مانده: {format_mb(sub.remaining_traffic_mb)}"
    )


def ownership_menu() -> InlineKeyboardMarkup:
    from app.telegram.menu_config import build_admin_inline_menu

    return build_admin_inline_menu("ownership")


def _owner_line(session, sub) -> str:
    link = None
    if sub.id is not None:
        link = repo.get_link_for_subscription(session, sub.id)
    if link is None:
        link = repo.get_link_for_username(session, sub.username)
    if not link:
        return "👤 مالک محلی: — (بدون مالک)"
    return f"👤 مالک محلی: <code>{link.telegram_id}</code>"


@router.message(Command("services"))
@router.message(F.text.func(lambda t: bool(t) and t in admin_menu_labels("admin", "services")))
async def cmd_services(message: Message, settings: Settings) -> None:
    if not is_admin(message.from_user.id if message.from_user else None, settings):
        return
    uid = message.from_user.id if message.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_svc_q:{uid}", "")
    await _render_admin_services_page(
        message, page=0, edit=False, query="", admin_uid=uid
    )


@router.message(Command("search"))
async def cmd_search(message: Message, settings: Settings) -> None:
    if not is_admin(message.from_user.id if message.from_user else None, settings):
        return
    uid = message.from_user.id if message.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "svc_search")
    await message.answer(
        "🔍 <b>جستجوی سریع</b>\nبخشی از نام کاربری را بفرستید:",
        reply_markup=admin_reply_menu(),
    )


async def _render_admin_services_page(
    message: Message,
    *,
    page: int = 0,
    edit: bool = False,
    query: str | None = None,
    admin_uid: int | None = None,
) -> None:
    page_size = PAGE_SIZE
    uid = admin_uid
    if uid is None and message.from_user and not getattr(message.from_user, "is_bot", False):
        uid = message.from_user.id
    with get_session() as session:
        if query is None and uid:
            stored = (repo.get_state(session, f"admin_svc_q:{uid}") or "").strip()
            query = stored or None
        elif uid is not None:
            repo.set_state(session, f"admin_svc_q:{uid}", (query or "").strip())
        if uid is not None:
            repo.set_state(session, f"admin_svc_page:{uid}", str(max(0, page)))
            repo.set_state(session, f"admin_detail_from:{uid}", "services")
        rows, total = repo.list_inventory_subscriptions(
            session,
            query=query,
            usable_only=True,
            offset=page * page_size,
            limit=page_size,
        )
        # Resolve vendor icons while session is open
        service_buttons = [
            (sub.id, sub.username, _vendor_list_icon(session, sub)) for sub in rows
        ]
    if total == 0:
        text = (
            f"نتیجه‌ای برای «{query}» نیست."
            if query
            else "سرویسی در موجودی فعال نیست."
        )
        kb_rows = [
            [
                InlineKeyboardButton(
                    text="🔍 جستجوی سریع",
                    callback_data="admin:svcsearch",
                )
            ]
        ]
        if query:
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت به لیست",
                        callback_data="admin:svclist",
                    )
                ]
            )
        await _ui_show(
            message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), edit=edit
        )
        return

    pages = total_pages(total, page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    kb_rows: list[list[InlineKeyboardButton]] = []
    for sub_id, username, vendor_icon in service_buttons:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{vendor_icon} {ltr(username)} {vendor_icon}",
                    callback_data=f"subinfo:id:{sub_id}",
                )
            ]
        )
    nav = pagination_row(
        page=page, total=total, page_size=page_size, callback_prefix="admin:svcpages"
    )
    if nav:
        kb_rows.append(nav)
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔍 جستجوی سریع",
                callback_data="admin:svcsearch",
            )
        ]
    )
    if query:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت به لیست",
                    callback_data="admin:svclist",
                )
            ]
        )
    title = "✨ <b>سرویس‌های موجودی</b>"
    if query:
        title += f" — جستجو: <b>{query}</b>"
    text = (
        f"{title}\n"
        f"صفحه {page + 1}/{pages} · ({start + 1}–{start + len(service_buttons)} از {total})\n"
        "✨ پلن اقتصادی · 💎 پلن حجمی مدت نامحدود\n"
        "برای جزئیات روی سرویس بزنید:"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await _ui_show(message, text, reply_markup=markup, edit=edit)


@router.callback_query(F.data == "admin:svclist")
async def admin_svc_list(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "")
        repo.set_state(session, f"admin_svc_q:{uid}", "")
        repo.set_state(session, f"admin_svc_page:{uid}", "0")
    await _render_admin_services_page(
        callback.message, page=0, edit=True, query="", admin_uid=uid
    )


@router.callback_query(F.data == "admin:svcback")
async def admin_svc_back(callback: CallbackQuery, settings: Settings) -> None:
    """Return to services list keeping current search + page."""
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        raw_page = (repo.get_state(session, f"admin_svc_page:{uid}") or "0").strip()
        try:
            page = max(0, int(raw_page))
        except ValueError:
            page = 0
    await _render_admin_services_page(
        callback.message, page=page, edit=True, admin_uid=uid
    )


@router.callback_query(F.data.startswith("admin:svcpages:"))
async def admin_svc_pages(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    page = int((callback.data or "").rsplit(":", 1)[1])
    await _render_admin_services_page(
        callback.message,
        page=max(0, page),
        edit=True,
        admin_uid=uid,
    )


@router.callback_query(F.data == "admin:svcsearch")
async def admin_svc_search(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "svc_search")
    await _ui_show(
        callback.message,
        "🔍 <b>جستجوی سریع</b>\nبخشی از نام کاربری را بفرستید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت",
                        callback_data="admin:svcback",
                    )
                ]
            ]
        ),
        edit=True,
    )


@router.message(CommandStart())
@router.message(F.text.in_({BTN_START, "شروع"}))
async def cmd_start(message: Message, settings: Settings) -> None:
    if not is_admin(message.from_user.id if message.from_user else None, settings):
        await message.answer("دسترسی ندارید.")
        return
    uid = message.from_user.id if message.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "")
        repo.set_state(session, f"admin_create_user:{uid}", "")
        repo.set_state(session, f"admin_create_owner:{uid}", "")
        welcome = repo.get_bot_message(session, "admin.welcome")
    await message.answer(welcome, reply_markup=admin_reply_menu())


@router.message(Command("menu"))
async def cmd_menu(message: Message, settings: Settings) -> None:
    if not is_admin(message.from_user.id if message.from_user else None, settings):
        return
    await message.answer("منوی پایین فعال شد.", reply_markup=admin_reply_menu())


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    with get_session() as session:
        counts = repo.count_by_status(session)
        active_jobs = repo.list_active_renew_jobs(session, limit=5)
    lines = ["📊 <b>وضعیت اشتراک‌ها</b>"]
    labels = {
        "active": "فعال",
        "warning": "هشدار",
        "expired": "منقضی",
        "deleted": "حذف‌شده",
        "unknown": "نامشخص",
    }
    if not counts:
        lines.append("هنوز اشتراکی در دیتابیس ثبت نشده.")
    else:
        for key, label in labels.items():
            if key in counts:
                lines.append(f"{label}: {counts[key]}")
        lines.append(f"جمع: {sum(counts.values())}")
    if active_jobs:
        lines.append("")
        lines.append(f"سفارش فعال در صف: {len(active_jobs)}")
    await callback.message.edit_text("\n".join(lines), reply_markup=main_menu())
    await callback.answer()


async def _render_warnings_list(
    message: Message, *, edit: bool, admin_uid: int, page: int = 0
) -> None:
    """Vendor warnings: only rows with vendor_alert_at from real vendor alerts."""
    page_size = PAGE_SIZE
    with get_session() as session:
        rows, total = repo.list_warning_subscriptions(
            session, offset=page * page_size, limit=page_size
        )
        days_th = repo.get_alert_days_threshold(session)
        traffic_th = repo.get_alert_traffic_mb_threshold(session)
        repo.set_state(session, f"admin_detail_from:{admin_uid}", "warnings")
        repo.set_state(session, f"admin_warn_page:{admin_uid}", str(max(0, page)))
        items = [
            (sub, _alert_reasons_for_sub(session, sub), _vendor_list_icon(session, sub))
            for sub in rows
        ]
    if total == 0:
        await _ui_show(
            message,
            "⚠️ <b>هشدار فروشنده</b>\n"
            "موردی از هشدار واقعی ربات فروشنده نیست.\n\n"
            "💡 نزدیک اتمام محلی جداست — از «🪫 نزدیک اتمام» ببینید.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔍 بررسی تمدیدشده‌ها",
                            callback_data="warn:checkrenewed",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🪫 نزدیک اتمام (محلی)",
                            callback_data="menu:local_alerts",
                        )
                    ],
                    [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu:home")],
                ]
            ),
            edit=edit,
        )
        return
    pages = total_pages(total, page_size)
    page = max(0, min(page, pages - 1))
    kb_rows: list[list[InlineKeyboardButton]] = []
    for sub, reasons, vendor_icon in items:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=_warning_button_text(
                        sub, reasons, vendor_icon=vendor_icon
                    ),
                    callback_data=f"subinfo:id:{sub.id}",
                )
            ]
        )
    nav = pagination_row(
        page=page, total=total, page_size=page_size, callback_prefix="warn:pages"
    )
    if nav:
        kb_rows.append(nav)
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔍 بررسی تمدیدشده‌ها",
                callback_data="warn:checkrenewed",
            )
        ]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🙈 پنهان همه",
                callback_data="warn:hideall",
            ),
            InlineKeyboardButton(
                text="👁 نمایش مجدد همه",
                callback_data="warn:unhideall",
            ),
        ]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🪫 نزدیک اتمام (محلی)",
                callback_data="menu:local_alerts",
            )
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu:home")]
    )
    await _ui_show(
        message,
        "⚠️ <b>هشدار فروشنده</b>\n"
        "فقط هشدار واقعی ربات فروشنده (مستقل از نزدیک اتمام محلی).\n"
        f"آستانه‌ها (نمایش): 📅 ≤{days_th} روز · 📶 ≤{format_mb(traffic_th)}\n"
        f"صفحه {page + 1}/{pages} · جمع {total}\n"
        "📅 روز کم · 📶 حجم کم · ⌛ منقضی\n"
        "اعداد فعلی دیتابیس‌اند؛ با باز کردن جزئیات/بروزرسانی تازه می‌شوند.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        edit=edit,
    )


async def _render_local_alerts_list(
    message: Message, *, edit: bool, admin_uid: int, page: int = 0
) -> None:
    """Our own almost-out list from stored days/traffic vs thresholds."""
    page_size = PAGE_SIZE
    with get_session() as session:
        items, total = repo.list_local_almost_out(
            session, offset=page * page_size, limit=page_size
        )
        days_th = repo.get_alert_days_threshold(session)
        traffic_th = repo.get_alert_traffic_mb_threshold(session)
        repo.set_state(session, f"admin_detail_from:{admin_uid}", "local_alerts")
        repo.set_state(session, f"admin_local_page:{admin_uid}", str(max(0, page)))
        almost_buttons = [
            (sub, reasons, _vendor_list_icon(session, sub)) for sub, reasons in items
        ]
    if total == 0:
        await _ui_show(
            message,
            "🪫 <b>نزدیک اتمام (محلی)</b>\n"
            "سرویسی زیر آستانهٔ محلی نیست.\n\n"
            f"آستانه‌ها: 📅 ≤{days_th} روز · 📶 ≤{format_mb(traffic_th)}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔍 بررسی همه سرویس‌ها",
                            callback_data="local:checkall",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔍 حذف تمدیدشده‌ها",
                            callback_data="warn:checkrenewed",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⚠️ هشدار فروشنده",
                            callback_data="menu:warnings",
                        )
                    ],
                    [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu:home")],
                ]
            ),
            edit=edit,
        )
        return
    pages = total_pages(total, page_size)
    page = max(0, min(page, pages - 1))
    kb_rows: list[list[InlineKeyboardButton]] = []
    for sub, reasons, vendor_icon in almost_buttons:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=_warning_button_text(
                        sub, reasons, vendor_icon=vendor_icon
                    ),
                    callback_data=f"subinfo:id:{sub.id}",
                )
            ]
        )
    nav = pagination_row(
        page=page, total=total, page_size=page_size, callback_prefix="local:pages"
    )
    if nav:
        kb_rows.append(nav)
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔍 بررسی همه سرویس‌ها",
                callback_data="local:checkall",
            )
        ]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔍 حذف تمدیدشده‌ها",
                callback_data="warn:checkrenewed",
            )
        ]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔄 بروزرسانی این صفحه از لینک",
                callback_data=f"local:refresh:{page}",
            )
        ]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🙈 پنهان همه",
                callback_data="local:hideall",
            ),
            InlineKeyboardButton(
                text="👁 نمایش مجدد همه",
                callback_data="local:unhideall",
            ),
        ]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="⚠️ هشدار فروشنده",
                callback_data="menu:warnings",
            )
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu:home")]
    )
    n_days = sum(1 for _, r, __ in almost_buttons if "days" in r)
    n_traffic = sum(1 for _, r, __ in almost_buttons if "traffic" in r)
    n_empty = sum(
        1
        for s, _, __ in almost_buttons
        if s.status == "volume_exhausted"
        or (s.remaining_traffic_mb is not None and s.remaining_traffic_mb <= 0)
    )
    await _ui_show(
        message,
        "🪫 <b>نزدیک اتمام (محلی)</b>\n"
        "قالب دکمه (چپ→راست): <code>name | days | traffic</code>\n"
        f"آستانه‌ها: 📅 ≤{days_th} روز · 📶 ≤{format_mb(traffic_th)}\n"
        f"صفحه {page + 1}/{pages} · این صفحه: 📅 {n_days} · 📶 {n_traffic} · "
        f"🚫 {n_empty} · جمع {total}\n"
        "📅 روز کم · 📶 حجم کم · 🚫 پایان حجم · 🔚 پایان زمان",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        edit=edit,
    )


@router.callback_query(F.data == "menu:warnings")
async def cb_warnings(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    await _render_warnings_list(callback.message, edit=True, admin_uid=uid, page=0)


@router.callback_query(F.data.startswith("warn:pages:"))
async def cb_warnings_pages(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    try:
        page = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        page = 0
    await _render_warnings_list(
        callback.message, edit=True, admin_uid=uid, page=max(0, page)
    )


@router.callback_query(F.data == "menu:local_alerts")
async def cb_local_alerts(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    await _render_local_alerts_list(callback.message, edit=True, admin_uid=uid, page=0)


@router.callback_query(F.data.startswith("local:pages:"))
async def cb_local_pages(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    try:
        page = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        page = 0
    await _render_local_alerts_list(
        callback.message, edit=True, admin_uid=uid, page=max(0, page)
    )


@router.callback_query(F.data.startswith("warn:hide:"))
async def cb_warn_hide_one(callback: CallbackQuery, settings: Settings) -> None:
    """Hide one vendor-warning row (admin dismiss only)."""
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    try:
        sub_id = int(parts[2])
    except (ValueError, IndexError):
        await safe_answer(callback, "نامعتبر", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.hide_vendor_warning_subscription(session, sub_id)
        raw = (repo.get_state(session, f"admin_warn_page:{uid}") or "0").strip()
        try:
            page = max(0, int(raw))
        except ValueError:
            page = 0
    await safe_answer(callback, "پنهان شد")
    await _render_warnings_list(
        callback.message, edit=True, admin_uid=uid, page=page
    )


@router.callback_query(F.data == "warn:checkrenewed")
async def cb_warn_check_renewed(callback: CallbackQuery, settings: Settings) -> None:
    """Manually sync renew stamps + drop vendor warnings superseded by renew/create."""
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        stats = repo.clear_recently_renewed_from_alert_lists(session)
    n = int(stats.get("vendor_cleared") or 0)
    recent = int(stats.get("recent_renewed") or 0)
    await safe_answer(
        callback,
        (
            f"{n} هشدار حذف شد · {recent} تمدید ≤{stats.get('grace_days', 3)}روز"
            if n or recent
            else "مورد تمدیدشده‌ای برای حذف نبود"
        ),
        show_alert=True,
    )
    # Refresh whichever list the admin was viewing.
    with get_session() as session:
        from_list = (
            repo.get_state(session, f"admin_detail_from:{uid}") or "warnings"
        ).strip()
    if from_list == "local_alerts":
        await _render_local_alerts_list(
            callback.message, edit=True, admin_uid=uid, page=0
        )
    else:
        await _render_warnings_list(
            callback.message, edit=True, admin_uid=uid, page=0
        )


@router.callback_query(F.data == "warn:hideall")
async def cb_warn_hide_all(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        n = repo.hide_all_vendor_warnings_visible(session)
    await safe_answer(callback, f"{n} مورد پنهان شد", show_alert=True)
    await _render_warnings_list(callback.message, edit=True, admin_uid=uid, page=0)


@router.callback_query(F.data == "warn:unhideall")
async def cb_warn_unhide_all(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.clear_vendor_warning_hidden(session)
    await safe_answer(callback, "همه دوباره نمایش داده می‌شوند", show_alert=True)
    await _render_warnings_list(callback.message, edit=True, admin_uid=uid, page=0)


@router.callback_query(F.data.startswith("local:hide:"))
async def cb_local_hide_one(callback: CallbackQuery, settings: Settings) -> None:
    """Hide one almost-out row (admin dismiss only; NetPlus delete still removes later)."""
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    try:
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else None
    except (ValueError, IndexError):
        await safe_answer(callback, "نامعتبر", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.hide_almost_out_subscription(session, sub_id)
        if page is None:
            raw = (repo.get_state(session, f"admin_local_page:{uid}") or "0").strip()
            try:
                page = max(0, int(raw))
            except ValueError:
                page = 0
    await safe_answer(callback, "پنهان شد")
    await _render_local_alerts_list(
        callback.message, edit=True, admin_uid=uid, page=max(0, int(page or 0))
    )


@router.callback_query(F.data == "local:hideall")
async def cb_local_hide_all(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        n = repo.hide_all_almost_out_visible(session)
    await safe_answer(callback, f"{n} مورد پنهان شد", show_alert=True)
    await _render_local_alerts_list(callback.message, edit=True, admin_uid=uid, page=0)


@router.callback_query(F.data == "local:unhideall")
async def cb_local_unhide_all(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.clear_almost_out_hidden(session)
    await safe_answer(callback, "همه دوباره نمایش داده می‌شوند", show_alert=True)
    await _render_local_alerts_list(callback.message, edit=True, admin_uid=uid, page=0)


@router.callback_query(F.data == "local:checkall")
async def cb_local_check_all(callback: CallbackQuery, settings: Settings) -> None:
    """Refresh userinfo for every usable service and rebuild almost-out list."""
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    await safe_answer(callback, "در حال بررسی همه سرویس‌ها…")
    uid = callback.from_user.id if callback.from_user else 0
    from app.services.lifecycle_alerts import check_all_services_for_almost_out

    try:
        stats = check_all_services_for_almost_out(force=True)
    except Exception:
        log.exception("admin local check-all failed")
        await callback.message.answer("❌ بررسی همه ناموفق شد؛ دوباره تلاش کنید.")
        return
    with get_session() as session:
        raw = (repo.get_state(session, f"admin_local_page:{uid}") or "0").strip()
        try:
            page = max(0, int(raw))
        except ValueError:
            page = 0
    await _render_local_alerts_list(
        callback.message, edit=True, admin_uid=uid, page=page
    )
    await callback.message.answer(
        "🔍 بررسی همه تمام شد\n"
        f"بررسی‌شده: <b>{stats['checked']}</b>\n"
        f"نزدیک اتمام: <b>{stats['almost_out']}</b>\n"
        f"خطا: <b>{stats['failed']}</b>"
    )


@router.callback_query(F.data.startswith("local:refresh:"))
async def cb_local_refresh_page(callback: CallbackQuery, settings: Settings) -> None:
    """Force HTTP userinfo refresh for every service on the current almost-out page."""
    if not is_admin(callback.from_user.id, settings):
        await safe_answer(callback, "دسترسی ندارید", show_alert=True)
        return
    await safe_answer(callback, "در حال بروزرسانی از لینک…")
    uid = callback.from_user.id if callback.from_user else 0
    try:
        page = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        page = 0
    page_size = PAGE_SIZE
    from app.services.subscription_refresh import refresh_subscription_userinfo

    with get_session() as session:
        items, _total = repo.list_local_almost_out(
            session, offset=page * page_size, limit=page_size
        )
        for sub, _reasons in items:
            try:
                refresh_subscription_userinfo(session, subscription=sub, force=True)
            except Exception:
                pass
    await _render_local_alerts_list(
        callback.message, edit=True, admin_uid=uid, page=max(0, page)
    )


@router.callback_query(F.data == "menu:search")
async def cb_search(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "svc_search")
    await _ui_show(
        callback.message,
        "🔍 <b>جستجوی سریع</b>\nنام کاربری یا بخشی از آن را بفرستید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="admin:svcback")],
                [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu:home")],
            ]
        ),
        edit=True,
    )
    await callback.answer()


@router.callback_query(F.data == "menu:create")
async def cb_create_start(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "create_wait_username")
    await _ui_show(
        callback.message,
        "🛒 <b>خرید جدید</b>\nنام کاربری سرویس را بفرستید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚫 انصراف", callback_data="menu:home")]
            ]
        ),
        edit=True,
    )
    await callback.answer()


@router.callback_query(F.data == "menu:home")
async def cb_home(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{callback.from_user.id}", "")
        repo.set_state(session, f"admin_create_user:{callback.from_user.id}", "")
        repo.set_state(session, f"admin_create_owner:{callback.from_user.id}", "")
    await callback.message.edit_text("🏠 منوی اصلی:", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("sync:run:"))
async def cb_sync_run(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    key = (callback.data or "").rsplit(":", 1)[-1].strip().lower()
    if key not in {"netplus", "finearts"}:
        await safe_answer(callback, "فروشنده نامعتبر", show_alert=True)
        return
    queue = renew_queue
    if queue is None or not getattr(queue, "driver", None):
        await safe_answer(callback, "صف/درایور آماده نیست", show_alert=True)
        return
    if getattr(queue, "_sync_running", False):
        await safe_answer(callback, "همگام‌سازی الان در حال اجراست", show_alert=True)
        return
    await safe_answer(callback)
    label = "FineArts" if key == "finearts" else "NetPlus"
    bot_uname = (
        settings.finearts_bot if key == "finearts" else settings.netplus_bot
    )
    await _ui_show(
        callback.message,
        f"🔄 همگام‌سازی افزایشی <b>{label}</b> شروع شد.\n"
        "لیست زنده با دیتابیس مقایسه می‌شود "
        "(جدیدها اضافه، حذف‌شده‌ها برداشته می‌شوند).\n"
        "صف سفارش‌های جدید موقتاً متوقف می‌شود.",
        edit=True,
    )

    async def _run_sync() -> None:
        import logging

        from app.services.inventory_incremental_sync import (
            run_manual_incremental_sync,
        )
        from app.telegram import customer_bridge

        try:
            report = await run_manual_incremental_sync(
                renew_queue=queue,
                vendor_key=key,
                vendor_name=label,
                bot_username=bot_uname,
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("%s incremental sync failed", label)
            await callback.message.answer(
                f"❌ همگام‌سازی {label} ناموفق:\n<code>{exc}</code>"
            )
            return

        from app.monitoring.notifications import deleted_service_customer_keyboard

        bot = customer_bridge.customer_bot
        notified = 0
        if bot and report.customer_notices:
            kb = deleted_service_customer_keyboard()
            for notice in report.customer_notices:
                try:
                    await bot.send_message(
                        notice["telegram_id"],
                        notice["text"],
                        reply_markup=kb,
                    )
                    notified += 1
                except Exception:
                    logging.getLogger(__name__).exception(
                        "sync delete notify failed for %s",
                        notice.get("username"),
                    )

        with get_session() as session:
            total = repo.count_by_status(session)
        note = ""
        if report.removals_skipped_reason:
            note = (
                f"\n⚠️ حذف از دیتابیس انجام نشد "
                f"({report.removals_skipped_reason})."
            )
        if report.hist_n and not report.live_n:
            note += (
                "\n⚠️ لیست زنده باز نشد؛ از تاریخچه استفاده شد "
                "(فقط افزودن/بروزرسانی)."
            )
        elif report.hist_n:
            note += f"\n(زنده: {report.live_n} · تاریخچه: {report.hist_n})"
        await callback.message.answer(
            f"✅ همگام‌سازی <b>{label}</b> تمام شد.\n"
            f"کشف‌شده: <b>{report.discovered}</b>\n"
            f"جدید: <b>{report.added}</b> · بروزرسانی: <b>{report.updated}</b> · "
            f"حذف محلی: <b>{report.removed}</b>\n"
            f"اطلاع به مشتری: <b>{notified}</b>\n"
            f"جمع در دیتابیس: {sum(total.values())} — {total}"
            f"{note}",
            reply_markup=admin_reply_menu(),
        )

    import asyncio

    asyncio.create_task(_run_sync())


_STRATEGY_LABELS_FA = {
    "automatic": "خودکار (لینک اگر پشتیبانی شود)",
    "subscription_url": "از لینک اشتراک (HTTP)",
    "provider_bot": "فقط ربات فروشنده (بدون HTTP)",
}


def _strategy_fa(value: str | None) -> str:
    key = (value or "automatic").strip().lower()
    return _STRATEGY_LABELS_FA.get(key, key)


@router.callback_query(F.data == "menu:vendor")
async def cb_vendor(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    from app.vendors.registry import list_registered_vendor_keys, get_vendor_capabilities

    with get_session() as session:
        db_vendors = {v.key: v for v in repo.list_vendors(session)}

    lines = ["🏪 <b>فروشندگان ثبت‌شده</b>\n"]
    for key in list_registered_vendor_keys() or ["netplus"]:
        caps = get_vendor_capabilities(key)
        name = caps.display_name if caps else key
        row = db_vendors.get(key)
        strat = _strategy_fa(row.info_refresh_strategy if row else None)
        if key == "netplus":
            bot = settings.netplus_bot
            extra = "شنونده پیام خصوصی روشن"
        elif key == "finearts":
            bot = settings.finearts_bot
            extra = (
                "شنونده پیام خصوصی روشن · موجودی + تمدید/خرید "
                "(پرداخت TetheRial کارت+مبلغ یکتا)"
            )
        else:
            bot = "—"
            extra = ""
        lines.append(
            f"• <b>{name}</b> ({key})\n"
            f"  یوزرنیم: {bot}\n"
            f"  بروزرسانی: <code>{strat}</code>\n"
            f"  {extra}"
        )
    lines.append("\nیوزرنیم اشتراک‌ها سراسری است (یک نام زنده در کل فروشگاه).")
    await _ui_show(
        callback.message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 تغییر استراتژی بروزرسانی",
                        callback_data="settings:refresh_strategy",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
        edit=True,
    )


async def _refresh_strategy_screen(message: Message, *, edit: bool = False) -> None:
    with get_session() as session:
        vendors = repo.list_vendors(session)
        rows_data = [
            (
                v.key,
                v.name,
                (v.info_refresh_strategy or "automatic").strip().lower(),
            )
            for v in vendors
        ]

    lines = [
        "🔄 <b>استراتژی بروزرسانی سرویس</b>\n",
        "تعیین می‌کند حجم/انقضا از کجا خوانده شود:",
        "• <code>subscription_url</code> — از لینک اشتراک (HTTP) ✅ پیشنهادی",
        "• <code>automatic</code> — مثل لینک، اگر فروشنده پشتیبانی کند",
        "• <code>provider_bot</code> — فقط با رفتن داخل ربات فروشنده "
        "(HTTP خاموش؛ باعث null ماندن داده می‌شود)\n",
    ]
    kb_rows: list[list[InlineKeyboardButton]] = []
    if not rows_data:
        lines.append("هنوز فروشنده‌ای در دیتابیس نیست.")
    for key, name, strat in rows_data:
        lines.append(
            f"• <b>{name}</b> (<code>{key}</code>)\n"
            f"  فعلی: <code>{strat}</code> — {_strategy_fa(strat)}"
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'✅' if strat == 'subscription_url' else '○'} لینک {key}",
                    callback_data=f"vendor:strategy:{key}:subscription_url",
                )
            ]
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'✅' if strat == 'automatic' else '○'} خودکار {key}",
                    callback_data=f"vendor:strategy:{key}:automatic",
                ),
                InlineKeyboardButton(
                    text=f"{'✅' if strat == 'provider_bot' else '○'} ربات {key}",
                    callback_data=f"vendor:strategy:{key}:provider_bot",
                ),
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")]
    )
    await _ui_show(
        message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        edit=edit,
    )


@router.callback_query(F.data == "settings:refresh_strategy")
async def cb_settings_refresh_strategy(
    callback: CallbackQuery, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    await _refresh_strategy_screen(callback.message, edit=True)


@router.callback_query(F.data.startswith("vendor:strategy:"))
async def cb_vendor_strategy_set(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    parts = (callback.data or "").split(":")
    # vendor:strategy:{key}:{strategy}
    if len(parts) != 4:
        await callback.answer("نامعتبر", show_alert=True)
        return
    _, _, vendor_key, strategy = parts
    try:
        with get_session() as session:
            row = repo.set_vendor_info_refresh_strategy(session, vendor_key, strategy)
            if not row:
                await callback.answer("فروشنده پیدا نشد", show_alert=True)
                return
            new_val = row.info_refresh_strategy
    except ValueError:
        await callback.answer("استراتژی نامعتبر", show_alert=True)
        return
    await callback.answer(f"ذخیره شد: {new_val}")
    await _refresh_strategy_screen(callback.message, edit=True)


@router.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    await _ui_show(
        callback.message,
        "هشدارهای NetPlus اینجا می‌آیند.\n"
        "تمدید و خرید جدید را به صف یک‌به‌یک بفرستید؛ مبلغ یکتا را پرداخت کنید.\n"
        "اگر تایید خودکار نیامد، دکمه رسید را بزنید و عکس رسید را در همین ربات بفرستید.",
        reply_markup=main_menu(),
        edit=True,
    )


@router.callback_query(F.data == "menu:prices")
async def cb_prices(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    await _send_prices(callback.message, edit=True)


@router.callback_query(F.data == "menu:card")
async def cb_card(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    await _send_card_info(callback.message, settings, edit=True)


@router.callback_query(F.data == "menu:pricing_text")
async def cb_pricing_text(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    with get_session() as session:
        current = repo.get_pricing_text(session)
        mn, mx = repo.get_wallet_limits(session)
    await _ui_show(
        callback.message,
        "📝 <b>متن تعرفه در ربات مشتری</b>\n\n"
        f"{current}\n\n"
        f"حد شارژ کیف پول: <code>{mn:,}</code> … <code>{mx:,}</code> ت\n\n"
        "دکمه‌ها:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ ویرایش متن تعرفه",
                        callback_data="pricing_text:edit",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💰 حد شارژ کیف پول",
                        callback_data="wallet_limits:edit",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
        edit=True,
    )


@router.callback_query(F.data == "pricing_text:edit")
async def cb_pricing_text_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "pricing_text_edit")
    await callback.message.answer(
        "متن کامل تعرفه را بفرستید (HTML ساده مجاز است).\n"
        "همین متن در ربات مشتری نشان داده می‌شود."
    )
    await callback.answer()


@router.callback_query(F.data == "wallet_limits:edit")
async def cb_wallet_limits_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "wallet_limits_edit")
    await callback.message.answer(
        "حداقل و حداکثر شارژ را به تومان بفرستید.\n"
        "مثال: <code>10000 600000</code>"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("support:reply:"))
async def cb_support_reply(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    cust_id = int((callback.data or "").rsplit(":", 1)[1])
    uid = callback.from_user.id
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", f"support_reply:{cust_id}")
    await callback.message.answer(
        f"پاسخ برای مشتری <code>{cust_id}</code> را بفرستید:"
    )
    await callback.answer()


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await callback.message.edit_text(
        "⚙️ <b>تنظیمات</b>\n"
        "یک گروه را انتخاب کنید — جزئیات داخل همان گروه است.",
        reply_markup=settings_menu(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:hub:"))
async def cb_settings_hub(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    hub = (callback.data or "").rsplit(":", 1)[-1]
    from app.telegram.menu_config import build_admin_inline_menu

    titles = {
        "shop": ("🛒 <b>فروش و قیمت‌ها</b>", "settings_shop"),
        "pay": ("💳 <b>پرداخت و تایید</b>", "settings_pay"),
        "customer": ("👥 <b>مشتری و کانال</b>", "settings_customer"),
        "advanced": ("🛠 <b>پیشرفته</b>", "settings_advanced"),
    }
    title, menu_id = titles.get(hub, ("⚙️ تنظیمات", "settings"))
    await callback.message.edit_text(title, reply_markup=build_admin_inline_menu(menu_id))
    await callback.answer()


@router.callback_query(F.data == "menu:requests")
async def cb_requests(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await callback.message.edit_text(
        "📥 <b>درخواست‌های مشتری</b>\n"
        "معمولاً فقط «نیاز به تایید شما» را باز کنید.\n"
        "رسیدها و پرداخت‌های در انتظار همان‌جا به‌صورت پیام جدید هم می‌آیند.",
        reply_markup=requests_menu(),
    )
    await callback.answer()


def _order_kind_fa(kind: str) -> str:
    return {
        "wallet": "💰 کیف پول",
        "create": "🛒 خرید",
        "renew": "♻️ تمدید",
    }.get(kind, kind)


_ORDER_STATUS_FA = {
    "awaiting_payment": "⏳ منتظر پرداخت",
    "receipt_window": "📤 منتظر رسید",
    "awaiting_review": "🔎 در حال بررسی",
    "approved": "✅ تایید شده",
    "rejected": "❌ رد شده",
    "fulfilling": "⚙️ در حال فعال‌سازی",
    "done": "✅ انجام شده",
    "cancelled": "🚫 لغو شده",
    "expired": "⏰ منقضی",
}


def _order_status_fa(status: str | None) -> str:
    key = (status or "").strip()
    return _ORDER_STATUS_FA.get(key, status or "—")


async def _render_requests_bucket(
    message: Message,
    *,
    bucket: str,
    page: int = 0,
    edit: bool = True,
) -> None:
    page_size = PAGE_SIZE
    if bucket == "awaiting_customer":
        with get_session() as session:
            tickets = repo.list_support_tickets(
                session, statuses=("awaiting_customer", "awaiting_admin", "open")
            )
        total = len(tickets)
        if total == 0:
            await _ui_show(
                message,
                "تیکت بازی نیست.",
                reply_markup=requests_menu(),
                edit=edit,
            )
            return
        pages = total_pages(total, page_size)
        page = max(0, min(page, pages - 1))
        chunk = tickets[page * page_size : page * page_size + page_size]
        lines = [f"💬 <b>تیکت‌ها</b> — صفحه {page + 1}/{pages}"]
        kb_rows: list[list[InlineKeyboardButton]] = []
        for t in chunk:
            lines.append(
                f"🎫 #{t.id} — {t.kind} — {t.status} · "
                f"<code>{t.telegram_id}</code>"
            )
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"↩️ پاسخ #{t.id}",
                        callback_data=f"ticket:reply:{t.id}",
                    )
                ]
            )
        nav = pagination_row(
            page=page,
            total=total,
            page_size=page_size,
            callback_prefix=f"requests:page:{bucket}",
        )
        if nav:
            kb_rows.append(nav)
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="🧹 پاک‌کردن این لیست",
                    callback_data=f"requests:clear:{bucket}",
                )
            ]
        )
        kb_rows.append(
            [InlineKeyboardButton(text="⬅️ درخواست‌ها", callback_data="menu:requests")]
        )
        await _ui_show(
            message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            edit=edit,
        )
        return

    status_map = {
        "awaiting_payment": ("awaiting_payment", "receipt_window"),
        "awaiting_review": ("awaiting_review",),
        "done": ("approved", "fulfilling", "done"),
        "rejected": ("rejected",),
        "expired": ("expired",),
    }
    statuses = status_map.get(bucket, (bucket,))
    with get_session() as session:
        # Fetch enough for pagination; repo limit caps at a generous window.
        orders = repo.list_customer_orders_by_statuses(
            session, statuses, limit=200
        )
    total = len(orders)
    if total == 0:
        await _ui_show(
            message, "موردی نیست.", reply_markup=requests_menu(), edit=edit
        )
        return
    pages = total_pages(total, page_size)
    page = max(0, min(page, pages - 1))
    chunk = orders[page * page_size : page * page_size + page_size]
    lines = [f"📥 <b>درخواست‌ها</b> — صفحه {page + 1}/{pages} از {total}"]
    kb_rows = []
    for o in chunk:
        kind_fa = _order_kind_fa(o.kind)
        lines.append(
            f"📦 #{o.id} {kind_fa} · {_order_status_fa(o.status)}\n"
            f"   {ltr_html(o.service_username)} · {o.price_toman:,} ت · "
            f"<code>{o.telegram_id}</code>"
        )
        st = o.status
        # One clear primary action: approve = mark paid + start vendor queue.
        if st in ("awaiting_payment", "receipt_window", "awaiting_review"):
            ok_label = f"✅ تایید و شروع #{o.id}"
        else:
            ok_label = f"✅ تایید #{o.id}"
        row = [
            InlineKeyboardButton(
                text=ok_label,
                callback_data=f"corder:ok:{o.id}",
            ),
            InlineKeyboardButton(
                text=f"🚫 رد #{o.id}",
                callback_data=f"corder:no:{o.id}",
            ),
        ]
        kb_rows.append(row)
        if st in ("awaiting_payment", "receipt_window", "awaiting_review") and (
            repo.order_is_vendor_direct(o)
        ):
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📤 رسید → فروشنده #{o.id}",
                        callback_data=f"corder:vendor_fwd:{o.id}",
                    )
                ]
            )
        if st in ("fulfilling", "approved") or (
            st == "done" and o.renew_job_id
        ):
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f" دستی تمام شد #{o.id}",
                        callback_data=f"corder:manual_done:{o.id}",
                    )
                ]
            )
    nav = pagination_row(
        page=page,
        total=total,
        page_size=page_size,
        callback_prefix=f"requests:page:{bucket}",
    )
    if nav:
        kb_rows.append(nav)
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🧹 پاک‌کردن این لیست",
                callback_data=f"requests:clear:{bucket}",
            )
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ درخواست‌ها", callback_data="menu:requests")]
    )
    shown = await _ui_show(
        message,
        "\n".join(lines)
        + "\n\n"
        "<i>✅ تایید و شروع = پرداخت OK + صف خرید/تمدید فروشنده\n"
        "🚫 رد = برگشت به مشتری با دلیل\n"
        "دستی تمام شد = فقط وقتی خودتان روی فروشنده کار را تمام کردید</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        edit=edit,
    )
    # Track this list message for every open order so panel approve can strip buttons.
    if shown is not None and getattr(shown, "message_id", None) and chunk:
        chat_id = shown.chat.id if shown.chat else (
            message.chat.id if message and message.chat else None
        )
        mid = int(shown.message_id)
        if chat_id is not None:
            with get_session() as session:
                for o in chunk:
                    if o.status in (
                        "awaiting_payment",
                        "receipt_window",
                        "awaiting_review",
                        "approved",
                        "fulfilling",
                    ):
                        repo.track_admin_order_ui_message(
                            session,
                            int(o.id),
                            int(chat_id),
                            mid,
                            delete_on_done=False,
                        )


@router.callback_query(F.data.startswith("requests:clear:"))
async def cb_requests_clear(callback: CallbackQuery, settings: Settings) -> None:
    """Hide bucket items from admin lists (customer orders/tickets stay intact)."""
    if not is_admin(callback.from_user.id, settings):
        return
    target = (callback.data or "").rsplit(":", 1)[1]
    status_map = {
        "awaiting_payment": ("awaiting_payment", "receipt_window"),
        "awaiting_review": ("awaiting_review",),
        "done": ("approved", "fulfilling", "done"),
        "rejected": ("rejected",),
        "expired": ("expired",),
    }
    with get_session() as session:
        if target == "all":
            n_orders = repo.hide_admin_orders(session)
            n_tickets = repo.hide_admin_tickets(session)
            await safe_answer(
                callback,
                f"پاک شد: {n_orders} سفارش · {n_tickets} تیکت",
                show_alert=True,
            )
            await _ui_show(
                callback.message,
                "🧹 همه لیست‌های درخواست از دید ادمین پاک شدند.",
                reply_markup=requests_menu(),
                edit=True,
            )
            return
        if target == "awaiting_customer":
            n = repo.hide_admin_tickets(
                session, statuses=("awaiting_customer", "awaiting_admin", "open")
            )
            await safe_answer(callback, f"{n} تیکت پنهان شد", show_alert=True)
            await _render_requests_bucket(
                callback.message, bucket=target, page=0, edit=True
            )
            return
        statuses = status_map.get(target)
        if not statuses:
            await safe_answer(callback, "نامعتبر", show_alert=True)
            return
        n = repo.hide_admin_orders(session, statuses=statuses)
        await safe_answer(callback, f"{n} مورد پنهان شد", show_alert=True)
        await _render_requests_bucket(
            callback.message, bucket=target, page=0, edit=True
        )


@router.callback_query(F.data.startswith("requests:bucket:"))
async def cb_requests_bucket(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    bucket = (callback.data or "").rsplit(":", 1)[1]
    await _render_requests_bucket(
        callback.message, bucket=bucket, page=0, edit=True
    )


@router.callback_query(F.data.startswith("requests:page:"))
async def cb_requests_page(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    # requests:page:{bucket}:{page}
    parts = (callback.data or "").split(":")
    bucket = parts[2] if len(parts) > 2 else "awaiting_review"
    try:
        page = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        page = 0
    await _render_requests_bucket(
        callback.message, bucket=bucket, page=max(0, page), edit=True
    )


@router.callback_query(F.data == "settings:messages")
async def cb_settings_messages(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    with get_session() as session:
        rows = repo.list_bot_messages(session)
    page_size = PAGE_SIZE
    total = len(rows)
    pages = total_pages(total, page_size)
    chunk = rows[:page_size]
    kb_rows = [
        [
            InlineKeyboardButton(
                text=f"✏️ {m.key}",
                callback_data=f"msg:edit:{m.key}",
            )
        ]
        for m in chunk
    ]
    nav = pagination_row(
        page=0, total=total, page_size=page_size, callback_prefix="settings:msgpages"
    )
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")])
    await _ui_show(
        callback.message,
        f"🧾 <b>پیام‌های مشتری</b> — صفحه 1/{pages}\nکلید را برای ویرایش انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        edit=True,
    )


@router.callback_query(F.data.startswith("settings:msgpages:"))
async def cb_settings_msg_pages(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    try:
        page = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        page = 0
    with get_session() as session:
        rows = repo.list_bot_messages(session)
    page_size = PAGE_SIZE
    total = len(rows)
    pages = total_pages(total, page_size)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * page_size : page * page_size + page_size]
    kb_rows = [
        [
            InlineKeyboardButton(
                text=f"✏️ {m.key}",
                callback_data=f"msg:edit:{m.key}",
            )
        ]
        for m in chunk
    ]
    nav = pagination_row(
        page=page, total=total, page_size=page_size, callback_prefix="settings:msgpages"
    )
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")])
    await _ui_show(
        callback.message,
        f"🧾 <b>پیام‌های مشتری</b> — صفحه {page + 1}/{pages}\nکلید را برای ویرایش انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        edit=True,
    )


@router.callback_query(F.data.startswith("msg:edit:"))
async def cb_msg_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    key = (callback.data or "").split(":", 2)[2]
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        current = repo.get_bot_message(session, key)
        repo.set_state(session, f"admin_flow:{uid}", f"bot_msg_edit:{key}")
    await callback.message.answer(
        f"متن جدید برای <code>{key}</code> را بفرستید.\n\n"
        f"فعلی:\n{current}"
    )
    await callback.answer()


@router.callback_query(F.data == "settings:lifecycle")
async def cb_settings_lifecycle(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    with get_session() as session:
        days = repo.get_alert_days_threshold(session)
        traffic = repo.get_alert_traffic_mb_threshold(session)
        cool = repo.get_lifecycle_alert_cooldown_seconds(session)
        scan = repo.get_lifecycle_scan_interval_seconds(session)
    await _ui_show(
        callback.message,
        "📣 <b>هشدار اتمام سرویس (محلی)</b>\n\n"
        "روز/حجم از دیتابیس خودمان چک می‌شود "
        "(بروزرسانی از لینک اشتراک با TTL)، نه از پیام هشدار NetPlus.\n"
        "حذف سرویس فقط وقتی از NetPlus پیام حذف بیاید به مشتری اطلاع داده می‌شود.\n\n"
        f"آستانه روز: <code>{days}</code>\n"
        f"آستانه حجم: <code>{traffic:g}</code> مگابایت\n"
        f"فاصله تکرار هشدار: <code>{format_seconds_fa(cool)}</code>\n"
        f"بازه اسکن: <code>{format_seconds_fa(scan)}</code>\n\n"
        "متن پیام‌ها را از «پیام‌ها و محتوا» ویرایش کنید:\n"
        "<code>alert_almost_out_days</code> · "
        "<code>alert_almost_out_traffic</code> · "
        "<code>alert_service_deleted</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ ویرایش آستانه‌ها",
                        callback_data="lifecycle:edit",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🧾 پیام‌ها",
                        callback_data="settings:messages",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
        edit=True,
    )


@router.callback_query(F.data == "lifecycle:edit")
async def cb_lifecycle_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "lifecycle_edit")
    await callback.message.answer(
        "یک خط بفرستید، مثال:\n"
        "<code>days 3</code>\n"
        "<code>traffic 1024</code> (مگابایت)\n"
        "<code>cooldown 1440</code> (دقیقه)\n"
        "<code>scan 5</code> (دقیقه)"
    )
    await callback.answer()


@router.callback_query(F.data == "settings:userinfo_ttl")
async def cb_settings_userinfo_ttl(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        ttl = repo.get_userinfo_ttl_seconds(session)
    await callback.message.answer(
        "⏱ <b>TTL بروزرسانی از لینک اشتراک</b>\n\n"
        "وقتی مشتری اطلاعات سرویس را می‌بیند، اگر از آخرین "
        "بروزرسانی HTTP بیشتر از این مدت گذشته باشد، "
        "حجم/انقضا از همان لینک ذخیره‌شده خوانده می‌شود "
        "(بدون رفتن به ربات NetPlus).\n\n"
        f"مقدار فعلی: <code>{format_seconds_fa(ttl)}</code>\n"
        "۰ = همیشه بروزرسانی کن\n\n"
        "برای تغییر دکمه ویرایش را بزنید.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ ویرایش TTL",
                        callback_data="userinfo_ttl:edit",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "userinfo_ttl:edit")
async def cb_userinfo_ttl_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "userinfo_ttl_edit")
    await callback.message.answer(
        "فقط عدد <b>دقیقه</b> را بفرستید (مثال: <code>2</code> یا <code>0</code>)."
    )
    await callback.answer()


@router.callback_query(F.data == "settings:reminders")
async def cb_settings_reminders(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        ttl = repo.get_order_ttl_seconds(session)
        delays = repo.get_reminder_delays_seconds(session)
    await callback.message.answer(
        "⏰ <b>یادآوری و انقضای سفارش</b>\n\n"
        f"مهلت پرداخت: <code>{format_seconds_fa(ttl)}</code>\n"
        f"فواصل یادآوری: <code>{format_seconds_list_fa(delays)}</code>\n\n"
        "برای تغییر مهلت (دقیقه): <code>ttl 1440</code>\n"
        "برای تغییر یادآوری‌ها (دقیقه): <code>remind 5,60,360</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ ویرایش",
                        callback_data="reminders:edit",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "reminders:edit")
async def cb_reminders_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "reminders_edit")
    await callback.message.answer(
        "یک خط بفرستید (اعداد بر حسب <b>دقیقه</b>):\n"
        "• <code>ttl 1440</code>\n"
        "• <code>remind 5,60,360</code>"
    )
    await callback.answer()


@router.callback_query(F.data == "settings:approval")
async def cb_settings_approval(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    with get_session() as session:
        manual = repo.is_orders_manual_approval(session)
        direct = repo.is_direct_vendor_card_pay_global(session)
        users = repo.get_direct_vendor_card_usernames(session)
    manual_fa = "✅ روشن (تایید دستی ادمین)" if manual else "⚡ خاموش (فعال‌سازی خودکار)"
    direct_fa = (
        "✅ روشن برای همه نام‌کاربری‌ها"
        if direct
        else "⚪️ خاموش (سراسری)"
    )
    users_fa = "، ".join(f"<code>{u}</code>" for u in users) if users else "—"
    await _ui_show(
        callback.message,
        "✅ <b>تایید سفارش و کارت مستقیم فروشنده</b>\n\n"
        f"تایید دستی خرید/تمدید: {manual_fa}\n"
        f"پرداخت مستقیم به کارت فروشنده: {direct_fa}\n"
        f"نام‌کاربری‌های خاص: {users_fa}\n\n"
        "با پرداخت مستقیم، مشتری کارت/مبلغ NetPlus را می‌بیند "
        "(افزایش موجودی فروشنده)، حتماً رسید می‌فرستد، "
        "و اگر تا ۳ دقیقه تایید خودکار نشود رسید به فروشنده ارسال می‌شود.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔴 خاموش کردن تایید دستی"
                        if manual
                        else "🟢 روشن کردن تایید دستی",
                        callback_data="approval:manual:toggle",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔴 خاموش کردن پرداخت مستقیم سراسری"
                        if direct
                        else "🟢 روشن کردن پرداخت مستقیم سراسری",
                        callback_data="approval:direct:toggle",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📋 انتخاب نام‌کاربری (جستجو)",
                        callback_data="approval:direct:users",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
        edit=True,
    )


@router.callback_query(F.data == "settings:channel")
async def cb_settings_channel(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    from app.telegram.channel_gate import (
        get_required_channel,
        is_channel_gate_enabled,
        set_channel_gate_enabled,
    )

    channel = get_required_channel()
    enabled = is_channel_gate_enabled()
    await _ui_show(
        callback.message,
        "📢 <b>عضویت اجباری در کانال</b>\n\n"
        f"کانال: @{channel}\n"
        f"وضعیت دروازه: {'✅ روشن' if enabled else '⚪️ خاموش'}\n\n"
        "حتماً ربات مشتری <b>@fxonevpnbot</b> را در کانال "
        "<b>ادمین</b> کنید (حداقل دسترسی دیدن اعضا). "
        "اگر ادمین نباشد تلگرام خطای "
        "«member list is inaccessible» می‌دهد و چک عضویت کار نمی‌کند.\n"
        "برای محدود کردن محتوای کانال به مشترکان، از تنظیمات خود کانال "
        "(Join Requests / محدود کردن پیام‌ها) استفاده کنید — "
        "ربات فقط استفاده از منوی مشتری را قفل می‌کند.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔴 خاموش کردن دروازه"
                        if enabled
                        else "🟢 روشن کردن دروازه",
                        callback_data="channel:gate:toggle",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ تغییر نام کانال",
                        callback_data="channel:edit",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
        edit=True,
    )


@router.callback_query(F.data == "channel:gate:toggle")
async def cb_channel_gate_toggle(
    callback: CallbackQuery, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    from app.telegram.channel_gate import (
        is_channel_gate_enabled,
        set_channel_gate_enabled,
    )

    now = not is_channel_gate_enabled()
    set_channel_gate_enabled(now)
    await cb_settings_channel(callback, settings)


@router.callback_query(F.data == "channel:edit")
async def cb_channel_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "channel_edit")
    await callback.message.answer(
        "یوزرنیم کانال را بدون @ بفرستید (مثال: fxonevpn)."
    )
    await callback.answer()


@router.callback_query(F.data == "approval:manual:toggle")
async def cb_approval_manual_toggle(
    callback: CallbackQuery, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        now = not repo.is_orders_manual_approval(session)
        repo.set_orders_manual_approval(session, now)
    await cb_settings_approval(callback, settings)


@router.callback_query(F.data == "approval:direct:toggle")
async def cb_approval_direct_toggle(
    callback: CallbackQuery, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        now = not repo.is_direct_vendor_card_pay_global(session)
        repo.set_direct_vendor_card_pay_global(session, now)
    await cb_settings_approval(callback, settings)


@router.callback_query(F.data == "settings:maintenance")
async def cb_settings_maintenance(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        enabled = repo.is_customer_bot_enabled(session)
        msg = repo.get_customer_bot_disabled_message(session)
    status = "✅ فعال" if enabled else "🛠 غیرفعال (حالت تعمیر)"
    await callback.message.answer(
        f"🛠 <b>حالت تعمیر ربات مشتری</b>\n\n"
        f"وضعیت: {status}\n\n"
        f"پیام زمان خاموشی:\n{msg}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🟢 روشن کردن ربات مشتری"
                        if not enabled
                        else "🔴 خاموش کردن ربات مشتری",
                        callback_data="maintenance:toggle",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ ویرایش پیام خاموشی",
                        callback_data="maintenance:edit_msg",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "maintenance:toggle")
async def cb_maintenance_toggle(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        currently_on = repo.is_customer_bot_enabled(session)
        repo.set_customer_bot_maintenance(session, enabled=not currently_on)
        now_on = repo.is_customer_bot_enabled(session)
        msg = repo.get_customer_bot_disabled_message(session)
    if now_on:
        await callback.message.answer(
            "✅ ربات مشتری دوباره فعال شد.",
            reply_markup=settings_menu(),
        )
    else:
        await callback.message.answer(
            "🛠 ربات مشتری خاموش شد.\n"
            f"مشتری این پیام را می‌بیند:\n\n{msg}",
            reply_markup=settings_menu(),
        )
    await callback.answer()


@router.callback_query(F.data == "maintenance:edit_msg")
async def cb_maintenance_edit_msg(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "maintenance_msg_edit")
        current = repo.get_customer_bot_disabled_message(session)
    await callback.message.answer(
        "پیام زمان خاموشی ربات مشتری را بفرستید:\n\n"
        f"فعلی:\n{current}"
    )
    await callback.answer()


@router.callback_query(F.data == "settings:education")
async def cb_settings_education(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        cats = repo.list_all_education_categories(session)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if c.enabled else '⛔'} {c.title}",
                callback_data=f"edu:admin:cat:{c.id}",
            )
        ]
        for c in cats
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ دسته جدید",
                callback_data="edu:admin:addcat",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")])
    await callback.message.answer(
        "📚 <b>مدیریت آموزش</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data == "edu:admin:addcat")
async def cb_edu_addcat(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "edu_add_cat")
    await callback.message.answer("عنوان دسته جدید را بفرستید:")
    await callback.answer()


@router.callback_query(F.data.startswith("edu:admin:cat:"))
async def cb_edu_admin_cat(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    cat_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        cat = repo.get_education_category(session, cat_id)
        lessons = repo.list_education_lessons(session, cat_id, enabled_only=False)
    if not cat:
        await callback.answer("یافت نشد", show_alert=True)
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"📄 {L.title}",
                callback_data=f"edu:admin:lesson:{L.id}",
            )
        ]
        for L in lessons
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ درس جدید",
                callback_data=f"edu:admin:addlesson:{cat_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🗑 حذف دسته",
                callback_data=f"edu:admin:delcat:{cat_id}",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="⬅️ آموزش", callback_data="settings:education")]
    )
    await callback.message.answer(
        f"📁 <b>{cat.title}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edu:admin:addlesson:"))
async def cb_edu_addlesson(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    cat_id = int((callback.data or "").rsplit(":", 1)[1])
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", f"edu_add_lesson:{cat_id}")
    await callback.message.answer(
        "درس را در یک پیام بفرستید:\n"
        "خط اول = عنوان\n"
        "خطوط بعدی = متن درس"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edu:admin:delcat:"))
async def cb_edu_delcat(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    cat_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        ok = repo.delete_education_category(session, cat_id)
    await callback.answer("حذف شد" if ok else "ناموفق", show_alert=True)
    await cb_settings_education(callback, settings)


@router.callback_query(F.data.startswith("edu:admin:lesson:"))
async def cb_edu_admin_lesson(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    lesson_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        lesson = repo.get_education_lesson(session, lesson_id)
    if not lesson:
        await callback.answer("یافت نشد", show_alert=True)
        return
    media_line = "بدون ویدیو/رسانه"
    if getattr(lesson, "media_file_id", None):
        media_line = (
            f"رسانه: {lesson.media_type or '?'} "
            f"(caption: {(lesson.media_caption or '—')[:40]})"
        )
    await callback.message.answer(
        f"📄 <b>{lesson.title}</b>\n\n{lesson.body}\n\n🎞 {media_line}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎬 تنظیم ویدیو / رسانه",
                        callback_data=f"edu:admin:media:{lesson.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 حذف رسانه",
                        callback_data=f"edu:admin:clearmedia:{lesson.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 حذف درس",
                        callback_data=f"edu:admin:dellesson:{lesson.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔙",
                        callback_data=f"edu:admin:cat:{lesson.category_id}",
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edu:admin:media:"))
async def cb_edu_admin_media(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    lesson_id = int((callback.data or "").rsplit(":", 1)[1])
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", f"edu_set_media:{lesson_id}")
    await callback.message.answer(
        "ویدیو (یا عکس/فایل) را همین‌جا بفرستید.\n"
        "اگر caption بگذارید همان به‌عنوان توضیح رسانه ذخیره می‌شود.\n"
        "انصراف: <code>-</code>"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edu:admin:clearmedia:"))
async def cb_edu_clear_media(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    lesson_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        repo.update_education_lesson(session, lesson_id, clear_media=True)
    await callback.answer("رسانه حذف شد", show_alert=True)
    callback.data = f"edu:admin:lesson:{lesson_id}"
    await cb_edu_admin_lesson(callback, settings)


@router.callback_query(F.data.startswith("edu:admin:dellesson:"))
async def cb_edu_dellesson(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    lesson_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        lesson = repo.get_education_lesson(session, lesson_id)
        cat_id = lesson.category_id if lesson else None
        ok = repo.delete_education_lesson(session, lesson_id)
    await callback.answer("حذف شد" if ok else "ناموفق", show_alert=True)
    if cat_id:
        # reuse cat view
        callback.data = f"edu:admin:cat:{cat_id}"
        await cb_edu_admin_cat(callback, settings)


@router.callback_query(F.data.startswith("ticket:reply:"))
async def cb_ticket_reply(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    ticket_id = int((callback.data or "").rsplit(":", 1)[1])
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        ticket = repo.get_support_ticket(session, ticket_id)
        if not ticket:
            await callback.answer("یافت نشد", show_alert=True)
            return
        repo.set_state(session, f"admin_flow:{uid}", f"ticket_reply:{ticket_id}")
    await callback.message.answer(f"پاسخ تیکت #{ticket_id} را بفرستید:")
    await callback.answer()


async def _send_card_info(
    message: Message, settings: Settings, *, edit: bool = False
) -> None:
    with get_session() as session:
        number, holder = repo.get_payment_card(
            session, settings.reseller_card_number, settings.reseller_card_holder
        )
        invoice_desc = repo.get_invoice_description(session)
    shown = number if number else "— تنظیم نشده —"
    await _ui_show(
        message,
        "💳 <b>کارت واریز مشتریان</b>\n\n"
        f"شماره: <code>{shown}</code>\n"
        f"به نام: <b>{holder or '—'}</b>\n\n"
        f"📝 متن فاکتور درگاه: <b>{invoice_desc}</b>\n"
        "اختیاری در متن: <code>{order}</code> <code>{kind}</code> <code>{user}</code>\n\n"
        "این کارت روی فاکتور مشتری نشان داده می‌شود.\n"
        "هر سفارش یک <b>مبلغ یکتای ریال</b> دارد تا بتوان پرداخت را مچ کرد.\n\n"
        "تایید دستی با مبلغ: پیام بفرستید مثل\n"
        "<code>تایید مبلغ 1500100</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ تغییر کارت",
                        callback_data="card:edit",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📄 تغییر متن فاکتور",
                        callback_data="invoice_desc:edit",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
        edit=edit,
    )


@router.callback_query(F.data == "card:edit")
async def cb_card_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", "card_edit_number")
    await callback.message.answer(
        "۱۶ رقم شماره کارت را بفرستید (بدون فاصله):"
    )
    await callback.answer()


@router.callback_query(F.data == "invoice_desc:edit")
async def cb_invoice_desc_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id
    with get_session() as session:
        current = repo.get_invoice_description(session)
        repo.set_state(session, f"admin_flow:{uid}", "invoice_desc_edit")
    await callback.message.answer(
        "📝 متن جدید فاکتور درگاه را بفرستید.\n"
        f"فعلی: <b>{current}</b>\n\n"
        "مثال‌ها:\n"
        "<code>پرداخت سفارش</code>\n"
        "<code>سفارش {order}</code>\n"
        "<code>{kind} سرویس {user}</code>\n\n"
        "اگر {order} نگذارید، شماره سفارش خودکار اضافه می‌شود."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("price:edit:"))
async def cb_price_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    # price:edit:ID  or  price:edit:friends:ID / colleagues:ID
    parts = (callback.data or "").split(":")
    tier = "default"
    plan_id = 0
    if len(parts) >= 4 and parts[2] in ("friends", "colleagues", "default"):
        tier = parts[2]
        plan_id = int(parts[3])
    else:
        plan_id = int(parts[-1])
    uid = callback.from_user.id
    with get_session() as session:
        plan = repo.get_price_plan(session, plan_id)
        if not plan:
            await callback.answer("یافت نشد", show_alert=True)
            return
        repo.set_state(session, f"admin_flow:{uid}", f"price_edit:{tier}:{plan_id}")
        title = plan.title
        if tier == "friends":
            current = plan.price_friends_toman
        elif tier == "colleagues":
            current = plan.price_colleagues_toman
        else:
            current = plan.price_toman
    tier_fa = repo.RATE_TIER_LABELS.get(tier, "عادی")
    cur_fa = f"{int(current):,}" if current is not None else "— (همان نرخ عادی)"
    await callback.message.answer(
        f"💰 قیمت «{tier_fa}» برای <b>{title}</b> را به تومان بفرستید.\n"
        f"فعلی: <code>{cur_fa}</code>\n"
        "برای پاک کردن نرخ ویژه: <code>-</code>"
    )
    await callback.answer()


async def _send_prices(message: Message, *, edit: bool = False, page: int = 0) -> None:
    with get_session() as session:
        active = repo.list_price_plans(session, enabled_only=True)
        all_plans = repo.list_price_plans(session, enabled_only=False)
        archived = [p for p in all_plans if not p.enabled]
    if not active and not archived:
        await _ui_show(
            message,
            "هنوز پلنی تعریف نشده.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")]
                ]
            ),
            edit=edit,
        )
        return
    page_size = PAGE_SIZE
    total = len(active)
    pages = total_pages(total, page_size) if total else 1
    page = max(0, min(page, pages - 1))
    chunk = active[page * page_size : page * page_size + page_size]
    lines = [f"💰 <b>پلن‌های فعال فروش</b> — صفحه {page + 1}/{pages}"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    for p in chunk:
        margin = ""
        if p.vendor_cost_toman:
            margin = f" · سود ~{(p.price_toman - p.vendor_cost_toman):,}"
        icon = "💎" if int(p.duration_days or 0) == 0 else "✨"
        fr = getattr(p, "price_friends_toman", None)
        col = getattr(p, "price_colleagues_toman", None)
        extra = ""
        if fr:
            extra += f" · دوستان {int(fr):,}"
        if col:
            extra += f" · همکار {int(col):,}"
        lines.append(
            f"{icon} <b>{p.title}</b> — <code>{p.price_toman:,}</code> ت"
            f"{margin}{extra}"
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"✏️ عادی {icon} {p.title[:18]}",
                    callback_data=f"price:edit:{p.id}",
                )
            ]
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="👫 دوستان",
                    callback_data=f"price:edit:friends:{p.id}",
                ),
                InlineKeyboardButton(
                    text="👔 همکار",
                    callback_data=f"price:edit:colleagues:{p.id}",
                ),
            ]
        )
    if archived:
        lines.append("\n🗄 <b>بایگانی</b>")
        for p in archived[:8]:
            lines.append(f"• {p.title} — <code>{p.price_toman:,}</code> ت")
    nav = pagination_row(
        page=page, total=total, page_size=page_size, callback_prefix="prices:pages"
    )
    if nav:
        kb_rows.append(nav)
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")]
    )
    await _ui_show(
        message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        edit=edit,
    )


@router.callback_query(F.data.startswith("prices:pages:"))
async def cb_prices_pages(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    try:
        page = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        page = 0
    await _send_prices(callback.message, edit=True, page=max(0, page))


def _resolve_sub_from_callback(data: str, *, id_prefix: str) -> tuple[str | None, int | None]:
    """
    Parse callbacks like subinfo:id:123 / subinfo:exact_name / sub:refresh:id:123.
    Returns (exact_username_or_None, subscription_id_or_None).
    """
    raw = data or ""
    if f"{id_prefix}:id:" in raw or raw.startswith(f"{id_prefix}id:"):
        # normalize: "subinfo:id:12" or mistaken forms
        pass
    parts = raw.split(":")
    # subinfo:id:123  -> ['subinfo','id','123']
    # sub:refresh:id:123 -> ['sub','refresh','id','123']
    # subinfo:uname -> ['subinfo','uname']
    if "id" in parts:
        idx = parts.index("id")
        if idx + 1 < len(parts) and parts[idx + 1].isdigit():
            return None, int(parts[idx + 1])
    # fallback exact username = everything after first prefix segment(s)
    if raw.startswith("subinfo:"):
        uname = raw.split(":", 1)[1]
        if uname.startswith("id:"):
            return None, int(uname.split(":", 1)[1])
        return uname, None
    if raw.startswith("sub:refresh:"):
        rest = raw.split(":", 2)[2]
        if rest.startswith("id:"):
            return None, int(rest.split(":", 1)[1])
        return rest, None
    return None, None


def _load_exact_subscription(session, *, username: str | None, sub_id: int | None):
    if sub_id is not None:
        return repo.get_subscription(session, sub_id)
    if username:
        return repo.get_subscription_by_username(session, username)
    return None


@router.callback_query(F.data.startswith("subinfo:"))
async def cb_subinfo(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    username, sub_id = _resolve_sub_from_callback(callback.data or "", id_prefix="subinfo")
    with get_session() as session:
        sub = _load_exact_subscription(session, username=username, sub_id=sub_id)
        if not sub:
            await safe_answer(callback, "یافت نشد", show_alert=True)
            return
        await safe_answer(callback)
        from app.services.subscription_refresh import refresh_subscription_userinfo

        refresh_subscription_userinfo(session, subscription=sub, force=False)
        text = _format_sub_detail(session, sub)
        src = (repo.get_state(session, f"admin_detail_from:{uid}") or "").strip()
        hide_src = src if src in ("local_alerts", "warnings") else None
        markup = sub_actions(
            sub.username,
            sub.id,
            back_cb=_detail_back_cb(session, uid),
            hide_alert_src=hide_src,
        )
    await _ui_show(callback.message, text, reply_markup=markup, edit=True)


@router.callback_query(F.data.startswith("admin:svclink:"))
async def cb_admin_svclink(callback: CallbackQuery, settings: Settings) -> None:
    """Show subscription URL + QR from admin service detail menu."""
    if not is_admin(callback.from_user.id, settings):
        return
    username = (callback.data or "").split(":", 2)[2]
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        url = (sub.subscription_url or "").strip() if sub else ""
        back = (
            f"subinfo:id:{sub.id}"
            if sub
            else f"subinfo:{username[:40]}"
        )
    if not url:
        await safe_answer(
            callback,
            "لینک اشتراک ذخیره نشده است.",
            show_alert=True,
        )
        return
    await safe_answer(callback)
    caption = (
        f"🔗 <b>لینک اشتراک</b>\n"
        f"👤 <code>{ltr(username)}</code>\n\n"
        f"<code>{url}</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 کپی لینک",
                    copy_text=CopyTextButton(text=url),
                )
            ],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)],
        ]
    )
    if url.startswith("http"):
        try:
            from app.utils.subscription_qr import subscription_qr_png

            png = subscription_qr_png(url)
            await callback.message.answer_photo(
                BufferedInputFile(png, filename=f"{username[:40]}_sub.png"),
                caption=caption,
                reply_markup=kb,
            )
            return
        except Exception:
            log.exception("admin subscription QR failed for %s", username)
    await _ui_show(callback.message, caption, reply_markup=kb, edit=True)


@router.callback_query(F.data.startswith("sub:refresh:"))
async def cb_refresh(callback: CallbackQuery, settings: Settings) -> None:
    """Refresh service usage/expiry from the stored subscription URL only."""
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    username, sub_id = _resolve_sub_from_callback(
        callback.data or "", id_prefix="sub:refresh"
    )
    with get_session() as session:
        sub0 = _load_exact_subscription(session, username=username, sub_id=sub_id)
        if not sub0:
            await safe_answer(callback, "یافت نشد", show_alert=True)
            return
        uname = sub0.username
        url = (sub0.subscription_url or "").strip()
    await safe_answer(callback, "در حال بروزرسانی از لینک…")
    from app.services.subscription_refresh import refresh_subscription_userinfo

    with get_session() as session:
        back = _detail_back_cb(session, uid)
        sub = (
            repo.get_subscription(session, sub_id)
            if sub_id is not None
            else repo.get_subscription_by_username(session, uname)
        )
        if not sub:
            await _ui_show(
                callback.message,
                "سرویس یافت نشد.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)]
                    ]
                ),
                edit=True,
            )
            return
        src = (repo.get_state(session, f"admin_detail_from:{uid}") or "").strip()
        hide_src = src if src in ("local_alerts", "warnings") else None
        if not url.startswith("http"):
            markup = sub_actions(
                sub.username, sub.id, back_cb=back, hide_alert_src=hide_src
            )
            await _ui_show(
                callback.message,
                "❌ لینک اشتراک ذخیره نشده؛ بروزرسانی از URL ممکن نیست.\n"
                + _format_sub_detail(session, sub),
                reply_markup=markup,
                edit=True,
            )
            return
        ok = refresh_subscription_userinfo(session, subscription=sub, force=True)
        session.refresh(sub)
        prefix = "🔄 <b>از لینک بروز شد</b>\n" if ok else "⚠️ <b>بروزرسانی لینک ناموفق بود</b>\n"
        text = prefix + _format_sub_detail(session, sub)
        markup = sub_actions(
            sub.username, sub.id, back_cb=back, hide_alert_src=hide_src
        )
    await _ui_show(callback.message, text, reply_markup=markup, edit=True)


@router.callback_query(F.data == "own:bulk")
async def cb_own_bulk(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        n = repo.count_unlinked_subscriptions(session)
        repo.set_state(
            session, f"admin_flow:{callback.from_user.id}", "bulk_wait_tg"
        )
    await _ui_show(
        callback.message,
        f"📦 <b>اختصاص دسته‌جمعی</b>\n"
        f"سرویس بدون مالک: <b>{n}</b>\n"
        "شناسه تلگرام مشتری مقصد را بفرستید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="menu:home")]
            ]
        ),
        edit=True,
    )
    await callback.answer()


async def _render_own_one_page(
    message: Message,
    *,
    admin_uid: int,
    page: int = 0,
    edit: bool = True,
) -> None:
    """Paginated unlinked services for single ownership assign + page-all transfer."""
    page_size = PAGE_SIZE
    page = max(0, int(page))
    with get_session() as session:
        # Probe total first so we can clamp page before loading rows.
        _probe, total = repo.list_unlinked_subscriptions_page(
            session, offset=0, limit=1
        )
        if total == 0:
            repo.set_state(session, f"admin_flow:{admin_uid}", "")
            repo.set_state(session, f"admin_own_page_ids:{admin_uid}", "[]")
            buttons = []
            rows = []
            pages = 1
        else:
            pages = total_pages(total, page_size)
            page = max(0, min(page, pages - 1))
            rows, total = repo.list_unlinked_subscriptions_page(
                session, offset=page * page_size, limit=page_size
            )
            repo.set_state(session, f"admin_own_page:{admin_uid}", str(page))
            repo.set_state(
                session,
                f"admin_own_page_ids:{admin_uid}",
                json.dumps([int(s.id) for s in rows]),
            )
            repo.set_state(session, f"admin_flow:{admin_uid}", "")
            buttons = [
                (
                    sub.id,
                    sub.username,
                    _vendor_list_icon(session, sub),
                    _owner_line(session, sub),
                )
                for sub in rows
            ]
    if total == 0:
        await _ui_show(
            message,
            "👤 <b>اختصاص تکی</b>\n"
            "سرویس بدون مالکی برای اختصاص نیست.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔍 جستجو با نام",
                            callback_data="own:search",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ بازگشت", callback_data="menu:ownership"
                        )
                    ],
                ]
            ),
            edit=edit,
        )
        return
    start = page * page_size
    kb_rows: list[list[InlineKeyboardButton]] = []
    for sub_id, username, vendor_icon, _owner in buttons:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{vendor_icon} {ltr(username)} {vendor_icon}",
                    callback_data=f"own:as:{sub_id}",
                )
            ]
        )
    if buttons:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="📦 انتقال همه سرویس‌های این صفحه",
                    callback_data=f"own:page_all:{page}",
                )
            ]
        )
    nav = pagination_row(
        page=page, total=total, page_size=page_size, callback_prefix="own:pages"
    )
    if nav:
        kb_rows.append(nav)
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔍 جستجو با نام",
                callback_data="own:search",
            )
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="menu:ownership")]
    )
    await _ui_show(
        message,
        "👤 <b>اختصاص تکی</b>\n"
        "سرویس‌های بدون مالک — یکی را بزنید یا همهٔ این صفحه را منتقل کنید.\n"
        f"صفحه {page + 1}/{pages} · ({start + 1}–{start + len(buttons)} از {total})",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        edit=edit,
    )


@router.callback_query(F.data == "own:one")
async def cb_own_one(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    await _render_own_one_page(
        callback.message, admin_uid=uid, page=0, edit=True
    )


@router.callback_query(F.data.startswith("own:pages:"))
async def cb_own_pages(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    try:
        page = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        page = 0
    await _render_own_one_page(
        callback.message, admin_uid=uid, page=max(0, page), edit=True
    )


@router.callback_query(F.data == "own:search")
async def cb_own_search(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    with get_session() as session:
        repo.set_state(
            session, f"admin_flow:{callback.from_user.id}", "assign_wait_username"
        )
    await _ui_show(
        callback.message,
        "نام کاربری سرویس را بفرستید (اختصاص / جابه‌جایی مالک):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت به لیست", callback_data="own:one"
                    )
                ]
            ]
        ),
        edit=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("own:page_all:"))
async def cb_own_page_all(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    try:
        page = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        page = 0
    with get_session() as session:
        raw = (repo.get_state(session, f"admin_own_page_ids:{uid}") or "[]").strip()
        try:
            ids = [int(x) for x in json.loads(raw)]
        except (json.JSONDecodeError, TypeError, ValueError):
            ids = []
        if not ids:
            # refresh page ids
            rows, _total = repo.list_unlinked_subscriptions_page(
                session, offset=page * PAGE_SIZE, limit=PAGE_SIZE
            )
            ids = [int(s.id) for s in rows]
            repo.set_state(
                session, f"admin_own_page_ids:{uid}", json.dumps(ids)
            )
        if not ids:
            await safe_answer(callback, "سرویسی در این صفحه نیست", show_alert=True)
            return
        repo.set_state(session, f"admin_flow:{uid}", f"assign_page_wait_tg:{page}")
        n = len(ids)
    await _ui_show(
        callback.message,
        f"📦 <b>انتقال همه سرویس‌های این صفحه</b>\n"
        f"تعداد در این صفحه: <b>{n}</b>\n"
        "شناسه تلگرام مشتری مقصد را بفرستید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت به لیست",
                        callback_data=f"own:pages:{page}",
                    )
                ]
            ]
        ),
        edit=True,
    )
    await callback.answer()


@router.callback_query(F.data == "menu:ownership")
async def cb_menu_ownership(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        unlinked = repo.count_unlinked_subscriptions(session)
        repo.set_state(session, f"admin_flow:{uid}", "")
    await _ui_show(
        callback.message,
        "👤 <b>مالکیت سرویس</b>\n"
        f"بدون مالک (قابل اختصاص): <b>{unlinked}</b>\n"
        "اختصاص فقط در دیتابیس محلی است؛ به NetPlus منتقل نمی‌شود.",
        reply_markup=ownership_menu(),
        edit=True,
    )


@router.callback_query(F.data.startswith("own:as:"))
async def cb_own_assign_sub(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    sub_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        from app.database.models import Subscription

        sub = session.get(Subscription, sub_id)
        if not sub:
            await callback.answer("یافت نشد", show_alert=True)
            return
        uname = sub.username
        owner = _owner_line(session, sub)
        repo.set_state(
            session, f"admin_flow:{callback.from_user.id}", f"assign_wait_tg:{sub_id}"
        )
    await _ui_show(
        callback.message,
        f"👤 تعیین مالک برای <code>{uname}</code>\n{owner}\n"
        "شناسه تلگرام مشتری مقصد را بفرستید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت به لیست", callback_data="own:one"
                    )
                ]
            ]
        ),
        edit=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("own:bulk_ok:"))
async def cb_own_bulk_confirm(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    to_tg = int((callback.data or "").rsplit(":", 1)[1])
    admin_id = callback.from_user.id
    try:
        with get_session() as session:
            if not repo.get_customer_profile(session, to_tg):
                await callback.answer(
                    "پروفایل مشتری نیست؛ اول باید ربات مشتری را استارت کند.",
                    show_alert=True,
                )
                return
            links = repo.bulk_assign_unlinked(
                session,
                to_telegram_id=to_tg,
                initiated_by_telegram_id=admin_id,
                note="admin_bulk_ui",
            )
            n = len(links)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.answer(
        f"✅ اختصاص دسته‌جمعی انجام شد.\n"
        f"تعداد: <b>{n}</b> → <code>{to_tg}</code>",
        reply_markup=admin_reply_menu(),
    )
    await callback.answer("انجام شد")


@router.callback_query(F.data.startswith("corder:vendor_fwd:"))
async def customer_order_vendor_forward(
    callback: CallbackQuery, settings: Settings
) -> None:
    """Manual: click vendor «ارسال رسید» then upload stored customer receipt."""
    if not is_admin(callback.from_user.id, settings):
        return
    order_id = int((callback.data or "").rsplit(":", 1)[1])
    await callback.answer("در حال ارسال به فروشنده…")
    from app.services.vendor_direct_pay import forward_receipt_to_vendor

    result = await forward_receipt_to_vendor(order_id, force=True)
    if not result.get("ok"):
        err = result.get("error") or "fail"
        await callback.answer(f"ناموفق: {err}", show_alert=True)
        if callback.message:
            try:
                await callback.message.answer(
                    f"❌ ارسال رسید سفارش #{order_id} به فروشنده ناموفق بود:\n"
                    f"<code>{err}</code>"
                )
            except Exception:
                pass
        return
    if result.get("already"):
        await callback.answer("قبلاً ارسال شده بود", show_alert=True)
        return
    await callback.answer("ارسال شد ✅", show_alert=True)
    if callback.message:
        try:
            await callback.message.answer(
                f"📤 رسید سفارش #{order_id} به فروشنده ارسال شد "
                "(دکمه ارسال رسید زده شد، سپس عکس فرستاده شد)."
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("corder:ok:"))
async def customer_order_approve(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    """Admin manual approve — same path as panel so all tracked approve UIs clear."""
    _ = renew_queue
    if not is_admin(callback.from_user.id, settings):
        return
    order_id = int((callback.data or "").rsplit(":", 1)[1])
    # Track the message the admin tapped so it is deleted with other review chats.
    if callback.message and callback.from_user:
        with get_session() as session:
            repo.track_admin_order_ui_message(
                session,
                order_id,
                int(callback.message.chat.id),
                int(callback.message.message_id),
                delete_on_done=True,
            )
    from app.services.admin_order_actions import approve_customer_order

    result = await approve_customer_order(
        order_id,
        source="admin_manual",
        requested_by=callback.from_user.id if callback.from_user else None,
    )
    if not result.get("ok"):
        err = result.get("error") or result.get("status") or "fail"
        await callback.answer(f"ناموفق: {err}", show_alert=True)
        return
    if result.get("already"):
        await callback.answer("قبلاً در صف/انجام است", show_alert=True)
        return
    job_id = result.get("job_id")
    if job_id:
        await callback.answer(f"تایید شد — صف فروشنده #{job_id}", show_alert=True)
    else:
        await callback.answer("تایید شد — سرویس شروع شد", show_alert=True)


@router.callback_query(F.data.startswith("corder:manual_done:"))
async def customer_order_manual_done(
    callback: CallbackQuery, settings: Settings, renew_queue: RenewQueue
) -> None:
    """Admin finished create/renew on vendor manually — close order + notify customer."""
    if not is_admin(callback.from_user.id, settings):
        return
    order_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            await safe_answer(callback, "یافت نشد", show_alert=True)
            return
        if order.kind == "wallet":
            await safe_answer(
                callback, "برای کیف پول از تایید عادی استفاده کنید", show_alert=True
            )
            return
        job_id = order.renew_job_id
        uname = order.service_username
        cust_tg = order.telegram_id
        sub = repo.get_subscription_by_username(session, uname)
        # Prefer the inventory row that already has vendor data (URL/expiry).
        if sub and not (sub.subscription_url or "").strip():
            richer = repo.get_subscription_by_username(session, uname)
            if richer and (richer.subscription_url or "").strip():
                sub = richer
        if sub:
            uname = sub.username
            order.service_username = uname
            repo.merge_username_case_duplicates(
                session, keep=sub, canonical_username=uname
            )
        sub_pk = sub.id if sub else None
        already_ready = bool(sub and (sub.subscription_url or "").strip())
        if not job_id:
            order.status = "done"
            order.completed_at = datetime.utcnow()
            order.note = (order.note or "") + "|manual_admin_complete"
            repo.touch_customer_order(order)
        # If vendor already finished (create_ok) but job id was lost / case-mismatched,
        # do not invent a second empty inventory row — just notify the customer.
        if already_ready and not job_id:
            job_id = None
    if job_id:
        result = await renew_queue.mark_manual_complete(job_id)
        await safe_answer(
            callback,
            "تایید دستی ثبت شد" if result == "ok" else result,
            show_alert=result != "ok",
        )
        await _ui_show(
            callback.message,
            f"✅ انجام دستی سفارش #{order_id} / صف #{job_id}\n"
            f"سرویس <code>{uname}</code>",
            edit=True,
        )
        return
    await safe_answer(callback, "تایید دستی ثبت شد")
    from app.telegram.handlers.customer import cleanup_customer_order_ui

    try:
        await cleanup_customer_order_ui(cust_tg, order_id)
        if customer_bridge.renew_queue:
            with get_session() as session:
                order = repo.get_customer_order(session, order_id)
                kind = (order.kind if order else "renew") or "renew"
                sub = repo.get_subscription_by_username(session, uname)
                url = (sub.subscription_url or "").strip() if sub else ""
                sid = sub.id if sub else sub_pk
                if sub:
                    uname = sub.username
            await customer_bridge.renew_queue._notify_customer_final(
                telegram_id=cust_tg,
                username=uname,
                job_kind=kind,
                order_id=order_id,
                sub_id=sid,
                url=url,
            )
    except Exception:
        pass
    await _ui_show(
        callback.message,
        f"✅ انجام دستی سفارش #{order_id}\nسرویس <code>{uname}</code>",
        edit=True,
    )


@router.callback_query(F.data.startswith("corder:amt:"))
async def customer_order_confirm_amount(
    callback: CallbackQuery, settings: Settings
) -> None:
    """Approve by the order's unique rial amount (same as «تایید مبلغ …»)."""
    if not is_admin(callback.from_user.id, settings):
        return
    order_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            await callback.answer("یافت نشد", show_alert=True)
            return
        amount = (order.amount_rial or "").strip()
        uname = order.service_username
    if not amount:
        await callback.answer("مبلغ یکتای ریال روی سفارش نیست", show_alert=True)
        return
    if callback.message and callback.message.chat:
        with get_session() as session:
            repo.track_admin_order_ui_message(
                session,
                order_id,
                int(callback.message.chat.id),
                int(callback.message.message_id),
                delete_on_done=True,
            )
    result = await auto_confirm_by_amount(amount, source="admin_amount_btn")
    if not result.get("ok"):
        await callback.answer(
            f"ناموفق: {result.get('error') or result}", show_alert=True
        )
        return
    await callback.answer(
        f"تایید شد — {uname} / مبلغ {amount}", show_alert=True
    )


@router.callback_query(F.data.startswith("corder:no:"))
async def customer_order_reject(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    order_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            await callback.answer("یافت نشد", show_alert=True)
            return
        if order.status in ("done", "fulfilling", "expired", "cancelled"):
            await callback.answer(f"قابل رد نیست: {order.status}", show_alert=True)
            return
        uid = callback.from_user.id if callback.from_user else 0
        repo.set_state(session, f"admin_flow:{uid}", f"reject_reason:{order_id}")
        if callback.message and callback.message.chat:
            repo.track_admin_order_ui_message(
                session,
                order_id,
                int(callback.message.chat.id),
                int(callback.message.message_id),
                delete_on_done=True,
            )
    await callback.answer()
    await callback.message.answer(
        f"❌ دلیل رد سفارش #{order_id} را بنویسید.\n"
        "این متن برای مشتری ارسال می‌شود و تیکت پشتیبانی باز می‌شود."
    )


async def _try_save_education_media(message: Message) -> bool:
    """If admin is in edu_set_media flow, save video/photo/doc. Returns True if handled."""
    uid = message.from_user.id if message.from_user else 0
    with get_session() as session:
        flow = (repo.get_state(session, f"admin_flow:{uid}") or "").strip()
    if not flow.startswith("edu_set_media:"):
        return False
    lesson_id = int(flow.split(":", 1)[1])
    media_type = None
    file_id = None
    if message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.animation:
        media_type = "animation"
        file_id = message.animation.file_id
    elif message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
    elif (message.text or "").strip() in {"-", "—"}:
        with get_session() as session:
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer("لغو شد.")
        return True
    else:
        await message.answer("ویدیو، عکس یا فایل بفرستید (یا - برای انصراف).")
        return True
    caption = (message.caption or "").strip() or None
    with get_session() as session:
        repo.update_education_lesson(
            session,
            lesson_id,
            media_type=media_type,
            media_file_id=file_id,
            media_caption=caption,
        )
        repo.set_state(session, f"admin_flow:{uid}", "")
    await message.answer(
        f"✅ رسانه درس #{lesson_id} ذخیره شد ({media_type})."
        + (" با caption." if caption else "")
    )
    return True


@router.message(F.video | F.animation | F.document)
async def on_admin_media(message: Message, settings: Settings) -> None:
    if not is_admin(message.from_user.id if message.from_user else None, settings):
        return
    if await _try_save_education_media(message):
        return


@router.message(F.photo)
async def on_receipt_photo(
    message: Message, settings: Settings, renew_queue: RenewQueue
) -> None:
    if not is_admin(message.from_user.id if message.from_user else None, settings):
        return
    if await _try_save_education_media(message):
        return
    with get_session() as session:
        jobs = [
            j
            for j in repo.list_active_renew_jobs(session)
            if j.status in ("awaiting_receipt_confirm", "receipt_window")
        ]
    if not jobs:
        await message.answer(
            "عکس رسید برای سفارش فعالی در حالت رسید نیست.\n"
            "اول دکمه رسید را بزنید، بعد عکس را بفرستید."
        )
        return
    job = jobs[0]
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    from io import BytesIO

    buf = BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)
    data = buf.getvalue()
    result = await renew_queue.forward_receipt_photo(job.id, data)
    if result != "ok":
        await message.answer(f"رسید: {result}")


@router.message(F.text)
async def text_router(message: Message, settings: Settings, renew_queue: RenewQueue) -> None:
    if not is_admin(message.from_user.id if message.from_user else None, settings):
        return
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return
    if query == BTN_START:
        return

    uid = message.from_user.id if message.from_user else 0

    # Persistent reply-menu shortcuts (NetPlus-style; labels configurable in panel)
    if query in admin_menu_labels("admin", "status"):
        with get_session() as session:
            counts = repo.count_by_status(session)
            header = repo.get_bot_message(session, "admin.status_header")
        lines = [header]
        for key, label in {
            "active": "✅ فعال",
            "warning": "⚠️ هشدار",
            "expired": "⌛ منقضی",
            "deleted": "🗑 حذف‌شده",
        }.items():
            if counts and key in counts:
                lines.append(f"{label}: {counts[key]}")
        await message.answer("\n".join(lines), reply_markup=admin_reply_menu())
        return
    if query in admin_menu_labels("admin", "warnings"):
        await _render_warnings_list(message, edit=False, admin_uid=uid)
        return
    if query in admin_menu_labels("admin", "local_alerts"):
        await _render_local_alerts_list(message, edit=False, admin_uid=uid)
        return
    if query in admin_menu_labels("admin", "ownership"):
        with get_session() as session:
            unlinked = repo.count_unlinked_subscriptions(session)
            repo.set_state(session, f"admin_flow:{uid}", "")
            ownership_text = repo.get_bot_message(
                session, "admin.ownership_prompt", unlinked=unlinked
            )
        await message.answer(ownership_text, reply_markup=ownership_menu())
        return
    if query in admin_menu_labels("admin", "search"):
        with get_session() as session:
            repo.set_state(session, f"admin_flow:{uid}", "svc_search")
            search_prompt = repo.get_bot_message(session, "admin.search_prompt")
        await message.answer(search_prompt, reply_markup=admin_reply_menu())
        return
    if query in admin_menu_labels("admin", "create"):
        with get_session() as session:
            repo.set_state(session, f"admin_flow:{uid}", "create_wait_username")
            create_prompt = repo.get_bot_message(session, "admin.create_prompt")
        await message.answer(create_prompt)
        return
    if query in admin_menu_labels("admin", "queue"):
        from app.telegram.handlers.renew import _render_queue_message

        await _render_queue_message(message)
        return
    if query == BTN_PRICES:
        await _send_prices(message)
        return
    if query == BTN_CARD:
        await _send_card_info(message, settings)
        return
    if query in admin_menu_labels("admin", "settings"):
        with get_session() as session:
            settings_prompt = repo.get_bot_message(session, "admin.settings_prompt")
        await message.answer(settings_prompt, reply_markup=settings_menu())
        return
    if query in admin_menu_labels("admin", "requests"):
        with get_session() as session:
            requests_prompt = repo.get_bot_message(session, "admin.requests_prompt")
        await message.answer(requests_prompt, reply_markup=requests_menu())
        return
    if query in admin_menu_labels("admin", "sync"):
        from app.telegram.menu_config import build_admin_inline_menu

        with get_session() as session:
            sync_prompt = repo.get_bot_message(session, "admin.sync_prompt")
        await message.answer(
            sync_prompt,
            reply_markup=build_admin_inline_menu("sync"),
        )
        return
    if query in {BTN_VENDOR, "🏪 NetPlus"}:
        await message.answer(
            f"🏪 فروشنده: <b>NetPlus</b>\n@{settings.netplus_bot.lstrip('@')}",
            reply_markup=admin_reply_menu(),
        )
        return
    if query in admin_menu_labels("admin", "help"):
        with get_session() as session:
            help_text = repo.get_bot_message(session, "admin.help")
        await message.answer(
            help_text,
            reply_markup=admin_reply_menu(),
        )
        return

    # Manual unique-amount confirm (same idea as NetPlus auto-match)
    if query.startswith("تایید مبلغ") or query.lower().startswith("confirm amount"):
        parts = query.replace("تایید مبلغ", "").replace("confirm amount", "")
        digits = "".join(ch for ch in parts if ch.isdigit())
        if len(digits) < 4:
            await message.answer("مثال: تایید مبلغ 1500100")
            return
        result = await auto_confirm_by_amount(digits, source="admin_manual")
        if not result.get("ok"):
            await message.answer(f"سفارشی با این مبلغ پیدا نشد ({digits}).")
            return
        await message.answer(
            f"✅ مبلغ مچ شد → سفارش #{result['order_id']}\n"
            "دکمه «انجام در NetPlus» را از پیام تایید بزنید (یا همان‌جا تایید کنید)."
        )
        return

    with get_session() as session:
        flow = repo.get_state(session, f"admin_flow:{uid}")

    if flow == "card_edit_number":
        digits = "".join(ch for ch in query if ch.isdigit())
        if len(digits) < 16:
            await message.answer("شماره کارت معتبر نیست (حداقل ۱۶ رقم).")
            return
        with get_session() as session:
            repo.set_state(session, f"admin_card_tmp:{uid}", digits)
            repo.set_state(session, f"admin_flow:{uid}", "card_edit_holder")
        await message.answer("نام دارنده کارت را بفرستید:")
        return

    if flow == "card_edit_holder":
        with get_session() as session:
            number = repo.get_state(session, f"admin_card_tmp:{uid}")
            repo.set_payment_card(session, number, query)
            repo.set_state(session, f"admin_flow:{uid}", "")
            repo.set_state(session, f"admin_card_tmp:{uid}", "")
        await message.answer(
            f"✅ کارت مشتریان ذخیره شد.\n<code>{number}</code>\nبه نام: <b>{query}</b>",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow == "invoice_desc_edit":
        with get_session() as session:
            saved = repo.set_invoice_description(session, query)
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer(
            f"✅ متن فاکتور ذخیره شد:\n<b>{saved}</b>",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow == "pricing_text_edit":
        with get_session() as session:
            saved = repo.set_pricing_text(session, query)
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer(
            "✅ متن تعرفه مشتری ذخیره شد.",
            reply_markup=admin_reply_menu(),
        )
        await message.answer(saved)
        return

    from app.telegram.handlers.admin_rates import handle_rates_admin_text

    if await handle_rates_admin_text(message, uid, flow, query):
        return

    if flow == "channel_edit":
        from app.telegram.channel_gate import set_required_channel

        ch = set_required_channel(query)
        with get_session() as session:
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer(
            f"✅ کانال اجباری: @{ch}",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow.startswith("edu_set_media:"):
        if query.strip() in {"-", "—"}:
            with get_session() as session:
                repo.set_state(session, f"admin_flow:{uid}", "")
            await message.answer("لغو شد.")
            return
        await message.answer("ویدیو/عکس/فایل بفرستید، نه متن.")
        return

    if flow == "userinfo_ttl_edit":
        raw = query.strip().replace(",", ".")
        try:
            minutes = float(raw)
        except ValueError:
            await message.answer("فقط عدد دقیقه بفرستید (مثال: 2).")
            return
        with get_session() as session:
            ttl = repo.set_userinfo_ttl_seconds(
                session, minutes_to_seconds(minutes)
            )
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer(
            f"✅ TTL بروزرسانی لینک: <code>{format_seconds_fa(ttl)}</code>",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow == "lifecycle_edit":
        parts = query.strip().split()
        if len(parts) < 2:
            await message.answer("مثال: days 3  یا  traffic 1024")
            return
        key = parts[0].lower()
        raw = parts[1].replace(",", ".")
        try:
            with get_session() as session:
                if key in ("days", "day"):
                    val = repo.set_alert_days_threshold(session, int(float(raw)))
                    msg = f"✅ آستانه روز: <code>{val}</code>"
                elif key in ("traffic", "mb", "volume"):
                    val = repo.set_alert_traffic_mb_threshold(session, float(raw))
                    msg = f"✅ آستانه حجم: <code>{val:g}</code> مگابایت"
                elif key in ("cooldown", "cool"):
                    val = repo.set_lifecycle_alert_cooldown_seconds(
                        session, minutes_to_seconds(raw)
                    )
                    msg = f"✅ فاصله تکرار هشدار: <code>{format_seconds_fa(val)}</code>"
                elif key == "scan":
                    val = repo.set_lifecycle_scan_interval_seconds(
                        session, minutes_to_seconds(raw)
                    )
                    msg = f"✅ بازه اسکن: <code>{format_seconds_fa(val)}</code>"
                else:
                    await message.answer("کلید نامعتبر. days / traffic / cooldown / scan")
                    return
                repo.set_state(session, f"admin_flow:{uid}", "")
        except ValueError:
            await message.answer("عدد نامعتبر است.")
            return
        await message.answer(msg, reply_markup=admin_reply_menu())
        return

    if flow == "wallet_limits_edit":
        parts = query.replace(",", "").replace("،", "").split()
        digits = [p for p in parts if p.isdigit()]
        if len(digits) < 2:
            await message.answer("مثال: 10000 600000")
            return
        with get_session() as session:
            mn, mx = repo.set_wallet_limits(session, int(digits[0]), int(digits[1]))
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer(
            f"✅ حد شارژ کیف پول: <code>{mn:,}</code> … <code>{mx:,}</code> تومان",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow.startswith("support_reply:"):
        cust_id = int(flow.split(":", 1)[1])
        with get_session() as session:
            ticket = repo.get_open_ticket_for_customer(session, cust_id)
            if ticket:
                repo.add_support_message(
                    session,
                    ticket.id,
                    sender="admin",
                    body=query,
                    admin_id=uid,
                )
                ticket_id = ticket.id
            else:
                ticket = repo.create_support_ticket(
                    session,
                    telegram_id=cust_id,
                    kind="support",
                    subject="پاسخ ادمین",
                    status="awaiting_customer",
                )
                repo.add_support_message(
                    session,
                    ticket.id,
                    sender="admin",
                    body=query,
                    admin_id=uid,
                )
                ticket_id = ticket.id
            repo.set_state(session, f"admin_flow:{uid}", "")
        from app.telegram import customer_bridge

        if not customer_bridge.customer_bot:
            await message.answer("ربات مشتری فعال نیست.")
            return
        try:
            await customer_bridge.customer_bot.send_message(
                cust_id,
                f"💬 <b>پاسخ پشتیبانی</b>\n\n{query}",
            )
            # Arm customer ticket reply flow
            with get_session() as session:
                repo.set_state(session, f"cust_flow:{cust_id}", f"ticket:{ticket_id}")
            await message.answer(
                f"✅ پاسخ برای <code>{cust_id}</code> ارسال شد (تیکت #{ticket_id}).",
                reply_markup=admin_reply_menu(),
            )
        except Exception:
            await message.answer(
                "ارسال پاسخ ناموفق بود (کاربر ربات را استارت نکرده؟).",
                reply_markup=admin_reply_menu(),
            )
        return

    if flow.startswith("ticket_reply:"):
        ticket_id = int(flow.split(":", 1)[1])
        with get_session() as session:
            ticket = repo.get_support_ticket(session, ticket_id)
            if not ticket:
                repo.set_state(session, f"admin_flow:{uid}", "")
                await message.answer("تیکت یافت نشد.")
                return
            repo.add_support_message(
                session,
                ticket_id,
                sender="admin",
                body=query,
                admin_id=uid,
            )
            cust_id = ticket.telegram_id
            repo.set_state(session, f"admin_flow:{uid}", "")
            repo.set_state(session, f"cust_flow:{cust_id}", f"ticket:{ticket_id}")
        from app.telegram import customer_bridge

        if customer_bridge.customer_bot:
            try:
                await customer_bridge.customer_bot.send_message(
                    cust_id,
                    f"💬 <b>پاسخ پشتیبانی</b> (تیکت #{ticket_id})\n\n{query}",
                )
            except Exception:
                await message.answer("ارسال به مشتری ناموفق بود.")
                return
        await message.answer(
            f"✅ پاسخ تیکت #{ticket_id} ارسال شد.",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow.startswith("reject_reason:"):
        order_id = int(flow.split(":", 1)[1])
        reason = query.strip()
        if len(reason) < 3:
            await message.answer("دلیل را کامل‌تر بنویسید.")
            return
        with get_session() as session:
            order = repo.get_customer_order(session, order_id)
            if not order:
                repo.set_state(session, f"admin_flow:{uid}", "")
                await message.answer("سفارش یافت نشد.")
                return
            repo.refund_wallet_applied(session, order)
            order.status = "rejected"
            order.note = (order.note or "") + f"|reject:{reason[:200]}"
            repo.touch_customer_order(order)
            cust_tg = order.telegram_id
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
                admin_id=uid,
            )
            notice = repo.get_bot_message(
                session, "reject_customer_notice", order=order_id, reason=reason
            )
            repo.set_state(session, f"admin_flow:{uid}", "")
            repo.set_state(session, f"cust_flow:{cust_tg}", f"ticket:{ticket.id}")
            ticket_id = ticket.id
        from app.telegram import customer_bridge
        from app.services.admin_order_actions import cleanup_tracked_admin_order_ui
        from app.telegram.handlers.customer import cleanup_customer_order_ui

        try:
            await cleanup_customer_order_ui(cust_tg, order_id)
        except Exception:
            pass
        try:
            await cleanup_tracked_admin_order_ui(
                order_id,
                summary=f"❌ سفارش #{order_id} رد شد",
            )
        except Exception:
            pass
        if customer_bridge.customer_bot:
            try:
                await customer_bridge.customer_bot.send_message(cust_tg, notice)
            except Exception:
                pass
        await message.answer(
            f"❌ سفارش #{order_id} رد شد.\nتیکت #{ticket_id} باز شد و دلیل برای مشتری ارسال شد.",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow.startswith("bot_msg_edit:"):
        key = flow.split(":", 1)[1]
        with get_session() as session:
            saved = repo.set_bot_message(session, key, query)
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer(
            f"✅ پیام <code>{key}</code> ذخیره شد.\n\n{saved}",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow == "reminders_edit":
        low = query.lower().strip()
        with get_session() as session:
            if low.startswith("ttl"):
                raw = query.split(None, 1)[1] if " " in query else ""
                try:
                    sec = minutes_to_seconds(raw)
                except ValueError:
                    await message.answer("مثال: ttl 1440  (دقیقه)")
                    return
                val = repo.set_order_ttl_seconds(session, sec)
                repo.set_state(session, f"admin_flow:{uid}", "")
                await message.answer(
                    f"✅ مهلت سفارش: {format_seconds_fa(val)}",
                    reply_markup=admin_reply_menu(),
                )
                return
            if low.startswith("remind"):
                raw = query.split(None, 1)[1] if " " in query else ""
                from app.utils.duration import parse_minutes_list

                parts = parse_minutes_list(raw)
                if not parts:
                    await message.answer("مثال: remind 5,60,360  (دقیقه)")
                    return
                delays = repo.set_reminder_delays_seconds(session, parts)
                repo.set_state(session, f"admin_flow:{uid}", "")
                await message.answer(
                    f"✅ یادآوری‌ها: {format_seconds_list_fa(delays)}",
                    reply_markup=admin_reply_menu(),
                )
                return
        await message.answer("فرمت: ttl 1440  یا  remind 5,60,360  (دقیقه)")
        return

    if flow == "maintenance_msg_edit":
        text = (query or "").strip()
        if not text:
            await message.answer("پیام خالی نباشد.")
            return
        with get_session() as session:
            currently_on = repo.is_customer_bot_enabled(session)
            repo.set_customer_bot_maintenance(
                session, enabled=currently_on, message=text
            )
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer(
            "✅ پیام زمان خاموشی ذخیره شد.",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow == "edu_add_cat":
        with get_session() as session:
            cat = repo.create_education_category(session, query)
            repo.set_state(session, f"admin_flow:{uid}", "")
            cid = cat.id
        await message.answer(
            f"✅ دسته «{query}» ساخته شد (#{cid}).",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow.startswith("edu_add_lesson:"):
        cat_id = int(flow.split(":", 1)[1])
        lines = query.splitlines()
        title = (lines[0] if lines else "درس").strip()[:128]
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        if not body:
            await message.answer("خط اول عنوان، خطوط بعدی متن درس.")
            return
        with get_session() as session:
            lesson = repo.create_education_lesson(
                session, cat_id, title, body
            )
            repo.set_state(session, f"admin_flow:{uid}", "")
            lid = lesson.id
        await message.answer(
            f"✅ درس «{title}» ذخیره شد (#{lid}).",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow.startswith("price_edit:"):
        # price_edit:ID  or  price_edit:friends:ID
        bits = flow.split(":")
        tier = "default"
        if len(bits) >= 3 and bits[1] in ("friends", "colleagues", "default"):
            tier = bits[1]
            plan_id = int(bits[2])
        else:
            plan_id = int(bits[1])
        raw = query.replace(",", "").replace("،", "").strip()
        if raw in {"-", "—", "clear", "پاک"}:
            if tier == "default":
                await message.answer("نرخ عادی را نمی‌توان پاک کرد؛ عدد بفرستید.")
                return
            with get_session() as session:
                plan = repo.set_plan_tier_price(
                    session, plan_id, tier=tier, price_toman=None
                )
                repo.set_state(session, f"admin_flow:{uid}", "")
                title = plan.title if plan else "?"
            await message.answer(
                f"✅ نرخ ویژه «{repo.RATE_TIER_LABELS.get(tier, tier)}» "
                f"برای <b>{title}</b> پاک شد (همان نرخ عادی).",
                reply_markup=admin_reply_menu(),
            )
            return
        if not raw.isdigit():
            await message.answer("فقط عدد تومان بفرستید.")
            return
        with get_session() as session:
            if tier == "default":
                plan = repo.set_price_toman(session, plan_id, int(raw))
            else:
                plan = repo.set_plan_tier_price(
                    session, plan_id, tier=tier, price_toman=int(raw)
                )
            repo.set_state(session, f"admin_flow:{uid}", "")
            title = plan.title if plan else "?"
            price = int(raw)
        await message.answer(
            f"✅ قیمت «{repo.RATE_TIER_LABELS.get(tier, 'عادی')}» "
            f"<b>{title}</b>: <code>{price:,}</code> تومان",
            reply_markup=admin_reply_menu(),
        )
        return

    if flow == "bulk_wait_tg":
        digits = "".join(ch for ch in query if ch.isdigit())
        if len(digits) < 5:
            await message.answer("شناسه تلگرام معتبر نیست.")
            return
        to_tg = int(digits)
        with get_session() as session:
            profile = repo.get_customer_profile(session, to_tg)
            n = repo.count_unlinked_subscriptions(session)
            repo.set_state(session, f"admin_flow:{uid}", "")
        if not profile:
            await message.answer(
                f"پروفایل <code>{to_tg}</code> نیست. مشتری باید ربات را استارت کند.",
                reply_markup=admin_reply_menu(),
            )
            return
        if n == 0:
            await message.answer(
                "سرویس بدون مالکی برای اختصاص نیست.",
                reply_markup=admin_reply_menu(),
            )
            return
        await message.answer(
            f"تایید اختصاص <b>{n}</b> سرویس بدون‌مالک به <code>{to_tg}</code>؟",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ تایید دسته‌جمعی",
                            callback_data=f"own:bulk_ok:{to_tg}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🚫 انصراف", callback_data="menu:home"
                        )
                    ],
                ]
            ),
        )
        return

    if flow == "svc_search":
        with get_session() as session:
            repo.set_state(session, f"admin_flow:{uid}", "")
        await _render_admin_services_page(
            message, page=0, edit=False, query=query, admin_uid=uid
        )
        return

    if flow == "assign_wait_username":
        with get_session() as session:
            exact = repo.get_subscription_by_username(session, query)
            if exact:
                rows = [exact]
            else:
                rows = repo.search_subscriptions(session, query, limit=8)
            if not rows:
                repo.set_state(session, f"admin_flow:{uid}", "")
                await message.answer("سرویس یافت نشد.", reply_markup=admin_reply_menu())
                return
            if len(rows) == 1:
                sub = rows[0]
                owner = _owner_line(session, sub)
                repo.set_state(session, f"admin_flow:{uid}", f"assign_wait_tg:{sub.id}")
                uname = sub.username
                await message.answer(
                    f"سرویس: <code>{uname}</code>\n{owner}\n"
                    "شناسه تلگرام مشتری مقصد را بفرستید:",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="⬅️ بازگشت به لیست",
                                    callback_data="own:one",
                                )
                            ]
                        ]
                    ),
                )
                return
            repo.set_state(session, f"admin_flow:{uid}", "")
            lines = ["چند سرویس مشابه پیدا شد؛ یکی را دقیق انتخاب کنید:"]
            kb = []
            for sub in rows:
                kb.append(
                    [
                        InlineKeyboardButton(
                            text=f"{_vendor_list_icon(session, sub)} {sub.username} {_vendor_list_icon(session, sub)}",
                            callback_data=f"own:as:{sub.id}",
                        )
                    ]
                )
            kb.append(
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت به لیست", callback_data="own:one"
                    )
                ]
            )
        await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        return

    if flow.startswith("assign_page_wait_tg"):
        digits = "".join(ch for ch in query if ch.isdigit())
        if len(digits) < 5:
            await message.answer("شناسه تلگرام معتبر نیست.")
            return
        to_tg = int(digits)
        page = 0
        if ":" in flow:
            try:
                page = int(flow.rsplit(":", 1)[1])
            except ValueError:
                page = 0
        try:
            with get_session() as session:
                if not repo.get_customer_profile(session, to_tg):
                    repo.set_state(session, f"admin_flow:{uid}", "")
                    raise ValueError("recipient_profile_missing")
                raw = (
                    repo.get_state(session, f"admin_own_page_ids:{uid}") or "[]"
                ).strip()
                try:
                    ids = [int(x) for x in json.loads(raw)]
                except (json.JSONDecodeError, TypeError, ValueError):
                    ids = []
                if not ids:
                    rows, _total = repo.list_unlinked_subscriptions_page(
                        session, offset=page * PAGE_SIZE, limit=PAGE_SIZE
                    )
                    ids = [int(s.id) for s in rows]
                if not ids:
                    repo.set_state(session, f"admin_flow:{uid}", "")
                    raise ValueError("empty_page")
                links = repo.assign_subscriptions_to_owner(
                    session,
                    subscription_ids=ids,
                    to_telegram_id=to_tg,
                    initiated_by_telegram_id=uid,
                    note="admin_page_ui",
                )
                n = len(links)
                repo.set_state(session, f"admin_flow:{uid}", "")
                repo.set_state(session, f"admin_own_page_ids:{uid}", "[]")
        except ValueError as exc:
            with get_session() as session:
                repo.set_state(session, f"admin_flow:{uid}", "")
            if str(exc) == "recipient_profile_missing":
                await message.answer(
                    f"پروفایل <code>{to_tg}</code> نیست. مشتری باید ربات را استارت کند.",
                    reply_markup=admin_reply_menu(),
                )
            elif str(exc) == "empty_page":
                await message.answer(
                    "سرویسی در این صفحه برای انتقال نیست.",
                    reply_markup=admin_reply_menu(),
                )
            else:
                await message.answer(f"ناموفق: {exc}", reply_markup=admin_reply_menu())
            return
        await message.answer(
            f"✅ <b>{n}</b> سرویس این صفحه → <code>{to_tg}</code>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="👤 ادامه اختصاص تکی",
                            callback_data="own:one",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ مالکیت", callback_data="menu:ownership"
                        )
                    ],
                ]
            ),
        )
        return

    if flow.startswith("assign_wait_tg"):
        digits = "".join(ch for ch in query if ch.isdigit())
        if len(digits) < 5:
            await message.answer("شناسه تلگرام معتبر نیست.")
            return
        to_tg = int(digits)
        sub_id = None
        if ":" in flow:
            try:
                sub_id = int(flow.split(":", 1)[1])
            except ValueError:
                sub_id = None
        try:
            with get_session() as session:
                from app.database.models import Subscription

                if not repo.get_customer_profile(session, to_tg):
                    repo.set_state(session, f"admin_flow:{uid}", "")
                    raise ValueError("recipient_profile_missing")
                sub = session.get(Subscription, sub_id) if sub_id else None
                if not sub:
                    repo.set_state(session, f"admin_flow:{uid}", "")
                    raise ValueError("subscription_not_found")
                had_owner = bool(
                    repo.get_link_for_subscription(session, sub.id)
                    or repo.get_link_for_username(session, sub.username)
                )
                link = repo.set_local_owner(
                    session,
                    to_telegram_id=to_tg,
                    initiated_by_telegram_id=uid,
                    source="admin_reassign" if had_owner else "admin_assign",
                    subscription=sub,
                    note="admin_ui",
                )
                uname = sub.username
                link_id = link.id
                repo.set_state(session, f"admin_flow:{uid}", "")
        except ValueError as exc:
            with get_session() as session:
                repo.set_state(session, f"admin_flow:{uid}", "")
            if str(exc) == "recipient_profile_missing":
                await message.answer(
                    f"پروفایل <code>{to_tg}</code> نیست. مشتری باید ربات را استارت کند.",
                    reply_markup=admin_reply_menu(),
                )
            else:
                await message.answer(f"ناموفق: {exc}", reply_markup=admin_reply_menu())
            return
        await message.answer(
            f"✅ مالک <code>{uname}</code> → <code>{to_tg}</code>\n"
            f"لینک #{link_id}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="👤 ادامه اختصاص تکی",
                            callback_data="own:one",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ مالکیت", callback_data="menu:ownership"
                        )
                    ],
                ]
            ),
        )
        return

    if flow == "create_wait_username":
        from app.utils.text import sanitize_service_username

        cleaned = sanitize_service_username(query)
        if not cleaned:
            await message.answer(
                "❌ نام کاربری نامعتبر است.\n"
                "فقط حروف انگلیسی، عدد و _ (۳ تا ۳۲ کاراکتر؛ "
                "شروع/پایان با _ نباشد).\n"
                "دوباره بفرستید:"
            )
            return
        with get_session() as session:
            repo.set_state(session, f"admin_create_user:{uid}", cleaned)
            repo.set_state(session, f"admin_create_owner:{uid}", "")
            repo.set_state(session, f"admin_flow:{uid}", "create_wait_owner")
        await message.answer(
            f"خرید برای <code>{cleaned}</code>\n\n"
            "شناسه تلگرام مالک مشتری را بفرستید "
            "(برای ارسال لینک/QR بعد از ساخت).\n"
            "اگر مالک ندارید، <code>-</code> بفرستید.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⏭ بدون مالک",
                            callback_data="create:owner:skip",
                        )
                    ],
                    [InlineKeyboardButton(text="🚫 انصراف", callback_data="menu:home")],
                ]
            ),
        )
        return

    if flow == "create_wait_owner":
        digits = "".join(ch for ch in query if ch.isdigit())
        skip = query.strip() in {"-", "—", "no", "skip", "."}
        if not skip:
            if len(digits) < 5:
                await message.answer(
                    "شناسه نامعتبر است. عدد تلگرام را بفرستید یا <code>-</code> برای رد شدن."
                )
                return
            with get_session() as session:
                if not repo.get_customer_profile(session, int(digits)):
                    await message.answer(
                        "این شناسه در ربات مشتری ثبت نشده "
                        "(حداقل یک‌بار /start زده باشد). دوباره بفرستید یا <code>-</code>."
                    )
                    return
                repo.set_state(session, f"admin_create_owner:{uid}", digits)
                repo.set_state(session, f"admin_flow:{uid}", "create_wait_duration")
            owner_line = f"مالک: <code>{digits}</code>"
        else:
            with get_session() as session:
                repo.set_state(session, f"admin_create_owner:{uid}", "")
                repo.set_state(session, f"admin_flow:{uid}", "create_wait_duration")
            owner_line = "بدون مالک مشتری"
        uname = ""
        with get_session() as session:
            uname = repo.get_state(session, f"admin_create_user:{uid}") or "—"
        from app.telegram.handlers.renew import _create_plans_keyboard

        await message.answer(
            f"خرید برای <code>{uname}</code>\n{owner_line}\n"
            "پلن را از کاتالوگ مشتری انتخاب کنید:",
            reply_markup=_create_plans_keyboard(),
        )
        return

    await _render_admin_services_page(
        message, page=0, edit=False, query=query, admin_uid=uid
    )
