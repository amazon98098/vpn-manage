"""Refresh named services from live vendor UI (exact username; no chat history / no NetPlus id)."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.database import repositories as repo
from app.database.database import get_session, init_db
from app.services.subscription_refresh import assign_subscription_url_if_matches
from app.telegram.user_client import build_user_client, ensure_authorized
from app.vendors.finearts_driver import FineArtsRenewDriver
from app.vendors.netplus_driver import NetPlusRenewDriver
from app.vendors.registry import DEFAULT_VENDOR_KEY

log = logging.getLogger("repair_named")


async def repair_one(drivers: dict, username: str) -> None:
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        if not sub:
            print(f"MISSING {username}")
            return
        vendor = repo.get_vendor(session, sub.vendor_id) if sub.vendor_id else None
        vkey = (vendor.key if vendor else DEFAULT_VENDOR_KEY) or DEFAULT_VENDOR_KEY
        before = {
            "product": sub.product_name,
            "status": sub.status,
            "limit": sub.traffic_limit_mb,
            "used": sub.used_traffic_mb,
            "remain": sub.remaining_traffic_mb,
            "expire": str(sub.expiration_date),
            "url": "SET" if sub.subscription_url else "NONE",
            "vendor": vkey,
        }
    print(f"\n=== {username} ({vkey})")
    print(" before", before)

    driver = drivers.get(vkey) or drivers[DEFAULT_VENDOR_KEY]
    data, url = await driver.fetch_status_and_subscription_url(username, None)
    if not data:
        print(" FAIL: exact status not opened")
        return

    with get_session() as session:
        vendor = repo.get_vendor_by_key(session, vkey)
        if not vendor:
            print(" FAIL: vendor missing")
            return
        data["last_refresh_method"] = "provider_bot"
        sub = repo.apply_status_snapshot(session, vendor.id, data)
        if not sub:
            print(" FAIL: apply_status_snapshot")
            return
        url_ok = False
        if url:
            url_ok = assign_subscription_url_if_matches(
                session, sub, url, apply_traffic=True
            )
        after = {
            "product": sub.product_name,
            "status": sub.status,
            "limit": sub.traffic_limit_mb,
            "used": sub.used_traffic_mb,
            "remain": sub.remaining_traffic_mb,
            "expire": str(sub.expiration_date),
            "url": "SET" if sub.subscription_url else "NONE",
            "url_assigned": url_ok,
            "live_product": data.get("product_name"),
        }
    print(" after", after)


async def main(names: list[str]) -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings(require_bot=False)
    init_db(settings.db_url)
    client = build_user_client(settings)
    await ensure_authorized(client)
    drivers = {
        "netplus": NetPlusRenewDriver(client, settings.netplus_bot),
        "finearts": FineArtsRenewDriver(client, settings.finearts_bot),
    }
    try:
        for name in names:
            await repair_one(drivers, name)
            await asyncio.sleep(1.5)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    targets = [a.strip() for a in sys.argv[1:] if a.strip()]
    if not targets:
        targets = ["373_yasna_hozhabri", "620_goodarzi_amlak"]
    asyncio.run(main(targets))
