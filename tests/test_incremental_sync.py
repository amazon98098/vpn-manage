"""Phase 7: incremental inventory sync (add / update / safe remove)."""
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
from app.services.inventory_incremental_sync import apply_incremental_inventory_sync


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
    s.commit()
    repo.ensure_default_bot_messages(s)
    s.commit()
    return s


def _vendor(s: Session) -> Vendor:
    v = s.scalar(select(Vendor).limit(1))
    assert v
    return v


def _sub(s: Session, username: str, sid: str | None = None) -> Subscription:
    v = _vendor(s)
    sub = Subscription(
        vendor_id=v.id,
        username=username,
        vendor_service_id=sid,
        status="active",
    )
    s.add(sub)
    s.flush()
    return sub


def test_incremental_adds_and_updates() -> None:
    s = _session()
    v = _vendor(s)
    old = _sub(s, "keep_me", sid="aaa")
    s.commit()
    items = [
        {"username": "keep_me", "id": "bbb", "source": "live"},
        {"username": "brand_new", "id": "ccc", "source": "live"},
    ]
    # Pad live list so removals would be allowed if something missing
    for i in range(20):
        items.append({"username": f"pad_{i}", "id": f"p{i}", "source": "live"})
    report = apply_incremental_inventory_sync(s, vendor=v, items=items)
    s.commit()
    assert report.added >= 1
    assert report.updated == 1
    s.refresh(old)
    assert old.vendor_service_id == "bbb"
    assert s.scalar(select(Subscription).where(Subscription.username == "brand_new"))
    s.close()


def test_incremental_removes_missing_when_live_complete() -> None:
    s = _session()
    v = _vendor(s)
    gone = _sub(s, "will_go")
    s.add(
        CustomerServiceLink(
            telegram_id=111,
            service_username="will_go",
            subscription_id=gone.id,
            vendor_id=v.id,
        )
    )
    s.commit()
    items = [{"username": f"keep_{i}", "id": str(i), "source": "live"} for i in range(25)]
    report = apply_incremental_inventory_sync(s, vendor=v, items=items)
    s.commit()
    assert report.removed >= 1
    assert s.scalar(select(Subscription).where(Subscription.username == "will_go")) is None
    assert repo.get_link_for_username(s, "will_go") is None
    # Removed services must not produce customer reminders.
    assert report.customer_notices == []
    s.close()


def test_history_fallback_skips_removals() -> None:
    s = _session()
    v = _vendor(s)
    _sub(s, "local_only")
    s.commit()
    items = [{"username": "hist_one", "id": "1", "source": "history"}]
    report = apply_incremental_inventory_sync(s, vendor=v, items=items)
    s.commit()
    assert report.removals_skipped_reason == "history_fallback"
    assert report.removed == 0
    assert s.scalar(select(Subscription).where(Subscription.username == "local_only"))
    assert s.scalar(select(Subscription).where(Subscription.username == "hist_one"))
    s.close()
