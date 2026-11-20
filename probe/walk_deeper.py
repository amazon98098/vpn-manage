"""Deeper careful exploration. Stop before payment/charge confirmation."""
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = str(BASE / "test_account")
OUT = BASE / "deeper_walk_report.json"


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
        "reply_markup_type": type(msg.reply_markup).__name__ if msg.reply_markup else None,
    }


async def wait_new(client, entity, after_id: int, timeout: float = 15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(entity, limit=6)
        for m in msgs:
            if m and m.id > after_id and not m.out:
                return m
        await asyncio.sleep(1.2)
    msgs = await client.get_messages(entity, limit=3)
    return next((m for m in msgs if m and not m.out), None)


async def click_inline(client, entity, msg, data: str):
    if not msg or not msg.buttons:
        return None
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if raw and raw.decode("utf-8", errors="replace") == data:
                before = (await client.get_messages(entity, limit=1))[0].id
                await btn.click()
                await asyncio.sleep(2.8)
                refreshed = await client.get_messages(entity, ids=msg.id)
                if refreshed and (
                    refreshed.message != msg.message
                    or buttons_of(refreshed) != buttons_of(msg)
                ):
                    return refreshed
                return await wait_new(client, entity, before)
    return None


async def send_and_wait(client, entity, text: str):
    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, text)
    await asyncio.sleep(3.0)
    return await wait_new(client, entity, before)


async def finearts_deeper(client):
    bot = await client.get_entity("@FineArtsVPNBot")
    steps = []

    before = (await client.get_messages(bot, limit=1))[0].id
    await client.send_message(bot, "/start")
    await asyncio.sleep(3)
    menu = await wait_new(client, bot, before)
    steps.append({"step": "menu", "message": msg_dump(menu)})

    buy = await click_inline(client, bot, menu, "buy")
    steps.append({"step": "buy", "message": msg_dump(buy)})
    await asyncio.sleep(2)

    # Open cheapest package details only
    detail = await click_inline(client, bot, buy, "prodcutservices_8690")
    steps.append({"step": "product_50gb_detail", "message": msg_dump(detail)})
    await asyncio.sleep(2)

    # If it asks for username or shows confirm, record and go back if possible
    if detail and detail.buttons:
        # Prefer back buttons; avoid pay/confirm/balance spend
        back_data = None
        interesting = []
        for row in detail.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                if not raw:
                    continue
                cb = raw.decode("utf-8", errors="replace")
                interesting.append({"text": btn.text, "callback_data": cb})
                low = (btn.text or "").lower() + " " + cb.lower()
                if any(k in low for k in ("بازگشت", "back", "cancel", "لغو", "منو")):
                    back_data = cb
        steps.append({"step": "product_buttons_seen", "buttons": interesting})
        if back_data:
            back = await click_inline(client, bot, detail, back_data)
            steps.append({"step": "backed_out", "message": msg_dump(back)})
    return steps


async def bombe_deeper(client):
    bot = await client.get_entity("@Bombejokerbot")
    steps = []

    before = (await client.get_messages(bot, limit=1))[0].id
    await client.send_message(bot, "/start")
    await asyncio.sleep(3.5)
    menu = await wait_new(client, bot, before)
    steps.append({"step": "menu", "message": msg_dump(menu)})

    labels = [
        "💰 تعرفه اشتراک ها",
        "🛍 مشاهده سرویس های خریداری شده من",
        "♻️ تمدید سرویس",
        "🔐 خرید اشتراک",
        "🏦 کیف پول + شارژ",
    ]
    for label in labels:
        resp = await send_and_wait(client, bot, label)
        steps.append({"step": f"reply:{label}", "message": msg_dump(resp)})
        await asyncio.sleep(3.5)
        # Always return to start between branches to avoid deep payment paths
        before = (await client.get_messages(bot, limit=1))[0].id
        await client.send_message(bot, "/start")
        await asyncio.sleep(3.0)
        await wait_new(client, bot, before)
    return steps


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return

    report = {}
    report["finearts"] = await finearts_deeper(client)
    print("FINEARTS_DEEPER_DONE")
    await asyncio.sleep(4)
    report["bombe"] = await bombe_deeper(client)
    print("BOMBE_DEEPER_DONE")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
