"""Configurable Telegram menus (main + inner), editable from the web panel."""
from __future__ import annotations

import json
import logging
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.database import repositories as repo
from app.database.database import get_session

log = logging.getLogger(__name__)

# ─── Customer main reply keyboard ─────────────────────────────────────────────

CUSTOMER_ACTIONS: dict[str, dict[str, Any]] = {
    "buy": {
        "fa_name": "خرید اشتراک",
        "label": "🛒 خرید اشتراک",
        "aliases": ["🛒 خرید اشتراک"],
    },
    "mine": {
        "fa_name": "سرویس‌های من",
        "label": "🛍 سرویس های من",
        "aliases": ["🛍 سرویس های من", "🛍 سرویس‌های من", "✨ سرویس‌های من"],
    },
    "wallet": {
        "fa_name": "کیف پول",
        "label": "💰 کیف پول",
        "aliases": ["💰 کیف پول", "🏦 کیف پول + شارژ"],
    },
    "prices": {
        "fa_name": "تعرفه‌ها",
        "label": "💵 تعرفه‌ها",
        "aliases": ["💵 تعرفه‌ها", "💰 تعرفه‌ها"],
    },
    "guide": {
        "fa_name": "آموزش",
        "label": "📚 آموزش",
        "aliases": ["📚 آموزش"],
    },
    "support": {
        "fa_name": "پشتیبانی",
        "label": "💬 پشتیبانی",
        "aliases": ["💬 پشتیبانی", "🆘 پشتیبانی"],
    },
    "referral": {
        "fa_name": "مجموعه‌گیری",
        "label": "🎁 مجموعه‌گیری",
        "aliases": ["🎁 مجموعه‌گیری", "مجموعه‌گیری"],
        # Present in catalog for later enable; omitted from default layout while off.
        "enabled": False,
    },
    "hide_menu": {
        "fa_name": "مخفی کردن منو",
        "label": "⬇️ مخفی کردن منو",
        "aliases": ["⬇️ مخفی کردن منو", "مخفی کردن منو", "⬇️ منو"],
    },
    "show_menu": {
        "fa_name": "نمایش منو",
        "label": "⬆️ نمایش منو",
        "aliases": ["⬆️ نمایش منو", "نمایش منو", "⬆️ منو"],
    },
}

DEFAULT_CUSTOMER_LAYOUT: list[list[str]] = [
    ["buy", "mine"],
    ["wallet", "prices"],
    ["guide", "support"],
    # "referral" stays off until explicitly enabled in layout/settings.
    ["hide_menu"],
]

# ─── Customer support submenu (reply keyboard) ────────────────────────────────

CUSTOMER_SUPPORT_ACTIONS: dict[str, dict[str, Any]] = {
    "support_send": {
        "fa_name": "ارسال پیام",
        "label": "🎟 ارسال پیام به پشتیبانی",
        "aliases": ["🎟 ارسال پیام به پشتیبانی"],
    },
    "my_tickets": {
        "fa_name": "تیکت‌های من",
        "label": "🎫 تیکت‌های من",
        "aliases": ["🎫 تیکت‌های من"],
    },
    "back": {
        "fa_name": "بازگشت",
        "label": "⬅️ بازگشت به منو",
        "aliases": ["⬅️ بازگشت به منو"],
    },
}

DEFAULT_CUSTOMER_SUPPORT_LAYOUT: list[list[str]] = [
    ["support_send"],
    ["my_tickets"],
    ["back"],
]

# ─── Admin main reply keyboard ────────────────────────────────────────────────

