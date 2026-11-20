"""Get NetPlus card-to-card payment instructions screen only. Never confirm paid."""
import asyncio
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = str(BASE / "test_account")
BOT = "@Bombejokerbot"
OUT = BASE / "netplus_card_screen.json"


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


def extract(text: str):
    return {
        "cards": re.findall(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", text or ""),
        "amounts": re.findall(
            r"[\d۰-۹]{1,3}(?:[.,٬]?[\d۰-۹]{3})+(?:\s*(?:تومان|ریال|تومن))?",
            text or "",
        ),
        "urls": re.findall(r"https?://\S+", text or ""),
    }


async def newest_after(client, entity, after_id: int, timeout: float = 20.0):
    deadline = asyncio.get_event_loop().time() + timeout
    found = []
    while asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(entity, limit=12)
        found = [m for m in msgs if m and m.id > after_id and not m.out]
        if found:
            found.sort(key=lambda m: m.id)
            return found
        await asyncio.sleep(1.0)
    return []


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return

    entity = await client.get_entity(BOT)
    steps = []

    # Clean start
    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, "/start")
    await asyncio.sleep(3.5)
    await newest_after(client, entity, before)

    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, "🏦 کیف پول + شارژ")
    await asyncio.sleep(3.5)
    wallet_msgs = await newest_after(client, entity, before)
    wallet = wallet_msgs[-1] if wallet_msgs else None
    steps.append({"step": "wallet", "message": msg_dump(wallet)})

    if not wallet or not wallet.buttons:
        raise RuntimeError("wallet screen missing")

    # Click Add_Balance
    before = (await client.get_messages(entity, limit=1))[0].id
    clicked = False
    for row in wallet.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if raw and raw.decode() == "Add_Balance":
                await btn.click()
                clicked = True
                break
        if clicked:
            break
    await asyncio.sleep(3.5)
    ask_msgs = await newest_after(client, entity, before - 1)
    # Also refresh wallet message in case it was edited
    edited = await client.get_messages(entity, ids=wallet.id)
    ask = edited if edited and "مبلغ" in (edited.message or "") else (ask_msgs[-1] if ask_msgs else edited)
    steps.append({"step": "ask_amount", "message": msg_dump(ask)})

    # Type amount
    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, "10000")
    await asyncio.sleep(4.0)
    method_msgs = await newest_after(client, entity, before)
    method = method_msgs[-1] if method_msgs else None
    steps.append({"step": "payment_method", "all_new": [msg_dump(m) for m in method_msgs], "message": msg_dump(method)})

    # Click card-to-card on the method message; also try edited copies
    target = None
    candidates = []
    if method:
        candidates.append(method)
    for m in method_msgs:
        candidates.append(m)
    # Include recent messages looking for cart_to_offline button
    recent = await client.get_messages(entity, limit=8)
    for m in recent:
        if m and m.buttons:
            candidates.append(m)

    before = (await client.get_messages(entity, limit=1))[0].id
    clicked_data = None
    for msg in candidates:
        if not msg or not msg.buttons:
            continue
        for row in msg.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                if not raw:
                    continue
                data = raw.decode("utf-8", errors="replace")
                if data == "cart_to_offline" or "کارت به کارت" in (btn.text or ""):
                    await btn.click()
                    clicked_data = {"text": btn.text, "callback_data": data, "on_message_id": msg.id}
                    target = msg
                    break
            if clicked_data:
                break
        if clicked_data:
            break

    steps.append({"step": "clicked_card_to_card", "clicked": clicked_data})
    await asyncio.sleep(4.5)

    new_msgs = await newest_after(client, entity, before)
    edited_target = await client.get_messages(entity, ids=target.id) if target else None
    recent_after = await client.get_messages(entity, limit=10)

    steps.append({
        "step": "after_card_click",
        "edited_original": msg_dump(edited_target),
        "new_messages": [msg_dump(m) for m in new_msgs],
        "recent": [msg_dump(m) for m in recent_after if m and not m.out][:6],
    })

    # Extract from all candidate texts
    texts = []
    for m in list(new_msgs) + ([edited_target] if edited_target else []) + list(recent_after):
        if m and not m.out and m.message:
            texts.append(m.message)
    combined = "\n---\n".join(texts)
    steps.append({"step": "extract_all", **extract(combined), "combined_preview": combined[:4000]})

    # Cancel: close list / back / start. Never paid.
    cancelled = False
    for m in [edited_target] + list(new_msgs) + list(recent_after):
        if cancelled or not m or not m.buttons:
            continue
        for row in m.buttons:
            for btn in row:
                text = btn.text or ""
                raw = getattr(btn, "data", None)
                data = raw.decode("utf-8", errors="replace") if raw else ""
                blob = f"{text} {data}".lower()
                if any(k in blob for k in ("پرداخت انجام", "واریز کردم", "paid", "ارسال رسید")):
                    steps.append({"step": "skipped_confirm_button", "text": text, "callback_data": data})
                    continue
                if any(k in blob for k in ("لغو", "cancel", "بستن", "colselist", "بازگشت", "back", "انصراف")):
                    if raw:
                        await btn.click()
                        await asyncio.sleep(2.5)
                        steps.append({"step": "cancelled_inline", "text": text, "callback_data": data})
                        cancelled = True
                        break
            if cancelled:
                break

    if not cancelled:
        before = (await client.get_messages(entity, limit=1))[0].id
        await client.send_message(entity, "/start")
        await asyncio.sleep(3)
        steps.append({"step": "cancelled_via_start", "message": msg_dump((await newest_after(client, entity, before) or [None])[-1] if await newest_after(client, entity, before) else None)})

    OUT.write_text(json.dumps({"bot": BOT, "steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
