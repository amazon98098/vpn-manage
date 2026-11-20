"""Careful read-only exploration of the two vendor bots."""
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.custom.message import Message

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = str(BASE / "test_account")
BOTS = [
    os.environ.get("VENDOR_BOT_1", "@Bombejokerbot"),
    os.environ.get("VENDOR_BOT_2", "@FineArtsVPNBot"),
]
OUT = BASE / "explore_report.json"


def serialize_button_rows(msg: Message):
    rows = []
    if not msg.buttons:
        return rows
    for row in msg.buttons:
        row_out = []
        for btn in row:
            item = {"text": btn.text}
            if getattr(btn, "data", None) is not None:
                try:
                    item["callback_data"] = btn.data.decode("utf-8", errors="replace")
                except Exception:
                    item["callback_data"] = repr(btn.data)
            if getattr(btn, "url", None):
                item["url"] = btn.url
            row_out.append(item)
        rows.append(row_out)
    return rows


async def explore_one(client: TelegramClient, username: str) -> dict:
    entity = await client.get_entity(username)
    info = {
        "username": username,
        "id": entity.id,
        "title": getattr(entity, "title", None)
        or getattr(entity, "first_name", None),
        "bot": bool(getattr(entity, "bot", False)),
        "recent_messages": [],
    }
    messages = await client.get_messages(entity, limit=25)
    for msg in messages:
        if not msg:
            continue
        info["recent_messages"].append(
            {
                "id": msg.id,
                "date": msg.date.isoformat() if msg.date else None,
                "out": bool(msg.out),
                "text": msg.message or "",
                "has_media": bool(msg.media),
                "buttons": serialize_button_rows(msg),
            }
        )
    return info


async def main() -> None:
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return

    me = await client.get_me()
    report = {
        "account": {
            "id": me.id,
            "username": me.username,
            "first_name": me.first_name,
        },
        "bots": [],
    }

    for bot in BOTS:
        await asyncio.sleep(2.5)  # gentle pacing
        try:
            data = await explore_one(client, bot)
            report["bots"].append(data)
            print(f"OK:{bot}:{len(data['recent_messages'])}_messages")
        except Exception as exc:
            report["bots"].append({"username": bot, "error": str(exc)})
            print(f"ERR:{bot}:{exc}")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