ADMIN_ACTIONS: dict[str, dict[str, Any]] = {
    "status": {
        "fa_name": "وضعیت",
        "label": "📊 وضعیت",
        "aliases": ["📊 وضعیت", "📊 وضعیت کلی"],
    },
    "warnings": {
        "fa_name": "هشدار فروشنده",
        "label": "⚠️ هشدار فروشنده",
        "aliases": ["⚠️ هشدار فروشنده", "⚠️ هشدارها"],
    },
    "local_alerts": {
        "fa_name": "نزدیک اتمام",
        "label": "🪫 نزدیک اتمام",
        "aliases": ["🪫 نزدیک اتمام"],
    },
    "services": {
        "fa_name": "سرویس‌ها",
        "label": "🛍 سرویس های من",
        "aliases": ["🛍 سرویس های من", "🛍 سرویس‌ها", "✨ سرویس‌ها"],
    },
    "search": {
        "fa_name": "جستجو",
        "label": "🔍 جستجو",
        "aliases": ["🔍 جستجو", "🔎 جستجو"],
    },
    "create": {
        "fa_name": "خرید جدید",
        "label": "🛒 خرید جدید",
        "aliases": ["🛒 خرید جدید"],
    },
    "queue": {
        "fa_name": "صف سفارش‌ها",
        "label": "📋 صف سفارش‌ها",
        "aliases": ["📋 صف سفارش‌ها"],
    },
    "requests": {
        "fa_name": "درخواست‌ها",
        "label": "📥 درخواست‌ها",
        "aliases": ["📥 درخواست‌ها"],
    },
    "settings": {
        "fa_name": "تنظیمات",
        "label": "⚙️ تنظیمات",
        "aliases": ["⚙️ تنظیمات"],
    },
    "sync": {
        "fa_name": "همگام‌سازی",
        "label": "🔄 همگام‌سازی",
        "aliases": ["🔄 همگام‌سازی", "🔄 همگام‌سازی NetPlus"],
    },
    "ownership": {
        "fa_name": "مالکیت",
        "label": "👤 مالکیت",
        "aliases": ["👤 مالکیت", "👤 مالکیت سرویس"],
    },
    "help": {
        "fa_name": "راهنما",
        "label": "ℹ️ راهنما",
        "aliases": ["ℹ️ راهنما"],
    },
}

DEFAULT_ADMIN_LAYOUT: list[list[str]] = [
    ["status"],
    ["warnings", "local_alerts"],
    ["services", "search"],
    ["create", "queue"],
    ["requests", "settings"],
    ["sync", "ownership"],
    ["help"],
]

# ─── Admin inline inner menus (labels only; callbacks fixed) ───────────────────

