"""Phase 6: local almost-out scan + provider-delete inventory removal."""
from __future__ import annotations

from sqlalchemy import create_engine, select
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
from app.services.lifecycle_alerts import scan_almost_out_alerts


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _migrate_sqlite(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    s.add(
        Vendor(
            key="netplus",
            name="NetPlus",
            telegram_username="bot",
            info_refresh_strategy="automatic",
        )
    )
    s.add(CustomerProfile(telegram_id=111, username="a", full_name="A"))
    s.add(CustomerProfile(telegram_id=222, username="b", full_name="B"))
    s.commit()
    repo.ensure_default_bot_messages(s)
    s.commit()
    return s


def _owned_sub(session: Session, username: str, **fields) -> Subscription:
    vendor = session.scalar(select(Vendor).limit(1))
    assert vendor
    sub = Subscription(
        vendor_id=vendor.id,
        username=username,
        status="active",
        **fields,
    )
    session.add(sub)
    session.flush()
    session.add(
        CustomerServiceLink(
            telegram_id=111,
            service_username=username,
            subscription_id=sub.id,
            vendor_id=vendor.id,
        )
    )
    session.flush()
    return sub


def test_scan_almost_out_days_and_cooldown() -> None:
    s = _session()
    repo.set_alert_days_threshold(s, 3)
    repo.set_alert_traffic_mb_threshold(s, 100)
    repo.set_lifecycle_alert_cooldown_seconds(s, 86400)
    from datetime import datetime, timedelta

    sub = _owned_sub(
        s,
        "low_days",
        expiration_date=datetime.utcnow() + timedelta(days=2, hours=3),
        remaining_traffic_mb=5000,
    )
    s.commit()
    msgs = scan_almost_out_alerts(s)
    s.commit()
    assert any(m["kind"] == "days" and m["telegram_id"] == 111 for m in msgs)
    assert sub.last_days_alert_at is not None
    # Second scan within cooldown → no duplicate days alert
    msgs2 = scan_almost_out_alerts(s)
    assert not any(m["kind"] == "days" for m in msgs2)
    s.close()


def test_scan_almost_out_traffic() -> None:
    s = _session()
    repo.set_alert_traffic_mb_threshold(s, 1024)
    from datetime import datetime, timedelta

    _owned_sub(
        s,
        "low_traf",
        expiration_date=datetime.utcnow() + timedelta(days=20),
        remaining_traffic_mb=100,
    )
    s.commit()
    msgs = scan_almost_out_alerts(s)
    assert any(m["kind"] == "traffic" for m in msgs)
    s.close()


def test_remove_subscription_after_provider_delete() -> None:
    s = _session()
    from datetime import datetime, timedelta

    sub = _owned_sub(
        s, "gone_svc", expiration_date=datetime.utcnow() + timedelta(days=10)
    )
    s.commit()
    info = repo.remove_subscription_after_provider_delete(s, "gone_svc")
    s.commit()
    assert info is not None
    assert info["telegram_id"] == 111
    assert info["username"] == "gone_svc"
    assert session_get_sub(s, "gone_svc") is None
    assert repo.get_link_for_username(s, "gone_svc") is None
    assert repo.is_usable_username_available(s, "gone_svc") is True
    s.close()


def session_get_sub(session: Session, username: str) -> Subscription | None:
    return session.scalar(
        select(Subscription).where(Subscription.username == username).limit(1)
    )


def test_lifecycle_message_defaults_seeded() -> None:
    s = _session()
    for key in (
        "alert_almost_out_days",
        "alert_almost_out_traffic",
        "alert_service_deleted",
    ):
        text = repo.get_bot_message(s, key, username="x", days=1, traffic="1 گیگ", product="p")
        assert "x" in text or "سرویس" in text
    s.close()
