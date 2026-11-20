"""Aban Gateway payment API (create / get / verify / simulate)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass
class AbanInvoice:
    invoice_id: str
    status: str
    amount_rial: int
    payable_rial: int
    payment_url: str
    card_number: str | None = None
    card_holder: str | None = None
    order_id: str | None = None
    is_test: bool = False
    verified: bool | None = None
    raw: dict[str, Any] | None = None


class AbanError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code


def aban_enabled(settings: Settings) -> bool:
    return bool(settings.abangateway_api_key.strip())


def aban_is_test(settings: Settings) -> bool:
    return settings.abangateway_api_key.strip().startswith("ag_test_")


def _headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.abangateway_api_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _base(settings: Settings) -> str:
    return settings.abangateway_api_base.rstrip("/")


def _parse_error(data: Any, text: str, status: int) -> AbanError:
    err = data.get("error") if isinstance(data, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    msg = (
        err.get("message")
        if isinstance(err, dict)
        else (text[:300] or f"HTTP {status}")
    )
    return AbanError(str(msg), status=status, code=str(code) if code else None)


def _parse_invoice(data: dict[str, Any]) -> AbanInvoice:
    return AbanInvoice(
        invoice_id=str(data.get("invoice_id") or ""),
        status=str(data.get("status") or ""),
        amount_rial=int(data.get("amount_rial") or 0),
        payable_rial=int(data.get("payable_rial") or data.get("amount_rial") or 0),
        payment_url=str(data.get("payment_url") or ""),
        card_number=str(data["card_number"]) if data.get("card_number") else None,
        card_holder=str(data["card_holder"]) if data.get("card_holder") else None,
        order_id=str(data["order_id"]) if data.get("order_id") else None,
        is_test=bool(data.get("is_test")),
        verified=data.get("verified") if isinstance(data.get("verified"), bool) else None,
        raw=data,
    )


async def _request(
    settings: Settings,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, Any, str]:
    url = f"{_base(settings)}{path}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(
            method, url, headers=_headers(settings), json=json_body
        ) as resp:
            text = await resp.text()
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {"raw": text}
            return resp.status, data, text


async def create_invoice(
    settings: Settings,
    *,
    amount_rial: int,
    order_id: str,
    description: str = "",
    metadata: dict[str, Any] | None = None,
    expiry_minutes: int | None = None,
) -> AbanInvoice:
    if not aban_enabled(settings):
        raise AbanError("Aban API key not configured")
    # API requires multiple of 10 (whole toman)
    amount = int(amount_rial)
    if amount <= 0 or amount % 10 != 0:
        raise AbanError(f"amount_rial must be positive multiple of 10, got {amount}")
    body: dict[str, Any] = {
        "amount_rial": amount,
        "order_id": str(order_id)[:128],
    }
    if settings.abangateway_callback_url:
        body["callback_url"] = settings.abangateway_callback_url
    if description:
        body["description"] = description[:1000]
    if metadata:
        body["metadata"] = metadata
    if expiry_minutes is not None:
        body["expiry_minutes"] = expiry_minutes

    status, data, text = await _request(
        settings, "POST", "/invoices", json_body=body
    )
    if status >= 400 or not isinstance(data, dict):
        err = _parse_error(data, text, status)
        log.warning("Aban create invoice failed status=%s code=%s", status, err.code)
        raise err
    inv = _parse_invoice(data)
    if not inv.invoice_id or not inv.payable_rial:
        raise AbanError("create response missing invoice_id/payable_rial")
    return inv


async def get_invoice(settings: Settings, invoice_id: str) -> AbanInvoice:
    status, data, text = await _request(settings, "GET", f"/invoices/{invoice_id}")
    if status >= 400 or not isinstance(data, dict):
        raise _parse_error(data, text, status)
    return _parse_invoice(data)


async def verify_invoice(settings: Settings, invoice_id: str) -> AbanInvoice:
    """One-shot verify. 409 already_verified is success for our idempotent poller."""
    status, data, text = await _request(
        settings, "POST", f"/invoices/{invoice_id}/verify"
    )
    if status == 409 and isinstance(data, dict):
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = err.get("code") if isinstance(err, dict) else None
        if code == "already_verified":
            inv = AbanInvoice(
                invoice_id=invoice_id,
                status="paid",
                amount_rial=int(data.get("amount_rial") or 0),
                payable_rial=int(data.get("payable_rial") or data.get("amount_rial") or 0),
                payment_url="",
                order_id=str(data["order_id"]) if data.get("order_id") else None,
                verified=True,
                raw=data,
            )
            return inv
    if status >= 400 or not isinstance(data, dict):
        raise _parse_error(data, text, status)
    inv = _parse_invoice(data)
    if inv.verified is None and status == 200:
        inv.verified = True
        if not inv.status:
            inv.status = "paid"
    return inv


async def simulate_payment(settings: Settings, invoice_id: str) -> AbanInvoice:
    """Sandbox only (ag_test_). Marks invoice paid without real transfer."""
    if not aban_is_test(settings):
        raise AbanError("simulate-payment requires ag_test_ key", code="sandbox_only")
    status, data, text = await _request(
        settings, "POST", f"/invoices/{invoice_id}/simulate-payment"
    )
    if status >= 400 or not isinstance(data, dict):
        raise _parse_error(data, text, status)
    return _parse_invoice(data)
