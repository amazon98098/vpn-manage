"""Fetch NetPlus status for empty product_name rows; store our short catalog labels."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.database.database import get_session, init_db
from app.database import repositories as repo
from app.database.models import Subscription
from app.telegram.user_client import build_user_client, ensure_authorized
from app.utils.text import local_product_name_from_vendor_text
from app.vendors.netplus_driver import NetPlusRenewDriver
from sqlalchemy import or_, select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_product")


async def main() -> None:
    settings = load_settings(require_bot=False)
    init_db(settings.db_url)
    with get_session() as session:
        rows = list(
            session.scalars(
                select(Subscription).where(
                    or_(
                        Subscription.product_name.is_(None),
                        Subscription.product_name == "",
                    )
                )
            )
        )
        targets = [
            {
                "id": s.id,
                "username": s.username,
                "vendor_service_id": s.vendor_service_id,
            }
            for s in rows
        ]
    if not targets:
        log.info("Nothing to backfill")
        return

    client = build_user_client(settings)
    await ensure_authorized(client)
    driver = NetPlusRenewDriver(client, settings.netplus_bot)
    results = []
    for t in targets:
        uname = t["username"]
        sid = t["vendor_service_id"]
        log.info("Fetching status for %s", uname)
        try:
            data = await driver.refresh_subscription_status(
                username=uname, service_id=sid
            )
        except Exception as exc:
            log.exception("fail %s", uname)
            results.append({"username": uname, "ok": False, "error": str(exc)})
            continue
        raw_product = (data or {}).get("product_name")
        label = local_product_name_from_vendor_text(raw_product) if raw_product else None
        if not label:
            results.append(
                {
                    "username": uname,
                    "ok": False,
                    "error": "no_product",
                    "raw": raw_product,
                }
            )
            continue
        with get_session() as session:
            sub = session.get(Subscription, t["id"])
            if sub:
                sub.product_name = label
                # Also apply other useful fields if present (without vendor priced text)
                if data.get("expiration_date") is not None:
                    from app.database.repositories import _as_datetime

                    exp = _as_datetime(data.get("expiration_date"))
                    if exp is not None:
                        sub.expiration_date = exp
                for key in (
                    "traffic_limit_mb",
                    "used_traffic_mb",
                    "remaining_traffic_mb",
                ):
                    if data.get(key) is not None:
                        try:
                            setattr(sub, key, float(data[key]))
                        except (TypeError, ValueError):
                            pass
                if data.get("status"):
                    sub.status = str(data["status"])
                session.commit()
        results.append(
            {
                "username": uname,
                "ok": True,
                "label": label,
                "raw": raw_product,
            }
        )
        log.info("%s -> %s", uname, label)
        await asyncio.sleep(0.8)

    out = ROOT / "data" / "product_name_backfill_live.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ok_n = sum(1 for r in results if r.get("ok"))
    log.info("DONE ok=%s fail=%s -> %s", ok_n, len(results) - ok_n, out)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
