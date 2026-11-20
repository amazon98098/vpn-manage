"""Refresh subscription usage from stored subscription_url (HTTP userinfo).

Never talks to the NetPlus Telegram bot. Respects admin-configured TTL unless force=True
(used after create / renew / change-link).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.database.models import Subscription, Vendor
from app.services.subscription_userinfo import (
    fetch_subscription_userinfo,
    url_username_matches,
)
from app.vendors.registry import vendor_supports_url_userinfo

log = logging.getLogger(__name__)


def userinfo_ttl_expired(sub: Subscription, ttl_seconds: int) -> bool:
    if ttl_seconds <= 0:
        return True
    if sub.last_userinfo_at is None:
        return True
    return datetime.utcnow() - sub.last_userinfo_at >= timedelta(seconds=ttl_seconds)


def recompute_subscription_status(
    session: Session,
    sub: Subscription,
    *,
    days_th: int | None = None,
    traffic_th: float | None = None,
) -> str:
    """Derive status from local traffic/expiry — no vendor bot required."""
    from app.utils.text import expire_within_days, is_expire_passed

    if days_th is None:
        days_th = repo.get_alert_days_threshold(session)
    if traffic_th is None:
        traffic_th = repo.get_alert_traffic_mb_threshold(session)

    if sub.status in ("removed_by_vendor", "disabled"):
        return sub.status

    rem = sub.remaining_traffic_mb
    if rem is not None and rem <= 0:
        sub.status = "volume_exhausted"
        return sub.status
    if is_expire_passed(sub.expiration_date):
        sub.status = "expired"
        return sub.status

    # Do NOT set status=warning from local thresholds — that polluted the
    # vendor-warnings page. Low days/traffic belong on almost-out only.
    # Preserve an existing vendor-warning status while vendor_alert_at is set.
    if getattr(sub, "vendor_alert_at", None) is not None and sub.status == "warning":
        return sub.status
    sub.status = "active"
    return sub.status


def apply_userinfo_fields(
    sub: Subscription,
    fields: dict,
    *,
    session: Session | None = None,
    days_th: int | None = None,
    traffic_th: float | None = None,
) -> None:
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
    if session is not None:
        # no_autoflush: avoid flushing dirty traffic rows while reading thresholds.
        with session.no_autoflush:
            recompute_subscription_status(
                session, sub, days_th=days_th, traffic_th=traffic_th
            )


def assign_subscription_url_if_matches(
    session: Session,
    sub: Subscription,
    url: str,
    *,
    timeout: float = 20.0,
    apply_traffic: bool = True,
) -> bool:
    """Store URL only when its filename header equals ``sub.username`` exactly.

    Fast check before any vendor-bot walk. On mismatch the URL is not stored.
    """
    clean = (url or "").strip()
    if not clean.startswith("http") or not sub:
        return False
    ui = fetch_subscription_userinfo(clean, timeout=timeout)
    if not ui.ok:
        log.info(
            "assign_url skipped username=%s err=%s",
            sub.username,
            ui.error,
        )
        return False
    if not url_username_matches(sub.username, ui.filename):
        log.warning(
            "assign_url refused: filename %r != username %r",
            ui.filename,
            sub.username,
        )
        return False
    sub.subscription_url = clean
    if apply_traffic:
        apply_userinfo_fields(sub, ui.as_subscription_fields(), session=session)
    else:
        sub.last_refresh_method = "subscription_url"
    session.flush()
    return True


def refresh_subscription_userinfo(
    session: Session,
    *,
    subscription: Subscription | None = None,
    username: str | None = None,
    force: bool = False,
    timeout: float = 20.0,
    days_th: int | None = None,
    traffic_th: float | None = None,
) -> bool:
    """Update usage/expiry from stored URL when TTL expired (or force).

    Returns True if a successful HTTP userinfo apply happened.
    Optional days_th/traffic_th avoid threshold SELECTs during bulk scans.
    """
    sub = subscription
    if sub is None and username:
        sub = repo.get_subscription_by_username(session, username)
    if sub is None:
        return False
    url = (sub.subscription_url or "").strip()
    if not url.startswith("http"):
        return False

    vendor: Vendor | None = None
    if sub.vendor_id:
        with session.no_autoflush:
            vendor = session.get(Vendor, sub.vendor_id)
    vendor_key = vendor.key if vendor else repo.DEFAULT_VENDOR_KEY
    strategy = (
        getattr(vendor, "info_refresh_strategy", None) if vendor else None
    ) or "automatic"
    if not vendor_supports_url_userinfo(
        vendor_key=vendor_key, info_refresh_strategy=strategy
    ):
        return False

    with session.no_autoflush:
        ttl = repo.get_userinfo_ttl_seconds(session)
    if not force and not userinfo_ttl_expired(sub, ttl):
        return False

    # HTTP outside any pending flush pressure from other writers.
    ui = fetch_subscription_userinfo(url, timeout=timeout)
    if not ui.ok:
        log.info(
            "userinfo refresh skipped/fail username=%s err=%s",
            sub.username,
            ui.error,
        )
        return False

    # Fast identity check: content-disposition filename must equal local username.
    if not url_username_matches(sub.username, ui.filename):
        log.warning(
            "userinfo refused: URL filename %r != local username %r — clearing URL",
            ui.filename,
            sub.username,
        )
        sub.subscription_url = None
        session.flush()
        return False

    apply_userinfo_fields(
        sub,
        ui.as_subscription_fields(),
        session=session,
        days_th=days_th,
        traffic_th=traffic_th,
    )
    session.flush()
    return True
