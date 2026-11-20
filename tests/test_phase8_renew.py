"""Phase 8: renew plan selection + job args from order."""
from __future__ import annotations

from sqlalchemy import select

from app.database.database import get_session, init_db
from app.database import repositories as repo
from app.database.models import PricePlan, Subscription, Vendor


def setup_module() -> None:
    init_db("sqlite:////tmp/vpn_phase8_renew_pytest.db")


def _ensure_vendor(session) -> Vendor:
    vendor = session.scalar(select(Vendor).limit(1))
    if vendor:
        return vendor
    vendor = Vendor(
        key="netplus",
        name="NetPlus",
        telegram_username="netplus_bot",
        info_refresh_strategy="automatic",
    )
    session.add(vendor)
    session.flush()
    return vendor


def test_list_enabled_duration_days_and_extend_callback() -> None:
    with get_session() as session:
        repo.ensure_default_price_plans(session)
        days = repo.list_enabled_duration_days(session)
        assert 31 in days
        assert repo.renew_extend_callback_for_days(31) == "productextendmonths_31"
        assert repo.renew_extend_callback_for_days(61) == "productextendmonths_61"
        assert repo.renew_extend_callback_for_days(91) == "productextendmonths_91"


def test_renew_job_args_current_vs_change() -> None:
    with get_session() as session:
        repo.ensure_default_price_plans(session)
        plans = repo.list_price_plans(session, enabled_only=True)
        assert plans
        plan = plans[0]
        order_cur = repo.create_customer_order(
            session,
            telegram_id=1,
            kind="renew",
            plan_id=plan.id,
            service_username="u_cur",
            price_toman=plan.price_toman,
            plan_mode="current",
        )
        mode, dur = repo.renew_job_args_from_order(session, order_cur)
        assert mode == "current"
        assert dur is None

        order_chg = repo.create_customer_order(
            session,
            telegram_id=1,
            kind="renew",
            plan_id=plan.id,
            service_username="u_chg",
            price_toman=plan.price_toman,
            plan_mode="change",
        )
        mode2, dur2 = repo.renew_job_args_from_order(session, order_chg)
        assert mode2 == "change"
        assert dur2 == repo.renew_extend_callback_for_days(plan.duration_days)

        order_legacy = repo.create_customer_order(
            session,
            telegram_id=1,
            kind="renew",
            plan_id=plan.id,
            service_username="u_leg",
            price_toman=plan.price_toman,
        )
        mode3, dur3 = repo.renew_job_args_from_order(session, order_legacy)
        assert mode3 == "current"
        assert dur3 is None


def test_find_renew_plan_matches_traffic() -> None:
    with get_session() as session:
        repo.ensure_default_price_plans(session)
        vendor = _ensure_vendor(session)
        existing = repo.get_subscription_by_username(session, "match55")
        if existing:
            existing.traffic_limit_mb = 55 * 1024
            existing.status = "active"
            existing.vendor_id = vendor.id
        else:
            session.add(
                Subscription(
                    vendor_id=vendor.id,
                    username="match55",
                    traffic_limit_mb=55 * 1024,
                    status="active",
                )
            )
        session.flush()
        plan = repo.find_renew_plan_for_username(session, "match55")
        assert plan is not None
        assert plan.traffic_gb is not None
        assert abs(float(plan.traffic_gb) - 55.0) < 0.1


def test_enabled_days_include_extra_duration_when_enabled() -> None:
    with get_session() as session:
        repo.ensure_default_price_plans(session)
        existing = session.scalar(
            select(PricePlan).where(PricePlan.key == "2m_55gb_test")
        )
        if not existing:
            session.add(
                PricePlan(
                    key="2m_55gb_test",
                    title="۵۵ گیگ دوماهه تست",
                    duration_days=61,
                    traffic_gb=55.0,
                    price_toman=300000,
                    enabled=True,
                    sort_order=99,
                )
            )
            session.flush()
        else:
            existing.enabled = True
            existing.duration_days = 61
            session.flush()
        days = repo.list_enabled_duration_days(session)
        assert 31 in days
        assert 61 in days
