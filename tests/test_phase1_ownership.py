"""Phase 1: ownership helpers, usable-username rules, status snapshot fields."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.database.database import _migrate_sqlite
from app.database.models import (
    Base,
    CustomerProfile,
    CustomerServiceLink,
    Subscription,
    Vendor,
)
from app.database import repositories as repo


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _migrate_sqlite(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    vendor = Vendor(
        key="netplus",
        name="NetPlus",
        telegram_username="Bombejokerbot",
        info_refresh_strategy="automatic",
    )
    s.add(vendor)
    s.add(CustomerProfile(telegram_id=111, username="owner", full_name="Owner"))
    s.add(CustomerProfile(telegram_id=222, username="recv", full_name="Recv"))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _sub(session: Session, username: str, status: str = "active") -> Subscription:
    vendor = session.scalar(select(Vendor).limit(1))
    assert vendor
    sub = Subscription(
        vendor_id=vendor.id,
        username=username,
        vendor_service_id="abc123",
        status=status,
    )
    session.add(sub)
    session.flush()
    return sub


def test_set_local_owner_assigns_and_audits(session: Session) -> None:
    sub = _sub(session, "svc_one")
    link = repo.set_local_owner(
        session,
        to_telegram_id=111,
        initiated_by_telegram_id=999,
        source="admin_assign",
        subscription=sub,
    )
    session.commit()
    assert link.telegram_id == 111
    assert link.subscription_id == sub.id
    assert link.service_username == "svc_one"
    assert session.scalar(select(CustomerServiceLink)) is link


def test_set_local_owner_reassigns_and_records_previous(session: Session) -> None:
    sub = _sub(session, "svc_two")
    repo.set_local_owner(
        session,
        to_telegram_id=111,
        initiated_by_telegram_id=999,
        source="admin_assign",
        subscription=sub,
    )
    session.flush()
    link = repo.set_local_owner(
        session,
        to_telegram_id=222,
        initiated_by_telegram_id=999,
        source="admin_reassign",
        subscription=sub,
    )
    session.commit()
    assert link.telegram_id == 222
    links = list(session.scalars(select(CustomerServiceLink)))
    assert len(links) == 1
    assert links[0].telegram_id == 222


def test_transfer_local_ownership_c2c(session: Session) -> None:
    sub = _sub(session, "svc_xfer")
    repo.set_local_owner(
        session,
        to_telegram_id=111,
        initiated_by_telegram_id=111,
        source="admin_assign",
        subscription=sub,
    )
    session.commit()
    link = repo.transfer_local_ownership(
        session,
        from_telegram_id=111,
        to_telegram_id=222,
        subscription=sub,
    )
    session.commit()
    assert link.telegram_id == 222
    assert repo.get_link_for_subscription(session, sub.id).telegram_id == 222
    with pytest.raises(ValueError, match="not_owner"):
        repo.transfer_local_ownership(
            session,
            from_telegram_id=111,
            to_telegram_id=222,
            subscription=sub,
        )
    with pytest.raises(ValueError, match="same_owner"):
        repo.transfer_local_ownership(
            session,
            from_telegram_id=222,
            to_telegram_id=222,
            subscription=sub,
        )


def test_set_local_owner_requires_profile(session: Session) -> None:
    sub = _sub(session, "svc_three")
    with pytest.raises(ValueError, match="recipient_profile_missing"):
        repo.set_local_owner(
            session,
            to_telegram_id=333,
            initiated_by_telegram_id=999,
            source="admin_assign",
            subscription=sub,
        )


def test_usable_username_blocks_active_allows_removed(session: Session) -> None:
    _sub(session, "taken_name", status="active")
    assert repo.is_usable_username_available(session, "taken_name") is False
    _sub(session, "free_again", status="removed_by_vendor")
    assert repo.is_usable_username_available(session, "free_again") is True


def test_partial_unique_usable_username_index(session: Session) -> None:
    engine = session.get_bind()
    _sub(session, "dup_ok", status="removed_by_vendor")
    session.commit()
    # Second historical/removed row with same username is allowed by partial index.
    vendor = session.scalar(select(Vendor).limit(1))
    assert vendor
    session.add(
        Subscription(
            vendor_id=vendor.id,
            username="dup_ok",
            status="removed_by_vendor",
        )
    )
    session.commit()
    # Active + another active with same username must fail at DB level.
    session.add(
        Subscription(vendor_id=vendor.id, username="live_dup", status="active")
    )
    session.commit()
    session.add(
        Subscription(vendor_id=vendor.id, username="live_dup", status="warning")
    )
    with pytest.raises(Exception):
        session.commit()
    session.rollback()


def test_apply_status_snapshot_stores_used_and_expiry(session: Session) -> None:
    vendor = session.scalar(select(Vendor).limit(1))
    assert vendor
    data = {
        "username": "snap_user",
        "kind": "status_active",
        "vendor_service_id": "deadbeef",
        "product_name": "55 گیگ کاربر نامحدود یکماهه🔸 105 هزار تومن",
        "location": "loc",
        "traffic_limit_mb": 25600.0,
        "used_traffic_mb": 100.0,
        "remaining_traffic_mb": 25500.0,
        "remaining_days": 10,
        "expiration_date": "2026-12-01",
    }
    sub = repo.apply_status_snapshot(session, vendor.id, data)
    session.commit()
    assert sub is not None
    assert sub.status == "active"
    assert sub.used_traffic_mb == 100.0
    assert str(sub.expiration_date).startswith("2026-12-01")
    assert sub.product_name == "۵۵ گیگ یکماهه"


def test_local_product_name_helpers() -> None:
    from app.utils.text import (
        format_local_product_name,
        local_product_name_from_vendor_text,
    )

    assert format_local_product_name(traffic_gb=25, duration_days=31) == "۲۵ گیگ یکماهه"
    assert format_local_product_name(traffic_gb=100, duration_days=91) == "۱۰۰ گیگ سه ماهه"
    assert (
        local_product_name_from_vendor_text(
            "100 گیگ کاربر نامحدود سه‌ماهه🔸 200 هزار تومن"
        )
        == "۱۰۰ گیگ سه ماهه"
    )
    assert (
        local_product_name_from_vendor_text(
            "🔶50 گیگ حجمی🔸کاربر و زمان نامحدود🔸360 ت"
        )
        == "۵۰ گیگ نامحدود زمانی"
    )
    assert format_local_product_name(traffic_gb=100, duration_days=None) == (
        "۱۰۰ گیگ نامحدود زمانی"
    )


def test_bulk_assign_unlinked(session: Session) -> None:
    a = _sub(session, "bulk_a")
    b = _sub(session, "bulk_b")
    c = _sub(session, "bulk_c")
    session.commit()
    repo.set_local_owner(
        session,
        to_telegram_id=222,
        initiated_by_telegram_id=999,
        source="admin_assign",
        subscription=c,
    )
    session.commit()
    assert repo.count_unlinked_subscriptions(session) == 2
    events = repo.bulk_assign_unlinked(
        session,
        to_telegram_id=111,
        initiated_by_telegram_id=999,
        note="test_bulk",
    )
    session.commit()
    assert len(events) == 2
    assert {e.service_username for e in events} == {"bulk_a", "bulk_b"}
    assert repo.count_unlinked_subscriptions(session) == 0
    link_a = repo.get_link_for_subscription(session, a.id)
    link_b = repo.get_link_for_subscription(session, b.id)
    link_c = repo.get_link_for_subscription(session, c.id)
    assert link_a and link_a.telegram_id == 111
    assert link_b and link_b.telegram_id == 111
    assert link_c and link_c.telegram_id == 222


def test_migrate_adds_columns(session: Session) -> None:
    cols = {
        r[1]
        for r in session.execute(text("PRAGMA table_info(subscriptions)")).fetchall()
    }
    assert "subscription_url" in cols
    assert "used_traffic_mb" in cols
    assert "last_refresh_method" in cols
    assert "vendor_service_id" in cols
    assert "product_name" in cols
    assert "created_at" in cols
    assert "last_userinfo_at" in cols
    for retired in (
        "customer_code",
        "location",
        "last_refresh_at",
        "last_refresh_error",
        "last_alert_type",
        "last_alert_at",
        "last_updated",
    ):
        assert retired not in cols
    vcols = {
        r[1] for r in session.execute(text("PRAGMA table_info(vendors)")).fetchall()
    }
    assert "info_refresh_strategy" in vcols
    tables = {
        r[0]
        for r in session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    assert "subscription_ownership_events" not in tables
    assert "customer_service_links" in tables
