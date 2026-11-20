"""Timed renew drive for one username; stop when NetPlus shows card-to-card pay info."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.database.database import get_session, init_db
from app.database import repositories as repo
from app.telegram.user_client import build_user_client, ensure_authorized
from app.vendors.netplus_driver import NetPlusRenewDriver

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "302_naseri"
OUT = Path(__file__).resolve().parent / f"timed_renew_{USERNAME}.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("timed_renew")


async def main() -> None:
    settings = load_settings(require_bot=False)
    init_db(settings.db_url)
    service_id = None
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, USERNAME)
        if sub:
            service_id = sub.vendor_service_id
            log.info(
                "DB sub id=%s vendor_service_id=%s expire=%s",
                sub.id,
                service_id,
                sub.expiration_date,
            )
        else:
            log.warning("No local subscription row for %s", USERNAME)

    client = build_user_client(settings)
    await ensure_authorized(client)
    driver = NetPlusRenewDriver(client, settings.netplus_bot)

    t0 = time.monotonic()
    result = await driver.drive_renew(
        username=USERNAME,
        service_id=service_id,
        plan_mode="current",
    )
    total = time.monotonic() - t0
    pay = result.payment
    payload = {
        "username": USERNAME,
        "service_id": service_id,
        "ok": result.ok,
        "stage": result.stage,
        "error": result.error,
        "total_seconds": round(total, 2),
        "payment": None
        if not pay
        else {
            "amount_rial": pay.amount_rial,
            "card": pay.card,
            "holder": pay.holder,
            "message_id": pay.message_id,
            "text_preview": (pay.text or "")[:400],
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("DONE %s", json.dumps(payload, ensure_ascii=False))
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
