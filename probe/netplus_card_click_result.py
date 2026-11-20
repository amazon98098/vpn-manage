"""Click NetPlus card-to-card and capture callback alert + any follow-up messages."""
import asyncio
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = str(BASE / "test_account")
BOT = "@Bombejokerbot"
OUT = BASE / "netplus_card_click_result.json"


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
        "has_media": bool(getattr(msg, "media", None)),
        "reply_markup_type": type(msg.reply_markup).__name__ if msg.reply_markup else None,
    }


def extract(text: str):
    return {
        "cards": re.findall(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", text or ""),
        "amounts": re.findall(
            r"[\d۰-۹]{1,3}(?:[.,٬]?[\d۰-۹]{3})+(?:\s*(?:تومان|ریال|تومن))?",
            text or "",
        ),
        "urls": re.findall(r"https?://\S+", text or ""),
    }


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    entity = await client.get_entity(BOT)
    steps = []
    incoming = []

    @client.on(events.NewMessage(chats=entity))
    async def on_new(event):
        if not event.out:
            incoming.append(msg_dump(event.message))

    @client.on(events.MessageEdited(chats=entity))
    async def on_edit(event):
        if not event.out:
            incoming.append({"edited": True, **msg_dump(event.message)})

    # Start clean
    await client.send_message(entity, "/start")
    await asyncio.sleep(3.5)

    await client.send_message(entity, "🏦 کیف پول + شارژ")
    await asyncio.sleep(3.5)
    msgs = await client.get_messages(entity, limit=5)
    wallet = next((m for m in msgs if m and m.buttons and any(
        getattr(b, "data", None) and b.data.decode() == "Add_Balance"
        for row in m.buttons for b in row
    )), None)
    steps.append({"step": "wallet", "message": msg_dump(wallet)})

    for row in wallet.buttons:
        for btn in row:
            if getattr(btn, "data", None) and btn.data.decode() == "Add_Balance":
                result = await btn.click()
                steps.append({"step": "add_balance_click_result", "result": repr(result), "message_attr": getattr(result, "message", None), "alert": getattr(result, "alert", None)})
                break

    await asyncio.sleep(3.0)
    edited_wallet = await client.get_messages(entity, ids=wallet.id)
    steps.append({"step": "after_add_balance", "message": msg_dump(edited_wallet)})

    await client.send_message(entity, "10000")
    await asyncio.sleep(4.5)
    method_msgs = await client.get_messages(entity, limit=6)
    method = next((m for m in method_msgs if m and m.message and "روش پرداخت" in m.message), None)
    steps.append({"step": "method", "message": msg_dump(method)})

    click_result = None
    for row in method.buttons:
        for btn in row:
            if getattr(btn, "data", None) and btn.data.decode() == "cart_to_offline":
                click_result = await btn.click()
                steps.append({
                    "step": "card_click_result",
                    "repr": repr(click_result),
                    "message": getattr(click_result, "message", None),
                    "alert": getattr(click_result, "alert", None),
                    "url": getattr(click_result, "url", None),
                })
                break

    # Wait for delayed messages / edits
    await asyncio.sleep(8.0)
    recent = await client.get_messages(entity, limit=12)
    steps.append({
        "step": "after_wait",
        "incoming_events": incoming,
        "recent": [msg_dump(m) for m in recent if m and not m.out][:8],
    })

    combined = "\n---\n".join(
        (m.get("text") if isinstance(m, dict) else (m.message or ""))
        for m in incoming
    )
    for m in recent:
        if m and not m.out and m.message:
            combined += "\n---\n" + m.message
    if click_result and getattr(click_result, "message", None):
        combined += "\n---\n" + click_result.message
    steps.append({"step": "extract", **extract(combined), "combined": combined[:5000]})

    # Cancel without paying
    for m in recent:
        if not m or not m.buttons:
            continue
        for row in m.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                text = btn.text or ""
                data = raw.decode() if raw else ""
                if any(k in f"{text} {data}".lower() for k in ("پرداخت انجام", "واریز کردم", "paid")):
                    continue
                if data in ("colselist", "account", "backuser") or "بستن" in text or "بازگشت" in text or "لغو" in text:
                    if raw:
                        await btn.click()
                        steps.append({"step": "cancelled", "text": text, "data": data})
                        await asyncio.sleep(2)
                        OUT.write_text(json.dumps({"bot": BOT, "steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
                        print(f"WROTE:{OUT}")
                        await client.disconnect()
                        return

    await client.send_message(entity, "/start")
    await asyncio.sleep(2)
    OUT.write_text(json.dumps({"bot": BOT, "steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