ADMIN_INLINE_MENUS: dict[str, dict[str, Any]] = {
    "settings": {
        "fa_title": "منوی تنظیمات (اینلاین)",
        "category": "settings",
        "items": [
            {"id": "messages", "label": "📝 پیام‌ها و محتوا", "callback": "settings:messages"},
            {"id": "education", "label": "📚 آموزش", "callback": "settings:education"},
            {"id": "prices", "label": "💰 پلن و قیمت‌ها", "callback": "menu:prices"},
            {"id": "pricing_text", "label": "📄 متن تعرفه مشتری", "callback": "menu:pricing_text"},
            {"id": "card", "label": "💳 پرداخت / کارت", "callback": "menu:card"},
            {"id": "wallet", "label": "💰 کیف پول", "callback": "wallet_limits:edit"},
            {"id": "reminders", "label": "🔔 یادآوری و انقضا", "callback": "settings:reminders"},
            {"id": "userinfo_ttl", "label": "⏱ TTL بروزرسانی لینک", "callback": "settings:userinfo_ttl"},
            {"id": "refresh_strategy", "label": "🔄 استراتژی بروزرسانی سرویس", "callback": "settings:refresh_strategy"},
            {"id": "approval", "label": "✅ تایید سفارش و کارت فروشنده", "callback": "settings:approval"},
            {"id": "rates", "label": "👥 نرخ دوستان/همکار و مجموعه‌گیری", "callback": "settings:rates"},
            {"id": "channel", "label": "📢 کانال اجباری مشتری", "callback": "settings:channel"},
            {"id": "lifecycle", "label": "📣 هشدار اتمام سرویس", "callback": "settings:lifecycle"},
            {"id": "maintenance", "label": "🛠 حالت تعمیر ربات مشتری", "callback": "settings:maintenance"},
            {"id": "vendors", "label": "🏪 فروشنده‌ها", "callback": "menu:vendor"},
            {"id": "back", "label": "⬅️ بازگشت", "callback": "menu:home"},
        ],
    },
    "requests": {
        "fa_title": "منوی درخواست‌ها (اینلاین)",
        "category": "requests",
        "items": [
            {"id": "awaiting_payment", "label": "⏳ منتظر پرداخت", "callback": "requests:bucket:awaiting_payment"},
            {"id": "awaiting_review", "label": "🔍 در انتظار بررسی", "callback": "requests:bucket:awaiting_review"},
            {"id": "awaiting_customer", "label": "💬 منتظر پاسخ مشتری", "callback": "requests:bucket:awaiting_customer"},
            {"id": "done", "label": "✅ انجام‌شده / صف", "callback": "requests:bucket:done"},
            {"id": "rejected", "label": "🚫 رد شده", "callback": "requests:bucket:rejected"},
            {"id": "expired", "label": "⌛ منقضی", "callback": "requests:bucket:expired"},
            {"id": "clear", "label": "🧹 پاک‌کردن همه لیست‌ها", "callback": "requests:clear:all"},
            {"id": "back", "label": "⬅️ بازگشت", "callback": "menu:home"},
        ],
    },
    "ownership": {
        "fa_title": "منوی مالکیت (اینلاین)",
        "category": "ownership",
        "items": [
            {"id": "bulk", "label": "📦 اختصاص دسته‌جمعی", "callback": "own:bulk"},
            {"id": "one", "label": "👤 اختصاص تکی", "callback": "own:one"},
            {"id": "back", "label": "⬅️ بازگشت", "callback": "menu:home"},
        ],
    },
    "sync": {
        "fa_title": "منوی همگام‌سازی (اینلاین)",
        "category": "tools",
        "items": [
            {"id": "netplus", "label": "✨ همگام NetPlus", "callback": "sync:run:netplus"},
            {"id": "finearts", "label": "💎 همگام FineArts", "callback": "sync:run:finearts"},
            {"id": "back", "label": "⬅️ بازگشت", "callback": "menu:home"},
        ],
    },
}

# ─── Customer inline menus (labels; callbacks may include {u}/{id}) ────────────

CUSTOMER_INLINE_MENUS: dict[str, dict[str, Any]] = {
    "home": {
        "fa_title": "منوی اصلی اینلاین مشتری",
        "category": "home",
        "items": [
            {"id": "buy", "label": "🛒 خرید اشتراک", "callback": "cust:go:buy"},
            {"id": "mine", "label": "🛍 سرویس های من", "callback": "cust:go:mine"},
            {"id": "wallet", "label": "💰 کیف پول", "callback": "cust:go:wallet"},
            {"id": "prices", "label": "💵 تعرفه‌ها", "callback": "cust:go:prices"},
            {"id": "guide", "label": "📚 آموزش", "callback": "cust:go:guide"},
            {"id": "support", "label": "💬 پشتیبانی", "callback": "cust:go:support"},
        ],
        "layout": [["buy", "mine"], ["wallet", "prices"], ["guide", "support"]],
    },
    "service_actions": {
        "fa_title": "منوی جزئیات سرویس",
        "category": "services",
        "items": [
            {"id": "refresh", "label": "🔄 بروزرسانی", "callback": "cust:svcinfo:{u}"},
            {"id": "link", "label": "🔗 لینک اشتراک", "callback": "cust:svclink:{u}"},
            {"id": "change_link", "label": "🔐 تغییر لینک", "callback": "cust:svcchange:ask:{u}"},
            {"id": "renew", "label": "♻️ تمدید اشتراک", "callback": "cust:svcrenew:{u}"},
            {"id": "transfer", "label": "🤝 انتقال مالکیت", "callback": "cust:xfer:{u}"},
            {"id": "back_list", "label": "⬅️ بازگشت به لیست", "callback": "cust:svclist"},
        ],
        "layout": [
            ["refresh"],
            ["link", "change_link"],
            ["renew"],
            ["transfer"],
            ["back_list"],
        ],
    },
    "create_success": {
        "fa_title": "منوی بعد از خرید موفق",
        "category": "checkout",
        "items": [
            {"id": "ios", "label": "🍎 آیفون / iOS", "callback": "cust:setup:ios"},
            {"id": "android", "label": "🤖 اندروید", "callback": "cust:setup:android"},
            {"id": "home", "label": "🏠 بازگشت به منوی اصلی", "callback": "cust:home"},
        ],
        "layout": [["ios"], ["android"], ["home"]],
    },
    "renew_success": {
        "fa_title": "منوی بعد از تمدید موفق",
        "category": "checkout",
        "items": [
            {"id": "list", "label": "🛍 بازگشت به لیست سرویس ها", "callback": "cust:svclist"},
            {
                "id": "detail",
                "label": "✨ بازگشت به مشخصات سرویس",
                "callback": "cust:sid:{id}",
            },
        ],
        "layout": [["list"], ["detail"]],
    },
}

