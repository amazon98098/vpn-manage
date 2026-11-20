"""Format Persian admin notifications for NetPlus alerts."""
from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.utils.text import format_mb


KIND_LABELS = {
    "alert_days": "⏱ هشدار زمان",
    "alert_mb": "📉 هشدار حجم",
    "alert_deleted": "🗑 حذف اشتراک",
}


def format_alert(parsed: dict[str, Any], vendor_name: str = "NetPlus") -> str:
    kind = parsed.get("kind", "unknown")
    title = KIND_LABELS.get(kind, "🔔 اعلان")
    lines = [
        f"<b>{title}</b>",
        f"فروشنده: {vendor_name}",
        f"نام کاربری: <code>{parsed.get('username') or 'نامشخص'}</code>",
    ]
    if parsed.get("remaining_days") is not None:
        lines.append(f"روز باقی‌مانده: {parsed['remaining_days']}")
    if parsed.get("remaining_traffic_mb") is not None:
        lines.append(f"حجم باقی‌مانده: {format_mb(parsed['remaining_traffic_mb'])}")
    if parsed.get("raw_summary"):
        lines.append(f"خلاصه: {parsed['raw_summary']}")
    if parsed.get("vendor_service_id"):
        lines.append(f"شناسه داخلی: <code>{parsed['vendor_service_id']}</code>")
    return "\n".join(lines)


def deleted_service_customer_keyboard() -> InlineKeyboardMarkup:
    """After non-renewal delete: buy new + home (no renew / no details)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 خرید سرویس جدید",
                    callback_data="cust:go:buy",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 بازگشت به منوی اصلی",
                    callback_data="cust:home",
                )
            ],
        ]
    )


def deleted_service_admin_keyboard() -> InlineKeyboardMarkup:
    """Admin bot after vendor delete: create new + home (no renew / no DB details)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 خرید سرویس جدید",
                    callback_data="menu:create",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 بازگشت به منوی اصلی",
                    callback_data="menu:home",
                )
            ],
        ]
    )


def alert_keyboard(
    username: str | None, *, kind: str | None = None
) -> InlineKeyboardMarkup | None:
    if kind == "alert_deleted":
        return deleted_service_admin_keyboard()
    if not username:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 جزئیات در دیتابیس",
                    callback_data=f"subinfo:{username[:40]}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="♻️ تمدید",
                    callback_data=f"renew:ask:{username[:40]}",
                )
            ],
        ]
    )
