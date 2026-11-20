"""Phase 5: TTL-gated HTTP userinfo refresh from stored subscription URL."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database.database import _migrate_sqlite
from app.database.models import Base, CustomerProfile, Subscription, Vendor
from app.database import repositories as repo
from app.services.subscription_refresh import (
    refresh_subscription_userinfo,
    userinfo_ttl_expired,
)
from app.services.subscription_userinfo import UserinfoResult


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
    s.add(CustomerProfile(telegram_id=1, username="u", full_name="U"))
    s.commit()
    return s


def test_userinfo_ttl_setting_roundtrip() -> None:
    s = _session()
    assert repo.get_userinfo_ttl_seconds(s) == 120
    assert repo.set_userinfo_ttl_seconds(s, 45) == 45
    assert repo.get_userinfo_ttl_seconds(s) == 45
    assert repo.set_userinfo_ttl_seconds(s, 0) == 0
    s.close()


def test_ttl_expired_logic() -> None:
    sub = Subscription(vendor_id=1, username="x", status="active")
    assert userinfo_ttl_expired(sub, 120) is True
    sub.last_userinfo_at = datetime.utcnow()
    assert userinfo_ttl_expired(sub, 120) is False
    sub.last_userinfo_at = datetime.utcnow() - timedelta(seconds=200)
    assert userinfo_ttl_expired(sub, 120) is True
    assert userinfo_ttl_expired(sub, 0) is True


def test_refresh_respects_ttl_and_force() -> None:
    s = _session()
    from sqlalchemy import select

    vendor = s.scalar(select(Vendor).limit(1))
    sub = Subscription(
        vendor_id=vendor.id,
        username="ttl_user",
        status="active",
        subscription_url="https://example.com/sub/abc",
        last_userinfo_at=datetime.utcnow(),
        remaining_traffic_mb=1.0,
    )
    s.add(sub)
    s.commit()
    repo.set_userinfo_ttl_seconds(s, 120)

    fake = UserinfoResult(
        ok=True,
        upload_bytes=0,
        download_bytes=1024 * 1024,
        total_bytes=10 * 1024 * 1024,
        expire_unix=int((datetime.utcnow() + timedelta(days=5)).timestamp()),
    )
    with patch(
        "app.services.subscription_refresh.fetch_subscription_userinfo",
        return_value=fake,
    ) as mocked:
        assert refresh_subscription_userinfo(s, subscription=sub, force=False) is False
        mocked.assert_not_called()
        assert refresh_subscription_userinfo(s, subscription=sub, force=True) is True
        mocked.assert_called_once()
        assert sub.used_traffic_mb is not None
        assert sub.last_userinfo_at is not None
    s.close()