STATE_CUSTOMER_LAYOUT = "menu_customer_layout_json"
STATE_CUSTOMER_LABELS = "menu_customer_labels_json"
STATE_ADMIN_LAYOUT = "menu_admin_layout_json"
STATE_ADMIN_LABELS = "menu_admin_labels_json"
STATE_CUSTOMER_SUPPORT_LAYOUT = "menu_customer_support_layout_json"
STATE_CUSTOMER_SUPPORT_LABELS = "menu_customer_support_labels_json"
STATE_ADMIN_INLINE_LABELS = "menu_admin_inline_labels_json"
STATE_ADMIN_INLINE_ORDER = "menu_admin_inline_order_json"
STATE_CUSTOMER_INLINE_LABELS = "menu_customer_inline_labels_json"
STATE_CUSTOMER_INLINE_ORDER = "menu_customer_inline_order_json"


def _parse_json(raw: str, default: Any) -> Any:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _load_layout(state_key: str, default: list[list[str]]) -> list[list[str]]:
    with get_session() as session:
        data = _parse_json(repo.get_state(session, state_key), default)
    if not isinstance(data, list) or not data:
        return [list(row) for row in default]
    out: list[list[str]] = []
    for row in data:
        if isinstance(row, list) and row:
            out.append([str(x).strip() for x in row if str(x).strip()])
    return out or [list(row) for row in default]


def _load_labels(state_key: str) -> dict[str, str]:
    with get_session() as session:
        data = _parse_json(repo.get_state(session, state_key), {})
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()}


def load_layout(bot: str) -> list[list[str]]:
    if bot == "customer":
        return _load_layout(STATE_CUSTOMER_LAYOUT, DEFAULT_CUSTOMER_LAYOUT)
    if bot == "customer_support":
        return _load_layout(STATE_CUSTOMER_SUPPORT_LAYOUT, DEFAULT_CUSTOMER_SUPPORT_LAYOUT)
    return _load_layout(STATE_ADMIN_LAYOUT, DEFAULT_ADMIN_LAYOUT)


def load_labels(bot: str) -> dict[str, str]:
    if bot == "customer":
        return _load_labels(STATE_CUSTOMER_LABELS)
    if bot == "customer_support":
        return _load_labels(STATE_CUSTOMER_SUPPORT_LABELS)
    return _load_labels(STATE_ADMIN_LABELS)


def _load_inline_labels(state_key: str) -> dict[str, dict[str, str]]:
    with get_session() as session:
        data = _parse_json(repo.get_state(session, state_key), {})
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for menu_id, labels in data.items():
        if isinstance(labels, dict):
            out[str(menu_id)] = {
                str(k): str(v)
                for k, v in labels.items()
                if str(k) != "_order" and str(v).strip()
            }
    return out


