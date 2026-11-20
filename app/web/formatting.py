"""Display helpers for the admin panel."""
from __future__ import annotations

import re
from typing import Any

from app.utils.text import format_remaining_compact_en, format_traffic_panel

ICON_ECONOMY = "✨"
ICON_VOLUME = "💎"


def traffic(mb: float | None) -> str:
    return format_traffic_panel(mb)


def card_grouped(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return ""
    parts = [digits[i : i + 4] for i in range(0, len(digits), 4)]
    return " ".join(parts)


def card_digits(number: str | None) -> str:
    return re.sub(r"\D", "", number or "")


def plan_icon(sub=None, *, product_name: str | None = None, vendor_key: str | None = None) -> str:
    """✨ economy · 💎 time-unlimited volume."""
    return (
        ICON_VOLUME
        if plan_family(sub, product_name=product_name, vendor_key=vendor_key) == "volume"
        else ICON_ECONOMY
    )


def plan_family(
    sub=None, *, product_name: str | None = None, vendor_key: str | None = None
) -> str:
    """Return 'economy' (timed) or 'volume' (time-unlimited / FineArts)."""
    name = product_name
    if name is None and sub is not None:
        name = getattr(sub, "product_name", None)
    name = (name or "").strip()
    if "نامحدود" in name:
        return "volume"
    key = vendor_key
    if key is None and sub is not None:
        pass
    if (key or "") == "finearts":
        return "volume"
    return "economy"


def price_plan_family(plan) -> str:
    """Sell PricePlan → economy|volume (duration_days <= 0 or fa_* = volume)."""
    if plan is None:
        return "economy"
    key = (getattr(plan, "key", None) or "").strip().lower()
    if key.startswith("fa_"):
        return "volume"
    days = getattr(plan, "duration_days", None)
    if days is not None and int(days) <= 0:
        return "volume"
    return "economy"


def status_icon(status: str | None) -> str:
    st = (status or "").strip().lower()
    return {
        "active": "✅",
        "warning": "⚠️",
        "volume_exhausted": "🚫",
        "expired": "🔚",
        "disabled": "⛔",
        "unknown": "❔",
        "removed_by_vendor": "🗑️",
    }.get(st, "•")


def num(value: Any, *, decimals: int = 0) -> str:
    """Format a number with thousands separators."""
    if value is None:
        return "0"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if decimals <= 0:
        return f"{int(round(n)):,}"
    return f"{n:,.{decimals}f}"


def toman(value: Any) -> str:
    return num(value)


def rial(value: Any) -> str:
    """Display rial amounts (stored as string or int)."""
    if value is None:
        return "0"
    raw = str(value).strip().replace(",", "").replace("٬", "")
    if not raw or raw == "—":
        return "0"
    try:
        return f"{int(float(raw)):,}"
    except ValueError:
        return raw


def vendor_icon(key: str | None) -> str:
    k = (key or "").strip().lower()
    if k == "finearts":
        return "🎨"
    if k == "netplus":
        return "✨"
    return "🏪"


def remain_days(expire_at: Any) -> str:
    """Compact remaining time for tables (5d / 2h / 0d / ∞)."""
    return format_remaining_compact_en(expire_at)
