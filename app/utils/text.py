"""Text helpers: Persian digit normalization, Jalali dates, sizes."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_LETTERS = str.maketrans({"ي": "ی", "ك": "ک", "\u200f": "", "\u200e": "", "\u200b": ""})


def normalize(text: str | None) -> str:
    """Convert Persian/Arabic digits to Latin and unify Arabic letters."""
    if not text:
        return ""
    return text.translate(_DIGITS).translate(_LETTERS)


def sanitize_service_username(name: str | None) -> str | None:
    """Strip invisible RTL/LTR marks and validate VPN service usernames.

    Returns cleaned username or None if invalid.
    Rules: 3–32 chars, [A-Za-z0-9_], no leading/trailing underscore.
    """
    raw = normalize(name).strip().lstrip("@")
    # Extra invisible / bidi controls sometimes pasted from Telegram UI.
    for ch in (
        "\u200e",
        "\u200f",
        "\u200b",
        "\u200c",
        "\u200d",
        "\ufeff",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
    ):
        raw = raw.replace(ch, "")
    raw = raw.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", raw):
        return None
    if raw.startswith("_") or raw.endswith("_"):
        return None
    return raw


def to_number(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = normalize(value).replace(",", "").replace("٬", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def to_int(value: str | None) -> int | None:
    n = to_number(value)
    return int(n) if n is not None else None


def size_to_mb(amount: float | None, unit: str | None) -> float | None:
    if amount is None:
        return None
    unit = (unit or "").strip()
    u = unit.upper()
    # Terabyte (NetPlus uses «ترابایت» on large stacked allowances)
    if unit.startswith("ترا") or unit.startswith("تيرا") or u.startswith("TB"):
        return amount * 1024 * 1024
    if unit.startswith("گیگ") or u.startswith("GB"):
        return amount * 1024
    if unit.startswith("مگ") or u.startswith("MB"):
        return amount
    if "بایت" in unit or u in {"B", "BYTE", "BYTES"}:
        return amount / (1024 * 1024)
    return amount


def format_mb(mb: float | None) -> str:
    if mb is None:
        return "نامشخص"
    if mb >= 1024:
        return f"{mb / 1024:.1f} گیگ"
    return f"{mb:.0f} مگ"


def format_traffic_alert(mb: float | None) -> str:
    """Alert copy: GB when ≥1GB, otherwise MB with decimals (e.g. 1018.11 مگابایت)."""
    if mb is None:
        return "نامشخص"
    if mb >= 1024:
        gb = mb / 1024
        text = f"{gb:.2f}".rstrip("0").rstrip(".")
        return f"{text} گیگابایت"
    text = f"{mb:.2f}".rstrip("0").rstrip(".")
    return f"{text} مگابایت"


def format_traffic_short_en(mb: float | None) -> str:
    """Compact English traffic for admin list buttons (LTR)."""
    if mb is None:
        return "?"
    if mb <= 0:
        return "0MB"
    if mb >= 1024:
        gb = mb / 1024
        text = f"{gb:.2f}".rstrip("0").rstrip(".")
        return f"{text}GB"
    text = f"{mb:.0f}" if mb >= 10 else f"{mb:.1f}".rstrip("0").rstrip(".")
    return f"{text}MB"


def format_traffic_panel(mb: float | None) -> str:
    """Admin panel: GB when ≥1 GB, otherwise MB — always with a unit."""
    if mb is None:
        return "—"
    if mb <= 0:
        return "0 MB"
    if mb >= 1024:
        gb = mb / 1024.0
        text = f"{gb:.2f}".rstrip("0").rstrip(".")
        return f"{text} GB"
    text = f"{mb:.2f}".rstrip("0").rstrip(".") if mb < 10 else f"{mb:.0f}"
    return f"{text} MB"


def _as_aware_utc(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def remaining_total_seconds(expire_at: date | datetime | None) -> int | None:
    """Seconds until expire (UTC). Negative if already ended. None if unknown."""
    exp = _as_aware_utc(expire_at)
    if exp is None:
        return None
    return int((exp - datetime.now(timezone.utc)).total_seconds())


def is_expire_passed(expire_at: date | datetime | None) -> bool:
    sec = remaining_total_seconds(expire_at)
    return sec is not None and sec <= 0


def expire_within_days(expire_at: date | datetime | None, days: int) -> bool:
    """True when expire is known and falls within the next ``days`` (inclusive of past)."""
    sec = remaining_total_seconds(expire_at)
    if sec is None:
        return False
    return sec <= int(days) * 86400


def format_remaining_compact_en(expire_at: date | datetime | None) -> str:
    """Admin list chip: ``5d``, ``2h``, ``0d``, or ``∞`` when no calendar expiry."""
    sec = remaining_total_seconds(expire_at)
    if sec is None:
        return "∞"
    if sec <= 0:
        return "0d"
    if sec < 86400:
        hours = max(1, (sec + 3599) // 3600)  # ceil hours, at least 1h if any time left
        return f"{hours}h"
    return f"{sec // 86400}d"


def format_remaining_until(expire_at: date | datetime | None) -> str:
    """NetPlus-style remaining: «1 ساعت 54دقیقه دیگر» / «23 روز 17 ساعت 10دقیقه دیگر»."""
    sec = remaining_total_seconds(expire_at)
    if sec is None:
        return "—"
    if sec <= 0:
        return "به پایان رسیده"
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes or not parts:
        parts.append(f"{minutes}دقیقه")
    return " ".join(parts) + " دیگر"


def format_expire_display(expire_at: date | datetime | None) -> str:
    """Customer/admin expire line: «۱۴۰۵/۰۸/۲۲ (1 ساعت 54دقیقه دیگر)»."""
    exp = _as_aware_utc(expire_at)
    if exp is None:
        return "نامحدود زمانی"
    jalali = format_jalali_date(exp.date())
    remain = format_remaining_until(exp)
    return f"{jalali} ({remain})"


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    month_days = [0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > month_days[gm]:
        gd -= month_days[gm]
        gm += 1
    return date(gy, gm, gd)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Return (jy, jm, jd) for a Gregorian date."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + (days % 31)
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def format_jalali_date(
    value: date | datetime | None, *, persian_digits: bool = True
) -> str:
    """Format a Gregorian date (or datetime) as Jalali YYYY/MM/DD."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    jy, jm, jd = gregorian_to_jalali(value.year, value.month, value.day)
    text = f"{jy:04d}/{jm:02d}/{jd:02d}"
    return to_persian_digits(text) if persian_digits else text