def _load_inline_order(state_key: str) -> dict[str, list[str]]:
    with get_session() as session:
        data = _parse_json(repo.get_state(session, state_key), {})
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for menu_id, ids in data.items():
        if isinstance(ids, list):
            out[str(menu_id)] = [str(x).strip() for x in ids if str(x).strip()]
    return out


def load_admin_inline_labels() -> dict[str, dict[str, str]]:
    return _load_inline_labels(STATE_ADMIN_INLINE_LABELS)


def load_admin_inline_order() -> dict[str, list[str]]:
    return _load_inline_order(STATE_ADMIN_INLINE_ORDER)


def load_customer_inline_labels() -> dict[str, dict[str, str]]:
    return _load_inline_labels(STATE_CUSTOMER_INLINE_LABELS)


def load_customer_inline_order() -> dict[str, list[str]]:
    return _load_inline_order(STATE_CUSTOMER_INLINE_ORDER)


def save_reply_menu(bot: str, layout: list[list[str]], labels: dict[str, str]) -> None:
    if bot == "customer":
        layout_key, labels_key = STATE_CUSTOMER_LAYOUT, STATE_CUSTOMER_LABELS
    elif bot == "customer_support":
        layout_key, labels_key = (
            STATE_CUSTOMER_SUPPORT_LAYOUT,
            STATE_CUSTOMER_SUPPORT_LABELS,
        )
    else:
        layout_key, labels_key = STATE_ADMIN_LAYOUT, STATE_ADMIN_LABELS
    with get_session() as session:
        repo.set_state(session, layout_key, json.dumps(layout, ensure_ascii=False))
        repo.set_state(session, labels_key, json.dumps(labels, ensure_ascii=False))


def save_menu(bot: str, layout: list[list[str]], labels: dict[str, str]) -> None:
    """Back-compat alias for main customer/admin reply menus."""
    which = "admin" if bot == "admin" else "customer"
    save_reply_menu(which, layout, labels)


def save_admin_inline_labels(
    data: dict[str, dict[str, str]],
    order: dict[str, list[str]] | None = None,
) -> None:
    with get_session() as session:
        repo.set_state(
            session, STATE_ADMIN_INLINE_LABELS, json.dumps(data, ensure_ascii=False)
        )
        if order is not None:
            repo.set_state(
                session,
                STATE_ADMIN_INLINE_ORDER,
                json.dumps(order, ensure_ascii=False),
            )


def save_customer_inline_labels(
    data: dict[str, dict[str, str]],
    order: dict[str, list[str]] | None = None,
) -> None:
    with get_session() as session:
        repo.set_state(
            session, STATE_CUSTOMER_INLINE_LABELS, json.dumps(data, ensure_ascii=False)
        )
        if order is not None:
            repo.set_state(
                session,
                STATE_CUSTOMER_INLINE_ORDER,
                json.dumps(order, ensure_ascii=False),
            )


def reset_menu(bot: str) -> None:
    if bot == "customer":
        keys = [
            STATE_CUSTOMER_LAYOUT,
            STATE_CUSTOMER_LABELS,
            STATE_CUSTOMER_SUPPORT_LAYOUT,
            STATE_CUSTOMER_SUPPORT_LABELS,
            STATE_CUSTOMER_INLINE_LABELS,
            STATE_CUSTOMER_INLINE_ORDER,
        ]
    elif bot == "customer_support":
        keys = [STATE_CUSTOMER_SUPPORT_LAYOUT, STATE_CUSTOMER_SUPPORT_LABELS]
    elif bot == "admin_inline":
        keys = [STATE_ADMIN_INLINE_LABELS, STATE_ADMIN_INLINE_ORDER]
    elif bot == "admin":
        keys = [
            STATE_ADMIN_LAYOUT,
            STATE_ADMIN_LABELS,
            STATE_ADMIN_INLINE_LABELS,
            STATE_ADMIN_INLINE_ORDER,
        ]
    else:
        keys = [STATE_ADMIN_LAYOUT, STATE_ADMIN_LABELS]
    with get_session() as session:
        for key in keys:
            repo.set_state(session, key, "")


