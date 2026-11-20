"""Full provider inventory replacement from NetPlus (gated destructive rebuild).

Wipes local subscriptions + ownership links only. Does not touch profiles, orders,
wallet, or payment history. Never logs subscription URL tokens or config bodies.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update

from app.database.database import get_session
from app.database.models import RenewJob, Subscription
from app.database import repositories as repo
from app.services.subscription_userinfo import (
    fetch_subscription_userinfo,
    redact_subscription_url,
)
from app.vendors.netplus_driver import NetPlusRenewDriver

log = logging.getLogger(__name__)


@dataclass
class RebuildReport:
    started_at: str
    finished_at: str | None = None
    backup_path: str | None = None
    wiped_subscriptions: int = 0
    wiped_links: int = 0
    nulled_renew_job_fks: int = 0
    discovered: int = 0
    inserted: int = 0
    status_ok: int = 0
    status_fail: int = 0
    url_ok: int = 0
    url_fail: int = 0
    userinfo_ok: int = 0
    userinfo_fail: int = 0
    protected_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    sample_usernames_len: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def backup_database(db_path: Path, backup_dir: Path | None = None) -> Path:
    backup_dir = backup_dir or db_path.parent
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"vpn_manager.db.bak.phase2_{stamp}"
    shutil.copy2(db_path, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(dest) + suffix))
    log.info("DB backup written to %s", dest)
    return dest


def _count(session, table: str) -> int:
    return int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def wipe_provider_inventory(session) -> tuple[int, int, int]:
    """Delete subscriptions + links; null renew_jobs.subscription_id."""
    nulled = session.execute(
        update(RenewJob)
        .where(RenewJob.subscription_id.is_not(None))
        .values(subscription_id=None)
    )
    links = _count(session, "customer_service_links")
    subs = _count(session, "subscriptions")
    session.execute(text("DELETE FROM customer_service_links"))
    session.execute(text("DELETE FROM subscriptions"))
    session.flush()
    return int(subs), int(links), int(nulled.rowcount or 0)


async def rebuild_inventory_from_netplus(
    *,
    driver: NetPlusRenewDriver,
    db_path: Path,
    netplus_bot_username: str,
    http_timeout: float = 20.0,
    max_pages: int = 50,
    enrich: bool = True,
    execute_wipe: bool = True,
) -> RebuildReport:
    """Full replacement import. execute_wipe=False skips DB changes (list only)."""
    report = RebuildReport(started_at=datetime.utcnow().isoformat() + "Z")
    with get_session() as session:
        report.protected_counts = {
            "customer_profiles": _count(session, "customer_profiles"),
            "customer_orders": _count(session, "customer_orders"),
            "wallet_ledger": _count(session, "wallet_ledger"),
            "renew_jobs": _count(session, "renew_jobs"),
            "incoming_messages": _count(session, "incoming_messages"),
        }
        before_protected = {
            k: report.protected_counts[k]
            for k in ("customer_profiles", "customer_orders", "wallet_ledger")
        }

    # Discover BEFORE wipe so a failed list open cannot destroy inventory blindly.
    if execute_wipe:
        backup = backup_database(db_path)
        report.backup_path = str(backup)

    log.info("NetPlus inventory list walk starting…")
    items = await driver.sync_service_inventory(max_pages=max_pages)
    report.discovered = len(items)
    report.sample_usernames_len = [len((i.get("username") or "")) for i in items[:5]]
    log.info("Discovered %s services from live list", report.discovered)

    min_expected = 20
    if execute_wipe and report.discovered < min_expected:
        report.errors.append(
            f"abort_wipe_low_discovery:{report.discovered}_lt_{min_expected}"
        )
        report.finished_at = datetime.utcnow().isoformat() + "Z"
        log.error(
            "Aborting wipe/import: discovered %s < %s (possible list UI failure)",
            report.discovered,
            min_expected,
        )
        return report

    if not execute_wipe:
        report.finished_at = datetime.utcnow().isoformat() + "Z"
        return report

    with get_session() as session:
        w_subs, w_links, w_null = wipe_provider_inventory(session)
        report.wiped_subscriptions = w_subs
        report.wiped_links = w_links
        report.nulled_renew_job_fks = w_null
        after = {
            "customer_profiles": _count(session, "customer_profiles"),
            "customer_orders": _count(session, "customer_orders"),
            "wallet_ledger": _count(session, "wallet_ledger"),
        }
        if after != before_protected:
            raise RuntimeError("protected_app_data_changed_during_wipe")

    with get_session() as session:
        vendor = repo.ensure_vendor(
            session,
            key="netplus",
            name="NetPlus",
            username=netplus_bot_username,
        )
        if not getattr(vendor, "info_refresh_strategy", None):
            vendor.info_refresh_strategy = "automatic"
        vendor_id = vendor.id

        for it in items:
            uname = (it.get("username") or "").strip()
            sid = (it.get("id") or "").strip() or None
            if not uname:
                report.errors.append("blank_username_skipped")
                continue
            session.add(
                Subscription(
                    vendor_id=vendor_id,
                    username=uname,
                    vendor_service_id=sid,
                    status="unknown",
                )
            )
            report.inserted += 1
        session.flush()

        rows = list(
            session.scalars(
                select(Subscription)
                .where(Subscription.vendor_id == vendor_id)
                .order_by(Subscription.id.asc())
            )
        )
        enrich_targets = [(r.id, r.username, r.vendor_service_id) for r in rows]

    if not enrich:
        report.finished_at = datetime.utcnow().isoformat() + "Z"
        return report

    for idx, (sub_id, uname, sid) in enumerate(enrich_targets, start=1):
        log.info("Enrich %s/%s username_len=%s", idx, len(enrich_targets), len(uname))
        try:
            data, url = await driver.fetch_status_and_subscription_url(uname, sid)
        except Exception as e:
            report.status_fail += 1
            report.url_fail += 1
            report.errors.append(f"enrich_exc:{type(e).__name__}")
            continue

        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                continue
            if data:
                data["username"] = data.get("username") or uname
                repo.apply_status_snapshot(session, vendor_id, data)
                sub = session.get(Subscription, sub_id)
                assert sub
                kind = data.get("kind")
                if kind == "status_volume_exhausted":
                    sub.status = "volume_exhausted"
                elif kind == "status_disabled":
                    sub.status = "disabled"
                elif kind == "status_expired":
                    sub.status = "expired"
                elif kind == "status_active":
                    sub.status = "active"
                sub.last_refresh_method = "provider_bot"
                report.status_ok += 1
            else:
                report.status_fail += 1

            if url:
                from app.services.subscription_refresh import (
                    assign_subscription_url_if_matches,
                )

                if assign_subscription_url_if_matches(
                    session, sub, url, apply_traffic=False
                ):
                    report.url_ok += 1
                else:
                    report.url_fail += 1
            else:
                report.url_fail += 1
            session.flush()
            stored_url = sub.subscription_url

        if not stored_url:
            report.userinfo_fail += 1
            continue

        ui = fetch_subscription_userinfo(stored_url, timeout=http_timeout)
        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                continue
            if ui.ok:
                fields = ui.as_subscription_fields()
                for key in (
                    "traffic_limit_mb",
                    "used_traffic_mb",
                    "remaining_traffic_mb",
                    "expiration_date",
                ):
                    if fields.get(key) is not None:
                        setattr(sub, key, fields[key])
                sub.last_refresh_method = "subscription_url"
                sub.last_userinfo_at = datetime.utcnow()
                report.userinfo_ok += 1
            else:
                report.userinfo_fail += 1
                log.info(
                    "userinfo fail username_len=%s err=%s redacted=%s",
                    len(uname),
                    ui.error,
                    redact_subscription_url(stored_url),
                )

    with get_session() as session:
        report.protected_counts = {
            "customer_profiles": _count(session, "customer_profiles"),
            "customer_orders": _count(session, "customer_orders"),
            "wallet_ledger": _count(session, "wallet_ledger"),
            "subscriptions": _count(session, "subscriptions"),
            "customer_service_links": _count(session, "customer_service_links"),
        }

    report.finished_at = datetime.utcnow().isoformat() + "Z"
    return report
