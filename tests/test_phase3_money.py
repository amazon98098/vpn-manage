"""Phase 3 money-safety and catalog helpers."""
from __future__ import annotations

import os
import time

from app.database.database import get_session, init_db
from app.database import repositories as repo

_DB = f"sqlite:////tmp/vpn_phase3_pytest_{os.getpid()}_{int(time.time())}.db"


def setup_module() -> None:
    init_db(_DB)


def test_debit_wallet_rejects_overdraft() -> None:
    with get_session() as session:
        tg = int(time.time() * 1000) % 1_000_000_000 + 1
        repo.credit_wallet(session, tg, 1000, kind="charge", note="seed")
        try:
            repo.debit_wallet(session, tg, 2000)
            assert False, "expected ValueError"
        except ValueError:
            assert repo.get_wallet_toman(session, tg) == 1000


def test_refund_wallet_applied_once() -> None:
    with get_session() as session:
        tg = int(time.time() * 1000) % 1_000_000_000 + 2
        repo.credit_wallet(session, tg, 50000, kind="charge", note="seed")
        order = repo.create_customer_order(
            session,
            telegram_id=tg,
            kind="create",
            plan_id=None,
            service_username=f"u{tg}",
            price_toman=40000,
        )
        repo.debit_wallet(session, tg, 15000, order_id=order.id, note="apply")
        order.wallet_applied_toman = 15000
        refunded = repo.refund_wallet_applied(session, order)
        assert refunded == 15000
        assert order.wallet_applied_toman == 0
        assert repo.get_wallet_toman(session, tg) == 50000
        assert repo.refund_wallet_applied(session, order) == 0


def test_credit_wallet_once_for_order() -> None:
    with get_session() as session:
        tg = int(time.time() * 1000) % 1_000_000_000 + 3
        order = repo.create_customer_order(
            session,
            telegram_id=tg,
            kind="wallet",
            plan_id=None,
            service_username="WALLET",
            price_toman=12000,
        )
        bal1, c1 = repo.credit_wallet_once_for_order(
            session, tg, 12000, order_id=order.id, note="t1"
        )
        bal2, c2 = repo.credit_wallet_once_for_order(
            session, tg, 12000, order_id=order.id, note="t2"
        )
        assert c1 is True and c2 is False
        assert bal1 == bal2 == 12000


def test_unique_amount_rial_no_collision() -> None:
    with get_session() as session:
        amounts = set()
        base = int(time.time() * 1000) % 1_000_000_000 + 1000
        for i in range(20):
            o = repo.create_customer_order(
                session,
                telegram_id=base + i,
                kind="wallet",
                plan_id=None,
                service_username="WALLET",
                price_toman=25000,
            )
            amounts.add(o.amount_rial)
        assert len(amounts) == 20


def test_cancel_refunds_awaiting_review() -> None:
    with get_session() as session:
        tg = int(time.time() * 1000) % 1_000_000_000 + 4
        repo.credit_wallet(session, tg, 30000, kind="charge", note="seed")
        order = repo.create_customer_order(
            session,
            telegram_id=tg,
            kind="create",
            plan_id=None,
            service_username=f"u{tg}",
            price_toman=20000,
        )
        repo.debit_wallet(session, tg, 5000, order_id=order.id, note="apply")
        order.wallet_applied_toman = 5000
        order.status = "awaiting_review"
        refunded = repo.cancel_customer_order(session, order)
        assert refunded == 5000
        assert order.status == "cancelled"
        assert repo.get_wallet_toman(session, tg) == 30000
