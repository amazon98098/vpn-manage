"""NetPlus top-up with 20000 toman to reveal card instructions. Never confirm paid."""
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
OUT = BASE / "netplus_card_20k.json"
TOPUP_AMOUNT = "20000"


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
            incoming.append({"edited": True, **(msg_dump(event.message) or {})})

    await client.send_message(entity, "/start")
    await asyncio.sleep(3.5)
    await client.send_message(entity, "🏦 کیف پول + شارژ")
    await asyncio.sleep(3.5)

    msgs = await client.get_messages(entity, limit=6)
    wallet = next((m for m in msgs if m and m.buttons), None)
    for row in wallet.buttons:
        for btn in row:
            if getattr(btn, "data", None) and btn.data.decode() == "Add_Balance":
                await btn.click()
                break
    await asyncio.sleep(3.0)

    await client.send_message(entity, TOPUP_AMOUNT)
    await asyncio.sleep(4.5)
    method = next(
        (
            m
            for m in await client.get_messages(entity, limit=8)
            if m and m.message and "روش پرداخت" in m.message
        ),
        None,
    )
    steps.append({"step": "method", "message": msg_dump(method), "amount": TOPUP_AMOUNT})

    click_result = None
    for row in method.buttons:
        for btn in row:
            if getattr(btn, "data", None) and btn.data.decode() == "cart_to_offline":
                click_result = await btn.click()
                steps.append({
                    "step": "card_click",
                    "callback_message": getattr(click_result, "message", None),
                    "alert": getattr(click_result, "alert", None),
                    "url": getattr(click_result, "url", None),
                })
                break

    await asyncio.sleep(10.0)
    recent = await client.get_messages(entity, limit=15)
    edited_method = await client.get_messages(entity, ids=method.id)

    steps.append({
        "step": "after_card",
        "callback_message": getattr(click_result, "message", None),
        "edited_method": msg_dump(edited_method),
        "incoming": incoming[-12:],
        "recent_in": [msg_dump(m) for m in recent if m and not m.out][:10],
    })

    texts = []
    if click_result and getattr(click_result, "message", None):
        texts.append(click_result.message)
    for m in recent:
        if m and not m.out and m.message:
            texts.append(m.message)
    for item in incoming:
        if item and item.get("text"):
            texts.append(item["text"])
    if edited_method and edited_method.message:
        texts.append(edited_method.message)
    combined = "\n---\n".join(texts)
    steps.append({"step": "extract", **extract(combined), "combined": combined[:6000]})

    # Cancel / close — never paid
    cancelled = False
    for m in [edited_method] + list(recent):
        if cancelled or not m or not m.buttons:
            continue
        for row in m.buttons:
            for btn in row:
                text = btn.text or ""
                raw = getattr(btn, "data", None)
                data = raw.decode() if raw else ""
                blob = f"{text} {data}".lower()
                if any(k in blob for k in ("پرداخت انجام", "واریز کردم", "paid", "ارسال رسید")):
                    steps.append({"step": "saw_paid_button_skipped", "text": text, "data": data})
                    continue
                if any(k in blob for k in ("لغو", "cancel", "بستن", "colselist", "انصراف", "بازگشت", "back")):
                    if raw:
                        await btn.click()
                        cancelled = True
                        steps.append({"step": "cancelled", "text": text, "data": data})
                        await asyncio.sleep(2.5)
                        break
            if cancelled:
                break

    if not cancelled:
        await client.send_message(entity, "/start")
        steps.append({"step": "cancelled_start"})

    OUT.write_text(json.dumps({"bot": BOT, "steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
