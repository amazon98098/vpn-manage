"""Phase 7: incremental inventory sync against live vendor list (no wipe).

- New on vendor → insert local row (no auto-assign)
- Still present → update vendor_service_id when provided
- Missing from live list → remove like provider delete (unlink + drop row)

Safety: if discovery looks incomplete (low count or history-only fallback),
only add/update — never remove.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.database.models import Subscription, USABLE_SUBSCRIPTION_STATUSES, Vendor

log = logging.getLogger(__name__)

MIN_LIVE_FOR_REMOVALS = 20
# FineArts inventory is smaller; allow removals once live discovery looks complete.
MIN_LIVE_FOR_REMOVALS_BY_VENDOR = {
    "netplus": 20,
    "finearts": 10,
}


@dataclass
class IncrementalSyncReport:
    discovered: int = 0
    live_n: int = 0
    hist_n: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    removals_skipped_reason: str | None = None
    customer_notices: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def apply_incremental_inventory_sync(
    session: Session,
    *,
    vendor: Vendor,
    items: list[dict[str, Any]],
    min_live_for_removals: int = MIN_LIVE_FOR_REMOVALS,
) -> IncrementalSyncReport:
    """Apply discovered NetPlus list items onto local usable inventory."""
    report = IncrementalSyncReport(discovered=len(items))
    live_items = [i for i in items if (i.get("source") or "live") != "history"]
    hist_items = [i for i in items if (i.get("source") or "") == "history"]
    report.live_n = len(live_items)
    report.hist_n = len(hist_items)

    # Prefer live list for identity; if only history, still upsert from that set.
    working = live_items if live_items else hist_items
    by_name: dict[str, dict[str, Any]] = {}
    for it in working:
        uname = (it.get("username") or "").strip()
        if not uname:
            report.errors.append("blank_username_skipped")
            continue
        by_name[uname] = it

    existing = list(
        session.scalars(
            select(Subscription).where(
                Subscription.vendor_id == vendor.id,
                Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)),
            )
        )
    )
    existing_by_name = {s.username: s for s in existing}

    for uname, it in by_name.items():
        sid = (it.get("id") or "").strip() or None
        sub = existing_by_name.get(uname)
        if sub is None:
            session.add(
                Subscription(
                    vendor_id=vendor.id,
                    username=uname,
                    vendor_service_id=sid,
                    status="unknown",
                    created_at=datetime.utcnow(),
                )
            )
            report.added += 1
            continue
        changed = False
        if sid and sub.vendor_service_id != sid:
            sub.vendor_service_id = sid
            changed = True
        if changed:
            report.updated += 1

    session.flush()

    allow_removals = bool(live_items) and len(by_name) >= min_live_for_removals
    if not allow_removals:
        if hist_items and not live_items:
            report.removals_skipped_reason = "history_fallback"
        elif len(by_name) < min_live_for_removals:
            report.removals_skipped_reason = (
                f"low_discovery:{len(by_name)}_lt_{min_live_for_removals}"
            )
        else:
            report.removals_skipped_reason = "removals_disabled"
        return report

    live_names = set(by_name.keys())
    # Re-read after inserts
    existing = list(
        session.scalars(
            select(Subscription).where(
                Subscription.vendor_id == vendor.id,
                Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)),
            )
        )
    )
    for sub in existing:
        if sub.username in live_names:
            continue
        owner = repo.remove_subscription_after_provider_delete(session, sub.username)
        report.removed += 1
        if owner:
            report.customer_notices.append(
                {
                    "telegram_id": owner["telegram_id"],
                    "username": owner["username"],
                    "text": repo.get_bot_message(
                        session,
                        "alert_service_deleted",
                        username=owner["username"],
                    ),
                }
            )

    session.flush()
    return report


async def run_manual_incremental_sync(
    *,
    renew_queue,
    netplus_bot_username: str | None = None,
    vendor_key: str = "netplus",
    vendor_name: str | None = None,
    bot_username: str | None = None,
    max_pages: int = 50,
) -> IncrementalSyncReport:
    """Fetch vendor list via queue lock, apply incremental DB sync."""
    from app.database.database import get_session

    key = (vendor_key or "netplus").strip().lower()
    name = vendor_name or ("FineArts" if key == "finearts" else "NetPlus")
    bot = bot_username or netplus_bot_username
    if not bot:
        raise ValueError("bot_username required for inventory sync")
    min_live = MIN_LIVE_FOR_REMOVALS_BY_VENDOR.get(key, MIN_LIVE_FOR_REMOVALS)

    items = await renew_queue.run_inventory_sync(
        max_pages=max_pages, vendor_key=key
    )
    with get_session() as session:
        vendor = repo.ensure_vendor(session, key, name, bot)
        report = apply_incremental_inventory_sync(
            session,
            vendor=vendor,
            items=items,
            min_live_for_removals=min_live,
        )
    return report