def actions_catalog(bot: str) -> dict[str, dict[str, Any]]:
    if bot == "customer":
        return CUSTOMER_ACTIONS
    if bot == "customer_support":
        return CUSTOMER_SUPPORT_ACTIONS
    return ADMIN_ACTIONS


def label_for(bot: str, action: str) -> str:
    catalog = actions_catalog(bot)
    meta = catalog.get(action) or {}
    overrides = load_labels(bot)
    return (overrides.get(action) or meta.get("label") or action).strip()


def labels_for(bot: str, action: str) -> set[str]:
    catalog = actions_catalog(bot)
    meta = catalog.get(action) or {}
    out = {label_for(bot, action)}
    for a in meta.get("aliases") or []:
        if a:
            out.add(str(a))
    default = meta.get("label")
    if default:
        out.add(str(default))
    return {x for x in out if x}


def build_reply_menu(bot: str) -> ReplyKeyboardMarkup:
    layout = load_layout(bot)
    catalog = actions_catalog(bot)
    rows: list[list[KeyboardButton]] = []
    for row in layout:
        btns: list[KeyboardButton] = []
        for action in row:
            meta = catalog.get(action)
            if not meta:
                continue
            if meta.get("enabled") is False:
                continue
            btns.append(KeyboardButton(text=label_for(bot, action)))
        if btns:
            rows.append(btns)
    if not rows:
        default = (
            DEFAULT_CUSTOMER_LAYOUT
            if bot == "customer"
            else DEFAULT_CUSTOMER_SUPPORT_LAYOUT
            if bot == "customer_support"
            else DEFAULT_ADMIN_LAYOUT
        )
        for row in default:
            row_btns: list[KeyboardButton] = []
            for a in row:
                meta = catalog.get(a)
                if not meta or meta.get("enabled") is False:
                    continue
                row_btns.append(KeyboardButton(text=label_for(bot, a)))
            if row_btns:
                rows.append(row_btns)
    placeholder = {
        "customer": "منو یا دستور…",
        "customer_support": "پشتیبانی…",
        "admin": "میانبر یا نام کاربری…",
    }.get(bot, "منو…")
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


