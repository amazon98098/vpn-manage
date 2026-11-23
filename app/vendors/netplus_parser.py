"""Parse NetPlus (@Bombejokerbot) private-chat messages from real history samples."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from app.utils.text import normalize, parse_jalali, size_to_mb, to_int, to_number

CALLBACK_ID_RE = re.compile(
    r"^(?:extend|updateproduct|product|subscriptionurl|config|changelink|"
    r"changestatus|transfer|removeserviceuser)_([0-9a-f]+)$"
)


@dataclass
class ParsedMessage:
    kind: str
    username: str | None = None
    vendor_service_id: str | None = None
    remaining_days: int | None = None
    remaining_traffic_mb: float | None = None
    traffic_limit_mb: float | None = None
    used_traffic_mb: float | None = None
    expiration_date: date | None = None
    location: str | None = None
    product_name: str | None = None
    amount_toman: int | None = None
    amount_rial: int | None = None
    card_number: str | None = None
    card_holder: str | None = None
    subscription_url: str | None = None
    raw_summary: str = ""
    notify: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.expiration_date:
            data["expiration_date"] = self.expiration_date.isoformat()
        return data


def extract_service_id(buttons: list[list[dict[str, str]]] | None) -> str | None:
    if not buttons:
        return None
    for row in buttons:
        for btn in row:
            data = btn.get("callback_data") or ""
            m = CALLBACK_ID_RE.match(data)
            if m:
                return m.group(1)
    return None


def parse_netplus(text: str, buttons: list[list[dict[str, str]]] | None = None) -> ParsedMessage:
    raw = text or ""
    t = normalize(raw)
    service_id = extract_service_id(buttons)

    # Days remaining alert
    m = re.search(
        r"از مهلت زمانی استفاده از سرویس\s+(\S+)\s+فقط\s+(\d+)\s+روز باقی مانده",
        t,
    )
    if m:
        return ParsedMessage(
            kind="alert_days",
            username=m.group(1),
            vendor_service_id=service_id,
            remaining_days=int(m.group(2)),
            notify=True,
            raw_summary=f"{m.group(2)} روز باقی‌مانده",
        )

    # Traffic remaining alert
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

    # Deleted for non-renewal
    m = re.search(r"بدلیل عدم تمدید،?\s*سرویس\s+(\S+)\s+از لیست سرویس های شما حذف", t)
    if m:
        return ParsedMessage(
            kind="alert_deleted",
            username=m.group(1),
            vendor_service_id=service_id,
            notify=True,
            raw_summary="حذف به دلیل عدم تمدید",
        )

    # Renew success
    m = re.search(r"تمدید برای سرویس شما با موفقیت صورت گرفت[\s\S]*?نام سرویس\s*:\s*(\S+)", t)
    if m:
        product = _field(t, r"نام محصول\s*:\s*(.+)")
        amount = to_int(_field(t, r"مبلغ تمدید\s*([\d,]+)"))
        exp = parse_jalali(_field(t, r"تاریخ اتمام\s*:\s*([^\n]+)"))
        return ParsedMessage(
            kind="renew_ok",
            username=m.group(1),
            vendor_service_id=service_id,
            product_name=product,
            amount_toman=amount,
            expiration_date=exp,
            notify=False,
            raw_summary="تمدید موفق",
        )

    # Create success
    m = re.search(r"سرویس با موفقیت ایجاد شد[\s\S]*?نام کاربری سرویس\s*:\s*(\S+)", t)
    if m:
        product = _field(t, r"نام سرویس:\s*(.+)")
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

    # Active / expired status screens
    if "وضعیت سرویس" in t and ("نام سرویس" in t or "نام کاربری سرویس" in t):
        username = _field(t, r"نام سرویس\s*:\s*(\S+)") or _field(
            t, r"نام کاربری سرویس\s*:\s*(\S+)"
        )
        remaining = size_to_mb(
            to_number(_field(t, r"حجم باقی مانده\s*:\s*([\d.]+)")),
            _field(t, r"حجم باقی مانده\s*:\s*[\d.]+\s*(\S+)"),
        )
        total = size_to_mb(
            to_number(_field(t, r"ترافیک\s*:\s*([\d.]+)")),
            _field(t, r"ترافیک\s*:\s*[\d.]+\s*(\S+)"),
        )
        used = size_to_mb(
            to_number(_field(t, r"حجم مصرفی\s*:\s*([\d.]+)")),
            _field(t, r"حجم مصرفی\s*:\s*[\d.]+\s*(\S+)"),
        )
        days = to_int(_field(t, r"\((\d+)\s*روز"))
        exp = parse_jalali(_field(t, r"تاریخ اتمام\s*:\s*([^\n(]+)"))
        location = _field(t, r"موقعیت سرویس\s*:\s*(.+)")
        product = _field(t, r"نام محصول\s*:\s*(.+)")
        if "پایان حجم" in t:
            kind = "status_volume_exhausted"
            summary = "پایان حجم"
        elif "غیرفعال" in t:
            kind = "status_disabled"
            summary = "غیرفعال"
        elif "پایان زمان سرویس" in t:
            kind = "status_expired"
            summary = "منقضی"
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
            remaining_days=days,
            expiration_date=exp,
            location=location,
            product_name=product,
            notify=False,
            raw_summary=summary,
        )

    # Payment instructions (unique amount)
    if "شماره کارت" in t and ("ریال" in t or "مبلغ" in t):
        amount_rial = to_int(_field(t, r"مبلغ(?:\s*دقیق)?\s*:\s*([\d,]+)\s*ریال"))
        if amount_rial is None:
            amount_rial = to_int(_field(t, r"مبلغ دقیق:\s*([\d,]+)\s*ریال"))
        card = _field(t, r"شماره کارت\s*:\s*(\d{16})")
        if not card:
            m_card = re.search(r"\b(\d{16})\b", t)
            card = m_card.group(1) if m_card else None
        holder = _field(t, r"به نام:\s*(.+)")
        return ParsedMessage(
            kind="payment_instructions",
            amount_rial=amount_rial,
            card_number=card,
            card_holder=holder,
            notify=False,
            raw_summary="دستور پرداخت",
        )

    if "فاکتور تمدید شما برای نام کاربری" in t:
        username = _field(t, r"نام کاربری\s+(\S+)\s+ایجاد شد")
        amount = to_int(_field(t, r"مبلغ تمدید\s*:\s*([\d,]+)"))
        product = _field(t, r"نام محصول\s*:\s*(.+)")
        return ParsedMessage(
            kind="renew_invoice",
            username=username,
            amount_toman=amount,
            product_name=product,
            notify=False,
            raw_summary="فاکتور تمدید",
        )

    if "تراکنش شما توسط ربات تایید" in t:
        amount_rial = to_int(_field(t, r"مبلغ(?:\s*دقیق)?\s*:\s*([\d,]+)\s*ریال"))
        if amount_rial is None:
            amount_rial = to_int(_field(t, r"([\d,]+)\s*ریال"))
        amount_toman = to_int(_field(t, r"([\d,]+)\s*تومان"))
        return ParsedMessage(
            kind="tx_bot_ok",
            amount_rial=amount_rial,
            amount_toman=amount_toman,
            notify=False,
            raw_summary="تایید تراکنش",
        )

    if "به کیف پول شما واریز" in t or "به موجودی کیف پول" in t:
        amount = to_int(_field(t, r"مبلغ\s+([\d,]+)\s+تومان"))
        if amount is None:
            amount = to_int(_field(t, r"([\d,]+)\s*تومان"))
        return ParsedMessage(
            kind="wallet_credit",
            amount_toman=amount,
            notify=False,
            raw_summary="شارژ کیف پول",
        )

    return ParsedMessage(kind="unknown", notify=False)


def _field(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    if not m:
        return None
    return m.group(1).strip()
