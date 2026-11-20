"""Gently walk vendor bot menus. No purchases/payments."""
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.tl.types import Channel, Chat, User

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = str(BASE / "test_account")
OUT = BASE / "menu_walk_report.json"


def buttons_of(msg):
    rows = []
    if not msg or not msg.buttons:
        return rows
    for row in msg.buttons:
        items = []
        for btn in row:
            item = {"text": btn.text}
            if getattr(btn, "data", None) is not None:
                item["callback_data"] = btn.data.decode("utf-8", errors="replace")
            if getattr(btn, "url", None):
                item["url"] = btn.url
            items.append(item)
        rows.append(items)
    return rows


def msg_dump(msg):
    if not msg:
        return None
    return {
        "id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "text": msg.message or "",
        "buttons": buttons_of(msg),
    }


async def wait_new(client, entity, after_id: int, timeout: float = 12.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(entity, limit=5)
        for m in msgs:
            if m and m.id > after_id and not m.out:
                return m
        await asyncio.sleep(1.2)
    return None


async def click_by_data(client, entity, msg, data: str):
    if not msg or not msg.buttons:
        return None
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if raw and raw.decode("utf-8", errors="replace") == data:
                before = msg.id
                await btn.click()
                await asyncio.sleep(2.5)
                # Prefer edited same message, else new message
                refreshed = await client.get_messages(entity, ids=msg.id)
                if refreshed and (refreshed.message != msg.message or buttons_of(refreshed) != buttons_of(msg)):
                    return refreshed
                return await wait_new(client, entity, before)
    return None


async def ensure_channel_joined(client, channel_username: str) -> dict:
    entity = await client.get_entity(channel_username)
    try:
        await client(functions.channels.JoinChannelRequest(entity))
        status = "joined_or_already"
    except Exception as exc:
        status = f"join_error:{exc}"
    await asyncio.sleep(2.0)
    return {"channel": channel_username, "id": entity.id, "status": status}


async def walk_finearts(client) -> dict:
    bot = await client.get_entity("@FineArtsVPNBot")
    result = {"bot": "@FineArtsVPNBot", "steps": []}

    # Get latest bot menu message with buttons
    msgs = await client.get_messages(bot, limit=15)
    menu = None
    for m in msgs:
        if m and not m.out and m.buttons:
            menu = m
            break
    if not menu:
        await client.send_message(bot, "/start")
        await asyncio.sleep(3)
        menu = await wait_new(client, bot, 0)
    result["steps"].append({"step": "main_menu", "message": msg_dump(menu)})

    # My services
    services = await click_by_data(client, bot, menu, "backorder")
    result["steps"].append({"step": "my_services", "message": msg_dump(services)})
    await asyncio.sleep(2.5)

    # Refresh main menu via /start again carefully
    before = (await client.get_messages(bot, limit=1))[0].id
    await client.send_message(bot, "/start")
    await asyncio.sleep(3)
    menu2 = await wait_new(client, bot, before)
    if not menu2:
        msgs = await client.get_messages(bot, limit=5)
        menu2 = next((m for m in msgs if m and m.buttons), None)
    result["steps"].append({"step": "main_menu_again", "message": msg_dump(menu2)})

    # Buy flow first screen only
    buy = await click_by_data(client, bot, menu2, "buy")
    result["steps"].append({"step": "buy_first_screen", "message": msg_dump(buy)})
    await asyncio.sleep(2.5)

    # Wallet
    before = (await client.get_messages(bot, limit=1))[0].id
    await client.send_message(bot, "/start")
    await asyncio.sleep(3)
    menu3 = await wait_new(client, bot, before)
    if not menu3:
        msgs = await client.get_messages(bot, limit=5)
        menu3 = next((m for m in msgs if m and m.buttons), None)
    wallet = await click_by_data(client, bot, menu3, "account")
    result["steps"].append({"step": "wallet", "message": msg_dump(wallet)})
    return result


async def walk_bombe(client) -> dict:
    bot = await client.get_entity("@Bombejokerbot")
    result = {"bot": "@Bombejokerbot", "steps": []}

    join_info = await ensure_channel_joined(client, "bombev2rayNgjoker")
    result["steps"].append({"step": "join_channel", "info": join_info})

    msgs = await client.get_messages(bot, limit=10)
    gate = next((m for m in msgs if m and m.buttons), None)
    result["steps"].append({"step": "before_confirm", "message": msg_dump(gate)})

    if gate:
        confirmed = await click_by_data(client, bot, gate, "confirmchannel")
        result["steps"].append({"step": "after_confirm_membership", "message": msg_dump(confirmed)})
        await asyncio.sleep(2.5)

    before = (await client.get_messages(bot, limit=1))[0].id
    await client.send_message(bot, "/start")
    await asyncio.sleep(3.5)
    menu = await wait_new(client, bot, before)
    if not menu:
        msgs = await client.get_messages(bot, limit=8)
        menu = next((m for m in msgs if m and not m.out), None)
    result["steps"].append({"step": "main_menu", "message": msg_dump(menu)})

    # Try common menu callbacks if present
    if menu and menu.buttons:
        for row in menu.buttons:
            for btn in row:
                data = getattr(btn, "data", None)
                if not data:
                    continue
                cb = data.decode("utf-8", errors="replace")
                # Skip payment-looking stuff
                if any(x in cb.lower() for x in ("pay", "card", "wallet", "factor")):
                    continue
                # Click first few non-url buttons to map menu, then stop
                clicked = await click_by_data(client, bot, menu, cb)
                result["steps"].append(
                    {
                        "step": f"click:{cb}",
                        "button_text": btn.text,
                        "message": msg_dump(clicked),
                    }
                )
                await asyncio.sleep(3.0)
                # Refresh menu for next click
                before = (await client.get_messages(bot, limit=1))[0].id
                await client.send_message(bot, "/start")
                await asyncio.sleep(3.0)
                menu = await wait_new(client, bot, before)
                if not menu:
                    msgs = await client.get_messages(bot, limit=8)
                    menu = next((m for m in msgs if m and m.buttons), None)
                # Only map a few top-level items
                if len([s for s in result["steps"] if s["step"].startswith("click:")]) >= 5:
                    break
            if len([s for s in result["steps"] if s["step"].startswith("click:")]) >= 5:
                break
    return result


async def main() -> None:
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return

    report = {"finearts": None, "bombe": None}
    await asyncio.sleep(2)
    report["finearts"] = await walk_finearts(client)
    print("FINEARTS_DONE")
    await asyncio.sleep(4)
    report["bombe"] = await walk_bombe(client)
    print("BOMBE_DONE")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