def collapsed_customer_menu() -> ReplyKeyboardMarkup:
    """NetPlus-style: single toggle button instead of removing the keyboard."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label_for("customer", "show_menu"))]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="منو جمع شده…",
    )


def _ordered_inline_items(
    menu_id: str,
    catalog: dict[str, dict[str, Any]],
    *,
    labels: dict[str, dict[str, str]],
    order_map: dict[str, list[str]],
) -> list[dict[str, Any]]:
    meta = catalog.get(menu_id) or {}
    items = list(meta.get("items") or [])
    by_id = {item["id"]: item for item in items}
    overrides = labels.get(menu_id) or {}
    order = order_map.get(menu_id) or []
    ordered_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for iid in order:
        if iid in by_id and iid not in seen:
            ordered_items.append(by_id[iid])
            seen.add(iid)
    for item in items:
        if item["id"] not in seen:
            ordered_items.append(item)
    # Attach resolved label for callers.
    out = []
    for item in ordered_items:
        row = dict(item)
        row["resolved_label"] = (
            overrides.get(item["id"]) or item["label"]
        ).strip()
        out.append(row)
    return out


def build_admin_inline_menu(menu_id: str) -> InlineKeyboardMarkup:
    ordered = _ordered_inline_items(
        menu_id,
        ADMIN_INLINE_MENUS,
        labels=load_admin_inline_labels(),
        order_map=load_admin_inline_order(),
    )
    rows: list[list[InlineKeyboardButton]] = []
    for item in ordered:
        rows.append(
            [
                InlineKeyboardButton(
                    text=item["resolved_label"],
                    callback_data=item["callback"],
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_customer_inline_menu(
    menu_id: str,
    *,
    username: str = "",
    sub_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Build a customer inline menu with optional username/sub_id placeholders."""
    meta = CUSTOMER_INLINE_MENUS.get(menu_id) or {}
    ordered = _ordered_inline_items(
        menu_id,
        CUSTOMER_INLINE_MENUS,
        labels=load_customer_inline_labels(),
        order_map=load_customer_inline_order(),
    )
    by_id = {item["id"]: item for item in ordered}
    layout = meta.get("layout")
    u = (username or "")[:40]
    sid = "" if sub_id is None else str(int(sub_id))

    def _cb(template: str) -> str:
        return (
            str(template)
            .replace("{u}", u)
            .replace("{username}", u)
            .replace("{id}", sid)
            .replace("{sub_id}", sid)
        )

    rows: list[list[InlineKeyboardButton]] = []
    if isinstance(layout, list) and layout:
        for row_ids in layout:
            btns: list[InlineKeyboardButton] = []
            for iid in row_ids:
                item = by_id.get(iid)
                if not item:
                    continue
                cb = _cb(item["callback"])
                if "{u}" in item["callback"] or "{username}" in item["callback"]:
                    if not u:
                        continue
                if ("{id}" in item["callback"] or "{sub_id}" in item["callback"]) and not sid:
                    # Fall back to username-based detail when sub_id missing.
                    if u and item["id"] == "detail":
                        cb = f"cust:svc:{u}"
                    else:
                        continue
                btns.append(
                    InlineKeyboardButton(
                        text=item["resolved_label"], callback_data=cb
                    )
                )
            if btns:
                rows.append(btns)
    else:
        for item in ordered:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=item["resolved_label"],
                        callback_data=_cb(item["callback"]),
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def layout_to_text(bot: str) -> str:
    """Action ids only (one row per line). Labels are stored separately."""
    layout = load_layout(bot)
    catalog = actions_catalog(bot)
    lines: list[str] = []
    for row in layout:
        cells = [action for action in row if action in catalog]
        if cells:
            lines.append(", ".join(cells))
    return "\n".join(lines)


def layout_rows_for_panel(bot: str) -> list[list[dict[str, str]]]:
    """Visual builder rows: [{id, fa_name, label}, ...]."""
    catalog = actions_catalog(bot)
    rows_out: list[list[dict[str, str]]] = []
    for row in load_layout(bot):
        cells: list[dict[str, str]] = []
        for action in row:
            meta = catalog.get(action)
            if not meta:
                continue
            cells.append(
                {
                    "id": action,
                    "fa_name": str(meta.get("fa_name") or meta.get("label") or action),
                    "label": label_for(bot, action),
                }
            )
        if cells:
            rows_out.append(cells)
    return rows_out


def catalog_for_panel(bot: str) -> list[dict[str, str]]:
    catalog = actions_catalog(bot)
    return [
        {
            "id": k,
            "fa_name": str(v.get("fa_name") or v["label"]),
            "label": label_for(bot, k),
            "default_label": str(v["label"]),
        }
        for k, v in catalog.items()
    ]


def parse_layout_text(bot: str, text: str) -> tuple[list[list[str]], dict[str, str]]:
    catalog = actions_catalog(bot)
    layout: list[list[str]] = []
    labels: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        row: list[str] = []
        for cell in line.split(","):
            part = cell.strip()
            if not part:
                continue
            if "=" in part:
                action, label = part.split("=", 1)
                action = action.strip()
                label = label.strip()
            else:
                action, label = part, ""
            if action not in catalog:
                raise ValueError(f"اکشن ناشناخته: {action}")
            row.append(action)
            if label:
                labels[action] = label
        if row:
            layout.append(row)
    if not layout:
        raise ValueError("چیدمان خالی است")
    return layout, labels


