"""Customer pays NetPlus card directly via wallet top-up walk."""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.database.database import get_session
from app.database import repositories as repo
from app.database.models import CustomerOrder

log = logging.getLogger(__name__)

RECEIPT_FORWARD_SECONDS = 180
META_KEY = "vendor_direct_meta:{oid}"
RECEIPT_KEY = "vendor_direct_receipt:{oid}"
RECEIPT_AT_KEY = "vendor_direct_receipt_at:{oid}"
FORWARDED_KEY = "vendor_direct_forwarded:{oid}"


def _meta_key(oid: int) -> str:
    return META_KEY.format(oid=oid)


def _receipt_key(oid: int) -> str:
    return RECEIPT_KEY.format(oid=oid)


def _receipt_at_key(oid: int) -> str:
    return RECEIPT_AT_KEY.format(oid=oid)


def _forwarded_key(oid: int) -> str:
    return FORWARDED_KEY.format(oid=oid)


def load_meta(session, order_id: int) -> dict:
    raw = (repo.get_state(session, _meta_key(order_id)) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_meta(session, order_id: int, data: dict) -> None:
    repo.set_state(session, _meta_key(order_id), json.dumps(data, ensure_ascii=False))


async def prepare_vendor_direct_payment(order_id: int) -> dict:
    """Walk NetPlus wallet top-up and stamp order with vendor card/amount."""
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            return {"ok": False, "error": "missing_order"}
        if order.kind not in ("create", "renew"):
            return {"ok": False, "error": "bad_kind"}
        amount_toman = repo.order_payable_toman(order)
        uname = order.service_username
        if amount_toman <= 0:
            return {"ok": False, "error": "nothing_payable"}

    from app.telegram import customer_bridge

    rq = customer_bridge.renew_queue
    if rq is None:
        return {"ok": False, "error": "queue_unavailable"}
    driver = rq._driver_for_vendor_id(None)
    if driver is None or not hasattr(driver, "drive_wallet_topup"):
        return {"ok": False, "error": "driver_unavailable"}

    result = await driver.drive_wallet_topup(amount_toman)
    if not result.ok or not result.payment:
        return {
            "ok": False,
            "error": result.error or result.stage or "topup_failed",
        }
    pay = result.payment
    amount_rial = (pay.amount_rial or "").replace(",", "").strip()
    card = (pay.card or "").strip()
    if not amount_rial or not card:
        return {"ok": False, "error": "incomplete_payment_screen"}

    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            return {"ok": False, "error": "missing_order"}
        order.amount_rial = amount_rial
        order.pay_card_number = card
        order.pay_card_holder = (pay.holder or "").strip() or None
        order.pay_source = "vendor_direct"
        order.checkout_url = None
        order.gateway_invoice_id = None
        order.variz_payment_id = None
        note = order.note or ""
        if "|vendor_direct" not in note:
            order.note = (note + "|vendor_direct").strip("|")
        if not order.instructions_at:
            order.instructions_at = datetime.utcnow()
        repo.touch_customer_order(order)
        save_meta(
            session,
            order_id,
            {
                "amount_rial": amount_rial,
                "card": card,
                "holder": pay.holder,
                "receipt_callback": pay.receipt_callback,
                "payment_message_id": pay.message_id,
                "username": uname,
                "amount_toman": amount_toman,
            },
        )
    return {
        "ok": True,
        "amount_rial": amount_rial,
        "card": card,
        "holder": pay.holder,
    }


def store_customer_receipt_bytes(order_id: int, file_bytes: bytes, *, filename: str = "receipt.jpg") -> None:
    """Persist receipt for possible NetPlus forward after the grace window."""
    if not file_bytes:
        return
    # Prefer disk under data/ so state table stays small
    try:
        root = Path(__file__).resolve().parents[2] / "data" / "vendor_direct_receipts"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{order_id}.bin"
        path.write_bytes(file_bytes)
        disk_ref = str(path)
    except Exception:
        log.exception("vendor_direct receipt disk write failed order=%s", order_id)
        disk_ref = ""
    payload = {
        "filename": filename,
        "b64": base64.b64encode(file_bytes).decode("ascii") if not disk_ref else "",
        "path": disk_ref,
        "saved_at": datetime.utcnow().isoformat(),
    }
    with get_session() as session:
        repo.set_state(session, _receipt_key(order_id), json.dumps(payload))
        repo.set_state(
            session,
            _receipt_at_key(order_id),
            datetime.utcnow().isoformat(timespec="seconds"),
        )
        # Clear prior forward flag so a new receipt can be re-sent if needed
        repo.set_state(session, _forwarded_key(order_id), "")


def _load_receipt_bytes(session, order_id: int) -> tuple[bytes, str] | None:
    raw = (repo.get_state(session, _receipt_key(order_id)) or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    filename = str(data.get("filename") or "receipt.jpg")
    path = str(data.get("path") or "").strip()
    if path:
        try:
            return Path(path).read_bytes(), filename
        except Exception:
            log.exception("read vendor_direct receipt path failed order=%s", order_id)
    b64 = str(data.get("b64") or "").strip()
    if not b64:
        return None
    try:
        return base64.b64decode(b64), filename
    except Exception:
        return None


async def maybe_forward_stale_receipts() -> None:
    """If receipt is older than 3 minutes and NetPlus still silent, send photo."""
    now = datetime.utcnow()
    with get_session() as session:
        rows = session.scalars(
            select(CustomerOrder)
            .where(
                CustomerOrder.kind.in_(("create", "renew")),
                CustomerOrder.status.in_(
                    ("awaiting_payment", "receipt_window", "awaiting_review")
                ),
            )
            .order_by(CustomerOrder.id.desc())
            .limit(60)
        ).all()
        candidates: list[int] = []
        for order in rows:
            if not repo.order_is_vendor_direct(order):
                continue
            oid = int(order.id)
            if (repo.get_state(session, _forwarded_key(oid)) or "").strip() == "1":
                continue
            raw_at = (repo.get_state(session, _receipt_at_key(oid)) or "").strip()
            if not raw_at:
                continue
            try:
                received = datetime.fromisoformat(raw_at)
            except ValueError:
                continue
            if now - received < timedelta(seconds=RECEIPT_FORWARD_SECONDS):
                continue
            if _load_receipt_bytes(session, oid) is None:
                continue
            candidates.append(oid)

    for oid in candidates:
        await forward_receipt_to_vendor(oid)


async def forward_receipt_to_vendor(order_id: int) -> dict:
    with get_session() as session:
        if (repo.get_state(session, _forwarded_key(order_id)) or "").strip() == "1":
            return {"ok": True, "already": True}
        packed = _load_receipt_bytes(session, order_id)
        meta = load_meta(session, order_id)
        if not packed:
            return {"ok": False, "error": "no_receipt"}
        file_bytes, filename = packed
        receipt_cb = meta.get("receipt_callback")
        pay_mid = meta.get("payment_message_id")

    from app.telegram import customer_bridge

    rq = customer_bridge.renew_queue
    if rq is None:
        return {"ok": False, "error": "queue_unavailable"}
    driver = rq._driver_for_vendor_id(None)
    if driver is None:
        return {"ok": False, "error": "driver_unavailable"}

    try:
        if receipt_cb:
            try:
                await driver.click_receipt(str(receipt_cb), int(pay_mid) if pay_mid else None)
            except Exception:
                log.exception("vendor_direct click_receipt failed order=%s", order_id)
        await driver.send_receipt_photo(file_bytes, filename=filename or "receipt.jpg")
    except Exception as exc:
        log.exception("vendor_direct forward receipt failed order=%s", order_id)
        return {"ok": False, "error": str(exc)}

    with get_session() as session:
        repo.set_state(session, _forwarded_key(order_id), "1")
        order = repo.get_customer_order(session, order_id)
        if order:
            order.note = (order.note or "") + "|vendor_receipt_forwarded"
            repo.touch_customer_order(order)
    return {"ok": True}


async def try_confirm_from_vendor_event(kind: str, parsed: dict) -> bool:
    """If NetPlus confirms a top-up matching a vendor-direct order, mark paid."""
    if kind not in ("wallet_credit", "tx_bot_ok"):
        return False
    amount_rial = parsed.get("amount_rial")
    if amount_rial is None and parsed.get("amount_toman") is not None:
        try:
            amount_rial = int(parsed["amount_toman"]) * 10
        except (TypeError, ValueError):
            amount_rial = None
    if amount_rial is None:
        return False

    with get_session() as session:
        order = repo.match_pending_vendor_direct_by_amount(session, amount_rial)
        if not order:
            return False
        oid = int(order.id)

    from app.services.customer_payments import auto_confirm_by_amount

    result = await auto_confirm_by_amount(
        str(amount_rial), source=f"vendor_direct_{kind}"
    )
    ok = bool(result.get("ok"))
    if ok:
        log.info("vendor_direct auto-confirmed order=%s via %s", oid, kind)
    return ok
