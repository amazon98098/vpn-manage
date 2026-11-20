"""Variz.app payment API client (create + verify; webhook optional)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass
class VarizPayment:
    id: str
    status: str
    amount_requested: str
    amount_final: str
    checkout_url: str
    code: int | None = None
    expires_at: str | None = None
    verified: bool | None = None
    order_id: str | None = None
    raw: dict[str, Any] | None = None


class VarizError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def variz_enabled(settings: Settings) -> bool:
    return bool(settings.varizapp_api_key.strip())


def _headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.varizapp_api_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _parse_payment(data: dict[str, Any]) -> VarizPayment:
    return VarizPayment(
        id=str(data.get("id") or ""),
        status=str(data.get("status") or ""),
        amount_requested=str(data.get("amountRequested") or data.get("amount") or ""),
        amount_final=str(data.get("amountFinal") or ""),
        checkout_url=str(data.get("checkoutUrl") or ""),
        code=data.get("code") if isinstance(data.get("code"), int) else None,
        expires_at=str(data["expiresAt"]) if data.get("expiresAt") else None,
        verified=data.get("verified") if isinstance(data.get("verified"), bool) else None,
        order_id=str(data["orderId"]) if data.get("orderId") else None,
        raw=data,
    )


async def create_payment(
    settings: Settings,
    *,
    amount_rial: int | str,
    order_id: str,
    description: str = "",
    payer_name: str | None = None,
    payer_phone: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> VarizPayment:
    if not variz_enabled(settings):
        raise VarizError("Variz API key not configured")
    body: dict[str, Any] = {
        "amount": str(amount_rial).replace(",", "").strip(),
        "callbackUrl": settings.varizapp_callback_url,
        "orderId": str(order_id)[:64],
    }
    if description:
        body["description"] = description[:200]
    if payer_name:
        body["payerName"] = payer_name[:100]
    if payer_phone:
        body["payerPhone"] = payer_phone
    if metadata:
        body["metadata"] = metadata

    url = f"{settings.varizapp_api_base.rstrip('/')}/v1/payments"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=_headers(settings), json=body) as resp:
            text = await resp.text()
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {"raw": text}
            if resp.status >= 400:
                err = data.get("error") if isinstance(data, dict) else None
                code = err.get("code") if isinstance(err, dict) else None
                msg = (
                    err.get("message")
                    if isinstance(err, dict)
                    else (text[:300] or f"HTTP {resp.status}")
                )
                log.warning("Variz create payment failed status=%s code=%s", resp.status, code)
                raise VarizError(str(msg), status=resp.status, code=str(code) if code else None)
            if not isinstance(data, dict):
                raise VarizError("invalid create response")
            pay = _parse_payment(data)
            if not pay.id or not pay.amount_final:
                raise VarizError("create response missing id/amountFinal")
            return pay


async def verify_payment(settings: Settings, payment_id: str) -> VarizPayment:
    if not variz_enabled(settings):
        raise VarizError("Variz API key not configured")
    url = f"{settings.varizapp_api_base.rstrip('/')}/v1/payments/{payment_id}/verify"
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=_headers(settings)) as resp:
            text = await resp.text()
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {"raw": text}
            if resp.status >= 400:
                err = data.get("error") if isinstance(data, dict) else None
                code = err.get("code") if isinstance(err, dict) else None
                msg = (
                    err.get("message")
                    if isinstance(err, dict)
                    else (text[:300] or f"HTTP {resp.status}")
                )
                raise VarizError(str(msg), status=resp.status, code=str(code) if code else None)
            if not isinstance(data, dict):
                raise VarizError("invalid verify response")
            return _parse_payment(data)


async def get_payment(settings: Settings, payment_id: str) -> VarizPayment:
    if not variz_enabled(settings):
        raise VarizError("Variz API key not configured")
    url = f"{settings.varizapp_api_base.rstrip('/')}/v1/payments/{payment_id}"
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=_headers(settings)) as resp:
            text = await resp.text()
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {"raw": text}
            if resp.status >= 400:
                raise VarizError(text[:300] or f"HTTP {resp.status}", status=resp.status)
            if not isinstance(data, dict):
                raise VarizError("invalid get response")
            return _parse_payment(data)