_JALALI_RE = re.compile(r"(1[34]\d{2})/(\d{1,2})/(\d{1,2})")


def parse_jalali(text: str | None) -> date | None:
    if not text:
        return None
    m = _JALALI_RE.search(normalize(text))
    if not m:
        return None
    try:
        return jalali_to_gregorian(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def customer_code(username: str | None) -> str | None:
    """Leading number convention like 518_mohammad_sohrabi -> 518."""
    if not username:
        return None
    m = re.match(r"^0*(\d+)_", username)
    return m.group(1) if m else None


_TO_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_persian_digits(value: int | float | str) -> str:
    if isinstance(value, float):
        value = int(round(value))
    return str(value).translate(_TO_PERSIAN_DIGITS)


def duration_months_label(duration_days: int) -> str:
    """Map plan length to our catalog wording (یکماهه / دوماهه / سه ماهه)."""
    months = max(1, int(round(float(duration_days) / 30.5)))
    labels = {1: "یکماهه", 2: "دوماهه", 3: "سه ماهه"}
    return labels.get(months, f"{to_persian_digits(months)}ماهه")


def format_local_product_name(
    *, traffic_gb: float | int, duration_days: int | None = 31
) -> str:
    """Our sell label only — e.g. «۵۵ گیگ یکماهه», «۱۰۰ گیگ نامحدود زمانی»."""
    gb = int(round(float(traffic_gb)))
    if duration_days is None:
        return f"{to_persian_digits(gb)} گیگ نامحدود زمانی"
    return f"{to_persian_digits(gb)} گیگ {duration_months_label(duration_days)}"


def local_product_name_from_vendor_text(text: str | None) -> str | None:
    """Derive our catalog label from vendor product text; discard vendor pricing."""
    t = normalize(text).replace("\u200c", "").replace("‌", "")
    if not t:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*گیگ", t)
    if not m:
        return None
    gb = float(m.group(1))
    # FineArts (and similar): volume plans with no calendar expiry
    if re.search(r"زمان\s*نامحدود|نامحدود\s*زمان|کاربر\s*و\s*زمان\s*نامحدود", t) or (
        "حجمی" in t and "نامحدود" in t and not re.search(r"ماه", t)
    ):
        return format_local_product_name(traffic_gb=gb, duration_days=None)
    if re.search(r"سه\s*ماه", t):
        days = 91
    elif re.search(r"دو\s*ماه", t):
        days = 61
    else:
        days = 31
    return format_local_product_name(traffic_gb=gb, duration_days=days)


def local_product_name_from_plan(plan) -> str | None:
    """Prefer structured GB+days; fall back to plan title without inventing NetPlus text."""
    if plan is None:
        return None
    if plan.traffic_gb is not None:
        days = int(plan.duration_days) if plan.duration_days else None
        if days == 0:
            days = None
        return format_local_product_name(
            traffic_gb=plan.traffic_gb,
            duration_days=days,
        )
    title = (plan.title or "").strip()
    return title or None
