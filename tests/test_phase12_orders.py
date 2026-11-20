"""Phase 1–2: order expiry, reminders, CMS, tickets."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.database.database import get_session, init_db
from app.database import repositories as repo


def setup_module() -> None:
    init_db("sqlite:////tmp/vpn_phase12_pytest.db")


def test_create_order_sets_expiry_and_reminders_config() -> None:
    with get_session() as session:
        repo.set_order_ttl_seconds(session, 3600)
        repo.set_reminder_delays_seconds(session, [60, 120])
        order = repo.create_customer_order(
            session,
            telegram_id=42,
            kind="wallet",
            plan_id=None,
            service_username="WALLET",
            price_toman=15000,
        )
        assert order.expires_at is not None
        assert order.expires_at > datetime.utcnow()
        assert order.reminder_count == 0
        assert repo.get_reminder_delays_seconds(session) == [60, 120]


def test_due_reminder_and_expire_lists() -> None:
    with get_session() as session:
        repo.set_reminder_delays_seconds(session, [1])
        order = repo.create_customer_order(
            session,
            telegram_id=43,
            kind="create",
            plan_id=None,
            service_username="u_test",
            price_toman=100000,
        )
        order.instructions_at = datetime.utcnow() - timedelta(seconds=30)
        order.expires_at = datetime.utcnow() + timedelta(hours=1)
        repo.touch_customer_order(order)
        oid = order.id

    with get_session() as session:
        due = repo.list_customer_orders_due_reminder(session)
        assert any(o.id == oid for o in due)

    with get_session() as session:
        order = repo.get_customer_order(session, oid)
        assert order
        order.expires_at = datetime.utcnow() - timedelta(seconds=5)
        repo.touch_customer_order(order)

    with get_session() as session:
        expired = repo.list_customer_orders_to_expire(session)
        assert any(o.id == oid for o in expired)


def test_bot_messages_and_education_seed() -> None:
    with get_session() as session:
        repo.ensure_default_bot_messages(session)
        repo.ensure_default_education(session)
        assert "خوش" in repo.get_bot_message(session, "welcome")
        cats = repo.list_education_categories(session)
        assert cats
        lessons = repo.list_education_lessons(session, cats[0].id)
        assert lessons


def test_reject_ticket_flow_helpers() -> None:
    with get_session() as session:
        order = repo.create_customer_order(
            session,
            telegram_id=99,
            kind="wallet",
            plan_id=None,
            service_username="WALLET",
            price_toman=20000,
        )
        ticket = repo.create_support_ticket(
            session,
            telegram_id=99,
            order_id=order.id,
            kind="rejection",
            subject="reject",
            status="awaiting_customer",
        )
        repo.add_support_message(
            session, ticket.id, sender="admin", body="مبلغ اشتباه"
        )
        msgs = repo.list_support_messages(session, ticket.id)
        assert len(msgs) == 1
        assert ticket.status == "awaiting_customer"


def test_pricing_text_scrubs_admin_jargon() -> None:
    with get_session() as session:
        repo.set_state(
            session,
            "customer_pricing_text",
            "قیمت‌ها از پنل ادمین قابل ویرایش هستند.",
        )
        text = repo.get_pricing_text(session)
        assert "پنل ادمین" not in text
