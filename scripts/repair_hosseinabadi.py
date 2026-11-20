"""Re-fetch hosseinabadi services from live NetPlus UI (no chat history)."""
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
from app.services.subscription_refresh import refresh_subscription_userinfo
from app.telegram.user_client import build_user_client, ensure_authorized
from app.vendors.netplus_driver import NetPlusRenewDriver

log = logging.getLogger("repair_hosseinabadi")
TARGETS = (
    "549_hosseinabadi",
    "185_hosseinabadi",
    "185_hosseinabadi_daughter",
)


async def repair_one(driver: NetPlusRenewDriver, username: str) -> None:
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        if not sub:
            print(f"MISSING {username}")
            return
        vsid = sub.vendor_service_id
        before = {
            "product": sub.product_name,
            "limit": sub.traffic_limit_mb,
            "used": sub.used_traffic_mb,
            "remain": sub.remaining_traffic_mb,
            "url": (sub.subscription_url or "")[:70],
            "vsid": vsid,
        }
    print(f"\n=== {username} vsid={vsid}")
    print(" before", before)

    data, url = await driver.fetch_status_and_subscription_url(username, vsid)
    if not data:
        print(" FAIL: could not open exact status")
        return

    with get_session() as session:
        vendor = repo.get_vendor_by_key(session, "netplus")
        if not vendor:
            print(" FAIL: no netplus vendor")
            return
        if url:
            data["subscription_url"] = url
        data["last_refresh_method"] = "provider_bot"
        sub = repo.apply_status_snapshot(session, vendor.id, data)
        if sub and url:
            sub.subscription_url = url
        # Prefer Clash headers for traffic when URL is fresh.
        if sub and (sub.subscription_url or "").startswith("http"):
            refresh_subscription_userinfo(session, subscription=sub, force=True)
        session.flush()
        after = {
            "product": sub.product_name if sub else None,
            "limit": sub.traffic_limit_mb if sub else None,
            "used": sub.used_traffic_mb if sub else None,
            "remain": sub.remaining_traffic_mb if sub else None,
            "url": ((sub.subscription_url or "")[:70] if sub else None),
            "vsid": sub.vendor_service_id if sub else None,
            "status": sub.status if sub else None,
        }
    print(" live_product", data.get("product_name"))
    print(" after", after)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings(require_bot=False)
    init_db(settings.db_url)
    client = build_user_client(settings)
    await ensure_authorized(client)
    driver = NetPlusRenewDriver(client, settings.netplus_bot)
    try:
        for uname in TARGETS:
            await repair_one(driver, uname)
            await asyncio.sleep(1.5)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
