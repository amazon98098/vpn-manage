"""Read-only dump of a vendor bot chat. Usage: python dump_history.py <test|main> <@bot> [limit]"""
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSIONS = {"test": "test_account", "main": "main_account"}


def buttons_of(msg):
    rows = []
    if not msg.buttons:
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


async def main() -> None:
    account = sys.argv[1]
    bot = sys.argv[2]
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None

    client = TelegramClient(str(BASE / SESSIONS[account]), API_ID, API_HASH)
    client.flood_sleep_threshold = 120
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return

    entity = await client.get_entity(bot)
    total = (await client.get_messages(entity, limit=0)).total
    print(f"TOTAL:{total}")

    out = []
    count = 0
    async for msg in client.iter_messages(entity, limit=limit, wait_time=1.5):
        out.append(
            {
                "id": msg.id,
                "date": msg.date.isoformat() if msg.date else None,
                "out": bool(msg.out),
                "text": msg.message or "",
                "media": type(msg.media).__name__ if msg.media else None,
                "edit_date": msg.edit_date.isoformat() if msg.edit_date else None,
                "reply_markup": type(msg.reply_markup).__name__ if msg.reply_markup else None,
                "buttons": buttons_of(msg),
            }
        )
        count += 1
        if count % 500 == 0:
            print(f"PROGRESS:{count}")

    name = bot.lstrip("@").lower()
    path = BASE / f"history_{account}_{name}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"WROTE:{path}:{count}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
