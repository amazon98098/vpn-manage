"""Shared reply / inline keyboards for admin bot."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.telegram.menu_config import build_reply_menu, label_for, labels_for

# Persistent bottom menu — defaults; live labels come from menu_config / panel
BTN_START = "🏠 شروع"  # legacy label; no longer shown on reply keyboard
BTN_STATUS = "📊 وضعیت"
BTN_WARNINGS = "⚠️ هشدار فروشنده"
BTN_LOCAL_ALERTS = "🪫 نزدیک اتمام"
BTN_SERVICES = "🛍 سرویس های من"
BTN_SEARCH = "🔍 جستجو"
BTN_CREATE = "🛒 خرید جدید"
BTN_QUEUE = "📋 صف سفارش‌ها"
BTN_REQUESTS = "📥 درخواست‌ها"
BTN_SETTINGS = "⚙️ تنظیمات"
BTN_SYNC = "🔄 همگام‌سازی"
BTN_OWNERSHIP = "👤 مالکیت"
BTN_HELP = "ℹ️ راهنما"

# Kept for backward compatibility with older handler branches
BTN_PRICES = "💰 قیمت‌ها"
BTN_CARD = "💳 کارت مشتریان"
BTN_VENDOR = "🏪 NetPlus"


def admin_reply_menu() -> ReplyKeyboardMarkup:
    return build_reply_menu("admin")


def admin_labels(action: str) -> set[str]:
    return labels_for("admin", action)


def admin_label(action: str) -> str:
    return label_for("admin", action)
