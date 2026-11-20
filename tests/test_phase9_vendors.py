"""Phase 9: vendor registry + job vendor_id + URL userinfo strategy."""
from __future__ import annotations

from sqlalchemy import select

from app.database.database import get_session, init_db
from app.database import repositories as repo
from app.database.models import Subscription, Vendor
from app.vendors.base import VendorCapabilities
from app.vendors.registry import (
    DEFAULT_VENDOR_KEY,
    clear_vendor_registry,
    get_vendor_capabilities,
    get_vendor_driver,
    list_registered_vendor_keys,
    register_vendor_driver,
    vendor_supports_url_userinfo,
)


def setup_module() -> None:
    init_db("sqlite:////tmp/vpn_phase9_vendors_pytest.db")
    clear_vendor_registry()


def teardown_function() -> None:
    clear_vendor_registry()


class _FakeDriver:
    vendor_key = "netplus"

    async def drive_renew(self, **kwargs):
        return None

    async def drive_create(self, **kwargs):
        return None

    async def change_subscription_link(self, username, service_id=None):
        return None


def test_registry_register_and_capabilities() -> None:
    driver = _FakeDriver()
    key = register_vendor_driver(driver, get_vendor_capabilities("netplus"))
    assert key == DEFAULT_VENDOR_KEY
    assert get_vendor_driver("netplus") is driver
    assert DEFAULT_VENDOR_KEY in list_registered_vendor_keys()
    caps = get_vendor_capabilities("netplus")
    assert caps is not None
    assert caps.bot_renew is True
    assert caps.url_userinfo is True


def test_vendor_supports_url_userinfo_strategies() -> None:
    assert vendor_supports_url_userinfo(
        vendor_key="netplus", info_refresh_strategy="automatic"
    )
    assert vendor_supports_url_userinfo(
        vendor_key="netplus", info_refresh_strategy="subscription_url"
    )
    assert not vendor_supports_url_userinfo(
        vendor_key="netplus", info_refresh_strategy="provider_bot"
    )


def test_create_renew_job_sets_vendor_id() -> None:
    with get_session() as session:
        vendor = session.scalar(select(Vendor).where(Vendor.key == "netplus"))
        if not vendor:
            vendor = Vendor(
                key="netplus",
                name="NetPlus",
                telegram_username="netplus_bot",
                info_refresh_strategy="automatic",
            )
            session.add(vendor)
            session.flush()
        sub = repo.get_subscription_by_username(session, "phase9_u1")
        if sub:
            sub.vendor_id = vendor.id
            sub.status = "active"
            sub.vendor_service_id = "svc9"
        else:
            sub = Subscription(
                vendor_id=vendor.id,
                username="phase9_u1",
                status="active",
                vendor_service_id="svc9",
            )
            session.add(sub)
        session.flush()
        job = repo.create_renew_job(
            session,
            username="phase9_u1",
            vendor_service_id="svc9",
            subscription_id=sub.id,
            plan_mode="current",
            requested_by=1,
            kind="renew",
        )
        assert job.vendor_id == vendor.id
        resolved = repo.resolve_vendor_for_username(session, "phase9_u1")
        assert resolved is not None
        assert resolved.id == vendor.id


def test_unknown_vendor_capabilities_defaults() -> None:
    caps = get_vendor_capabilities("future_vendor")
    assert caps is None
    # Register a stub future vendor without claiming URL userinfo
    register_vendor_driver(
        _FakeDriver(),
        VendorCapabilities(
            key="future_vendor",
            display_name="Future",
            url_userinfo=False,
            bot_change_link=False,
        ),
    )
    caps2 = get_vendor_capabilities("future_vendor")
    assert caps2 is not None
    assert caps2.url_userinfo is False
    assert caps2.bot_change_link is False
    assert not vendor_supports_url_userinfo(
        vendor_key="future_vendor", info_refresh_strategy="automatic"
    )
