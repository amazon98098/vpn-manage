"""Probe NetPlus extend menu for alam_chehreh_155_1 — dump buttons after extend."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from app.vendors.netplus_driver import NetPlusRenewDriver, buttons_of, find_data, flat_buttons

SESSION = str(ROOT / "data" / "sessions" / "main_account")
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT = os.getenv("NETPLUS_BOT", "@Bombejokerbot")
USER = "alam_chehreh_155_1"
SID = "b106202c"


async def main() -> None:
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return
    driver = NetPlusRenewDriver(client, BOT)
    async with driver.lock:
        status = await driver.open_status(USER, SID)
        out = {
            "status_preview": (status.message or "")[:400] if status else None,
            "status_buttons": buttons_of(status),
        }
        extend = find_data(status, "extend_") if status else None
        out["extend_cb"] = extend
        menu = await driver.click_data(status, extend) if status and extend else None
        out["menu_preview"] = (menu.message or "")[:500] if menu else None
        out["menu_buttons"] = buttons_of(menu)
        # If duration present, click 31 and dump packages
        dur = None
        for cand in (
            "productextendmonths_31",
            "productextendmonths_61",
            "productextendmonths_91",
        ):
            if find_data(menu, cand, exact=True):
                dur = cand
                break
        if not dur and menu:
            for b in flat_buttons(menu):
                t = b.get("text") or ""
                d = b.get("callback_data") or ""
                if "ماه" in t or "گیگ" in t or "extend" in d or "month" in d:
                    out.setdefault("interesting", []).append(b)
        out["chosen_duration"] = dur
        if dur and menu:
            after = await driver.click_data(menu, dur)
            out["after_dur_preview"] = (after.message or "")[:500] if after else None
            out["after_dur_buttons"] = buttons_of(after)
        (ROOT / "probe" / "alam_extend_menu.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("WROTE probe/alam_extend_menu.json")
        print("menu buttons:")
        for b in flat_buttons(menu) if menu else []:
            print(" ", b)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
