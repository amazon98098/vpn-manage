"""Human-friendly duration helpers for settings UI (seconds ↔ minutes)."""
from __future__ import annotations


def format_seconds_fa(seconds: int) -> str:
    """Persian display: minutes (or hours) when value is at least 60s."""
    try:
        sec = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if sec <= 0:
        return "۰ ثانیه"
    if sec % 3600 == 0 and sec >= 3600:
        h = sec // 3600
        return f"{h} ساعت"
    if sec >= 60 and sec % 60 == 0:
        m = sec // 60
        return f"{m} دقیقه"
    if sec >= 60:
        m, s = divmod(sec, 60)
        return f"{m} دقیقه و {s} ثانیه"
    return f"{sec} ثانیه"


def format_seconds_en(seconds: int) -> str:
    try:
        sec = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if sec <= 0:
        return "0 seconds"
    if sec % 3600 == 0 and sec >= 3600:
        h = sec // 3600
        return f"{h} hour{'s' if h != 1 else ''}"
    if sec >= 60 and sec % 60 == 0:
        m = sec // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    if sec >= 60:
        m, s = divmod(sec, 60)
        return f"{m}m {s}s"
    return f"{sec} seconds"


def seconds_to_minutes_value(seconds: int) -> int | float:
    """Form value in minutes; keep fractional only if needed."""
    try:
        sec = max(0, int(seconds))
    except (TypeError, ValueError):
        return 0
    if sec % 60 == 0:
        return sec // 60
    return round(sec / 60.0, 2)


def minutes_to_seconds(raw: str | int | float) -> int:
    """Parse minutes input (int/float/string) to seconds."""
    if isinstance(raw, (int, float)):
        return max(0, int(float(raw) * 60))
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        return 0
    return max(0, int(float(text) * 60))


def parse_minutes_list(raw: str) -> list[int]:
    """Comma-separated minutes → seconds list."""
    out: list[int] = []
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(minutes_to_seconds(part))
        except ValueError:
            continue
    return out


def format_seconds_list_fa(values: list[int]) -> str:
    return "، ".join(format_seconds_fa(v) for v in values)


def format_minutes_csv(values: list[int]) -> str:
    """CSV of minute values for form fields."""
    parts: list[str] = []
    for v in values:
        mv = seconds_to_minutes_value(v)
        parts.append(str(int(mv)) if float(mv).is_integer() else str(mv))
    return ",".join(parts)