def available_actions_help(bot: str) -> str:
    catalog = actions_catalog(bot)
    return " · ".join(
        f"{k} = {v.get('fa_name') or v['label']}" for k, v in catalog.items()
    )


def _inline_sections_for_panel(
    catalog: dict[str, dict[str, Any]],
    *,
    labels: dict[str, dict[str, str]],
    order_map: dict[str, list[str]],
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for mid, meta in catalog.items():
        by_id = {item["id"]: item for item in meta["items"]}
        ordered_ids: list[str] = []
        seen: set[str] = set()
        for iid in order_map.get(mid) or []:
            if iid in by_id and iid not in seen:
                ordered_ids.append(iid)
                seen.add(iid)
        for item in meta["items"]:
            if item["id"] not in seen:
                ordered_ids.append(item["id"])
        items = []
        ov = labels.get(mid) or {}
        for iid in ordered_ids:
            item = by_id[iid]
            items.append(
                {
                    "id": item["id"],
                    "callback": item["callback"],
                    "default_label": item["label"],
                    "label": ov.get(item["id"]) or item["label"],
                }
            )
        sections.append(
            {
                "id": mid,
                "title": meta["fa_title"],
                "category": meta.get("category") or "other",
                "items": items,
            }
        )
    return sections


_INLINE_CATEGORY_TITLES: dict[str, str] = {
    "settings": "تنظیمات",
    "requests": "درخواست‌ها",
    "ownership": "مالکیت",
    "tools": "ابزارها",
    "home": "خانه",
    "services": "سرویس‌ها",
    "checkout": "پرداخت / موفقیت",
    "other": "سایر",
}


def _group_inline_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group inline menu sections by category for the panel."""
    order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for sec in sections:
        cat = str(sec.get("category") or "other")
        if cat not in buckets:
            buckets[cat] = []
            order.append(cat)
        buckets[cat].append(sec)
    return [
        {
            "key": cat,
            "title": _INLINE_CATEGORY_TITLES.get(cat, cat),
            "sections": buckets[cat],
        }
        for cat in order
    ]


def panel_snapshot(bot_tab: str) -> dict[str, Any]:
    """Data for the menus HTML page (Persian-oriented)."""
    if bot_tab == "admin":
        inline_sections = _inline_sections_for_panel(
            ADMIN_INLINE_MENUS,
            labels=load_admin_inline_labels(),
            order_map=load_admin_inline_order(),
        )
        return {
            "main": {
                "id": "admin",
                "title": "منوی اصلی ادمین",
                "subtitle": "کیبورد پایین ربات ادمین",
                "layout_text": layout_to_text("admin"),
                "rows": layout_rows_for_panel("admin"),
                "catalog": catalog_for_panel("admin"),
            },
            "inner_reply": [],
            "inline_sections": inline_sections,
            "inline_groups": _group_inline_sections(inline_sections),
        }
    inline_sections = _inline_sections_for_panel(
        CUSTOMER_INLINE_MENUS,
        labels=load_customer_inline_labels(),
        order_map=load_customer_inline_order(),
    )
    return {
        "main": {
            "id": "customer",
            "title": "منوی اصلی مشتری",
            "subtitle": "کیبورد پایین ربات مشتری",
            "layout_text": layout_to_text("customer"),
            "rows": layout_rows_for_panel("customer"),
            "catalog": catalog_for_panel("customer"),
        },
        "inner_reply": [
            {
                "id": "customer_support",
                "title": "منوی داخلی پشتیبانی",
                "subtitle": "بعد از زدن دکمه پشتیبانی نمایش داده می‌شود",
                "layout_text": layout_to_text("customer_support"),
                "rows": layout_rows_for_panel("customer_support"),
                "catalog": catalog_for_panel("customer_support"),
                "label_prefix": "label_support_",
                "layout_field": "layout_support",
                "scope": "support",
            }
        ],
        "inline_sections": inline_sections,
        "inline_groups": _group_inline_sections(inline_sections),
    }
