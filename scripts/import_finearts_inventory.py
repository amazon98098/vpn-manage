"""Import FineArts inventory from probe export + private URLs into DB."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select

from app.config import load_settings
from app.database import repositories as repo
from app.database.database import get_session, init_db
from app.database.models import Subscription
from app.utils.text import local_product_name_from_vendor_text


def _gb_to_mb(gb: float | None) -> float | None:
    if gb is None:
        return None
    return float(gb) * 1024.0


def _status_from_raw(raw: str | None) -> str:
    t = raw or ""
    if "پایان حجم" in t:
        return "volume_exhausted"
    if "غیرفعال" in t:
        return "disabled"
    return "active"


def main() -> None:
    settings = load_settings(require_bot=False)
    init_db(settings.db_url)

    export_path = ROOT / "probe" / "finearts_inventory_export.json"
    urls_path = ROOT / "probe" / "finearts_private_urls.json"
    if not export_path.exists():
        raise SystemExit(f"Missing {export_path}")

    payload = json.loads(export_path.read_text(encoding="utf-8"))
    private_urls: dict[str, str] = {}
    if urls_path.exists():
        private_urls = json.loads(urls_path.read_text(encoding="utf-8"))

    inventory = payload.get("inventory") or []
    bot = settings.finearts_bot

    created = updated = skipped = 0
    with get_session() as session:
        vendor = repo.ensure_vendor(session, "finearts", "FineArts", bot)
        vendor.enabled = True
        for row in inventory:
            username = (row.get("username") or "").strip()
            if not username:
                continue
            sid = (row.get("vendor_service_id") or "").strip() or None
            sub = session.scalar(
                select(Subscription).where(
                    Subscription.vendor_id == vendor.id,
                    Subscription.username == username,
                )
            )
            if sub is None:
                existing = repo.get_subscription_by_username(session, username)
                if existing is not None:
                    print(
                        f"SKIP clash {username} already on vendor_id={existing.vendor_id}"
                    )
                    skipped += 1
                    continue
                sub = Subscription(vendor_id=vendor.id, username=username)
                session.add(sub)
                created += 1
            else:
                updated += 1

            if sid:
                sub.vendor_service_id = sid
            sub.traffic_limit_mb = _gb_to_mb(row.get("traffic_gb"))
            sub.used_traffic_mb = _gb_to_mb(row.get("used_gb"))
            remain = row.get("remain_gb")
            if remain is None and row.get("status_raw") and "پایان" in str(
                row.get("status_raw")
            ):
                sub.remaining_traffic_mb = 0.0
            else:
                sub.remaining_traffic_mb = _gb_to_mb(remain)
            sub.expiration_date = None
            sub.status = _status_from_raw(row.get("status_raw"))
            label = local_product_name_from_vendor_text(row.get("product_raw"))
            if label:
                sub.product_name = label
            url = private_urls.get(username)
            if url:
                sub.subscription_url = url
                sub.last_refresh_method = "import"
            session.flush()

        total = session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.vendor_id == vendor.id)
        )

    urls_attached = sum(
        1 for u in inventory if private_urls.get((u.get("username") or "").strip())
    )
    print(
        f"FineArts import: created={created} updated={updated} skipped={skipped} "
        f"vendor_total={total} urls_in_export={urls_attached}"
    )


if __name__ == "__main__":
    main()
