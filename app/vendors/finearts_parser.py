"""Parse FineArts (@FineArtsVPNBot) private-chat messages."""
from __future__ import annotations

import re
from typing import Any

from app.utils.text import normalize, size_to_mb, to_int, to_number
from app.vendors.netplus_parser import ParsedMessage, extract_service_id


def _field(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    if not m:
        return None
    return m.group(1).strip()


def _size_field(text: str, label: str) -> float | None:
    """Parse «حجم … : N واحد» including NAN / صفر."""
    raw = _field(text, rf"{label}\s*:\s*([^\n]+)")
    if not raw:
        return None
    if re.search(r"\bNAN\b", raw, re.I) or "صفر" in raw:
        return 0.0
    m = re.search(r"([\d.]+)\s*(\S+)", raw)
    if not m:
        return None
    return size_to_mb(to_number(m.group(1)), m.group(2))


def parse_finearts(
    text: str, buttons: list[list[dict[str, str]]] | None = None
) -> ParsedMessage:
    raw = text or ""
    t = normalize(raw)
    service_id = extract_service_id(buttons)

    m = re.search(
        r"از حجم سرویس\s+(\S+)\s+تنها\s+([\d.]+)\s*(مگابایت|گیگابایت|MB|GB)\s+باقی مانده",
        t,
        re.I,
    )
    if m:
        mb = size_to_mb(to_number(m.group(2)), m.group(3))
        return ParsedMessage(
            kind="alert_mb",
            username=m.group(1),
            vendor_service_id=service_id,
            remaining_traffic_mb=mb,
            notify=True,
            raw_summary=f"{m.group(2)} {m.group(3)} باقی‌مانده",
        )

    m = re.search(
        r"تمدید برای سرویس شما با موفقیت صورت گرفت[\s\S]*?نام سرویس\s*:\s*(\S+)",
        t,
    )
    if m:
        product = _field(t, r"نام محصول\s*:\s*(.+)")
        amount = to_int(_field(t, r"مبلغ تمدید\s*([\d,]+)"))
        return ParsedMessage(
            kind="renew_ok",
            username=m.group(1),
            vendor_service_id=service_id,
            product_name=product,
            amount_toman=amount,
            amount_rial=(amount * 10) if amount is not None else None,
            expiration_date=None,  # FineArts volume plans are time-unlimited
            notify=False,
            raw_summary="تمدید موفق",
        )

    m = re.search(
        r"سرویس با موفقیت ایجاد شد[\s\S]*?نام کاربری سرویس\s*:\s*(\S+)",
        t,
    )
    if m:
        product = _field(t, r"نام سرویس:\s*(.+)") or _field(t, r"نام محصول\s*:\s*(.+)")
        location = _field(t, r"لوکیشن:\s*(.+)")
        url_m = re.search(r"https?://\S+", t)
        return ParsedMessage(
            kind="create_ok",
            username=m.group(1),
            vendor_service_id=service_id,
            product_name=product,
            location=location,
            subscription_url=(url_m.group(0).rstrip(").,]") if url_m else None),
            notify=False,
            raw_summary="ساخت موفق",
        )

    if "وضعیت سرویس" in t and ("نام سرویس" in t or "نام کاربری سرویس" in t):
        username = _field(t, r"نام سرویس\s*:\s*(\S+)") or _field(
            t, r"نام کاربری(?: سرویس)?\s*:\s*(\S+)"
        )
        remaining = _size_field(t, "حجم باقی مانده")
        total = _size_field(t, "ترافیک")
        used = _size_field(t, "حجم مصرفی")
        location = _field(t, r"موقعیت سرویس\s*:\s*(.+)")
        product = _field(t, r"نام محصول\s*:\s*(.+)")
        # Never invent a calendar expiry from «نامحدود (0)»
        if "پایان حجم" in t:
            kind = "status_volume_exhausted"
            summary = "پایان حجم"
        elif "غیرفعال" in t:
            kind = "status_disabled"
            summary = "غیرفعال"
        else:
            kind = "status_active"
            summary = "فعال"
        return ParsedMessage(
            kind=kind,
            username=username,
            vendor_service_id=service_id,
            remaining_traffic_mb=remaining,
            traffic_limit_mb=total,
            used_traffic_mb=used,
            remaining_days=None,
            expiration_date=None,
            location=location,
            product_name=product,
            notify=False,
            raw_summary=summary,
        )

    if "فاکتور تمدید شما برای نام کاربری" in t:
        username = _field(t, r"نام کاربری\s+(\S+)\s+ایجاد شد")
        amount = to_int(_field(t, r"مبلغ تمدید\s*:\s*([\d,]+)"))
        product = _field(t, r"نام محصول\s*:\s*(.+)")
        return ParsedMessage(
            kind="renew_invoice",
            username=username,
            amount_toman=amount,
            amount_rial=(amount * 10) if amount is not None else None,
            product_name=product,
            notify=False,
            raw_summary="فاکتور تمدید",
        )

    if "تراکنش شما ایجاد شد" in t and ("پرداخت" in t or "Tetherial" in t or "t.me/" in t):
        return ParsedMessage(
            kind="payment_external",
            notify=False,
            raw_summary="لینک پرداخت خارجی",
        )

    if "تراکنش شما توسط ربات تایید" in t:
        return ParsedMessage(kind="tx_bot_ok", notify=False, raw_summary="تایید تراکنش")

    return ParsedMessage(kind="unknown", notify=False)


def status_dict_from_parsed(parsed: ParsedMessage, *, username: str | None = None) -> dict[str, Any]:
    data = parsed.as_dict()
    if username and not data.get("username"):
        data["username"] = username
    return data
