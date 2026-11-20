"""Fast audit: local username must equal subscription URL filename exactly.

Clears mismatched URLs (does not talk to vendor bots).
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.database.database import get_session, init_db
from app.database.models import Subscription, USABLE_SUBSCRIPTION_STATUSES
from app.services.subscription_userinfo import (
    fetch_subscription_userinfo,
    normalize_url_username,
    url_username_matches,
)
from sqlalchemy import select


def _check(row: tuple[int, str, str]) -> dict:
    sub_id, username, url = row
    ui = fetch_subscription_userinfo(url, timeout=18.0)
    if not ui.ok:
        return {
            "id": sub_id,
            "username": username,
            "ok": False,
            "error": ui.error,
            "filename": None,
            "mismatch": False,
        }
    match = url_username_matches(username, ui.filename)
    return {
        "id": sub_id,
        "username": username,
        "ok": True,
        "error": None,
        "filename": ui.filename,
        "mismatch": not match,
        "want": normalize_url_username(username),
        "got": normalize_url_username(ui.filename),
    }


def main() -> None:
    settings = load_settings(require_bot=False)
    init_db(settings.db_url)
    with get_session() as session:
        rows = list(
            session.execute(
                select(
                    Subscription.id,
                    Subscription.username,
                    Subscription.subscription_url,
                ).where(
                    Subscription.subscription_url.is_not(None),
                    Subscription.subscription_url != "",
                    Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)),
                )
            )
        )
    work = [(int(i), str(u), str(url).strip()) for i, u, url in rows if str(url).startswith("http")]
    print(f"checking {len(work)} urls…")
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_check, item) for item in work]
        for fut in as_completed(futs):
            results.append(fut.result())

    mismatches = [r for r in results if r.get("mismatch")]
    errors = [r for r in results if not r.get("ok")]
    print(f"ok={len(results) - len(mismatches) - len(errors)} mismatch={len(mismatches)} error={len(errors)}")
    for r in sorted(mismatches, key=lambda x: x["username"]):
        print(f"MISMATCH id={r['id']} local={r['username']!r} url_file={r['filename']!r}")
    for r in sorted(errors, key=lambda x: x["username"])[:30]:
        print(f"ERROR id={r['id']} local={r['username']!r} err={r['error']}")

    if mismatches:
        clear_ids = [r["id"] for r in mismatches]
        with get_session() as session:
            for sid in clear_ids:
                sub = session.get(Subscription, sid)
                if sub:
                    print(f"CLEAR url for {sub.username}")
                    sub.subscription_url = None
            session.flush()
        print(f"cleared {len(clear_ids)} mismatched urls")


if __name__ == "__main__":
    main()
