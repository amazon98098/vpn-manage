#!/usr/bin/env python3
"""Gated NetPlus inventory full rebuild (destructive to subscriptions/links only).

Usage:
  .venv/bin/python scripts/rebuild_netplus_inventory.py --execute
  .venv/bin/python scripts/rebuild_netplus_inventory.py --discover-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.database.database import init_db
from app.services.inventory_rebuild import rebuild_inventory_from_netplus
from app.telegram.user_client import build_user_client, ensure_authorized
from app.vendors.netplus_driver import NetPlusRenewDriver


async def _amain(args: argparse.Namespace) -> int:
    settings = load_settings(require_bot=False)
    init_db(settings.db_url)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    client = build_user_client(settings)
    await ensure_authorized(client)
    driver = NetPlusRenewDriver(client, settings.netplus_bot)
    try:
        report = await rebuild_inventory_from_netplus(
            driver=driver,
            db_path=settings.data_dir / "vpn_manager.db",
            netplus_bot_username=settings.netplus_bot,
            http_timeout=settings.info_http_timeout_seconds,
            enrich=not args.identity_only,
            execute_wipe=args.execute and not args.discover_only,
        )
    finally:
        await client.disconnect()

    out = settings.data_dir / "inventory_rebuild_report.json"
    out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    print(f"report_written={out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Rebuild local NetPlus inventory")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--execute",
        action="store_true",
        help="Backup, wipe subscriptions/links, import + enrich from NetPlus",
    )
    g.add_argument(
        "--discover-only",
        action="store_true",
        help="List walk only; no DB wipe/write",
    )
    p.add_argument(
        "--identity-only",
        action="store_true",
        help="With --execute: insert list identity only (skip status/URL/userinfo)",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
