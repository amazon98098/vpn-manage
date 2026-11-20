"""Enter minimum top-up amount on NetPlus to reveal card/payment screen. Do NOT confirm paid."""
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
OUT = BASE / "netplus_topup_amount.json"
AMOUNT_TO_TRY = "10000"


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


async def wait_bot(client, entity, after_id: int, timeout: float = 18.0):
    deadline = asyncio.get_event_loop().time() + timeout
    latest = None
    while asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(entity, limit=10)
        for m in msgs:
            if m and m.id > after_id and not m.out:
                return m
            if m and not m.out:
                latest = m
        await asyncio.sleep(1.2)
    return latest


async def click_inline(client, entity, msg, data: str):
    if not msg or not msg.buttons:
        return None
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if raw and raw.decode("utf-8", errors="replace") == data:
                before = (await client.get_messages(entity, limit=1))[0].id
                await btn.click()
                await asyncio.sleep(3.2)
                refreshed = await client.get_messages(entity, ids=msg.id)
                if refreshed and (
                    refreshed.message != msg.message
                    or buttons_of(refreshed) != buttons_of(msg)
                ):
                    return refreshed
                return await wait_bot(client, entity, before)
    return None


async def send_text(client, entity, text: str):
    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, text)
    await asyncio.sleep(3.5)
    return await wait_bot(client, entity, before)


def extract_pay_info(text: str):
    cards = re.findall(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", text or "")
    amounts = re.findall(
        r"[\d۰-۹]{1,3}(?:[.,٬]?[\d۰-۹]{3})+(?:\s*(?:تومان|ریال|تومن))?",
        text or "",
    )
    urls = re.findall(r"https?://\S+", text or "")
    return {"cards": cards, "amounts": amounts, "urls": urls}


async def cancel_out(client, entity, msg, steps):
    """Prefer cancel/back; never click paid."""
    if not msg:
        return
    # Try inline cancel/back first
    if msg.buttons:
        for row in msg.buttons:
            for btn in row:
                text = btn.text or ""
                raw = getattr(btn, "data", None)
                data = raw.decode("utf-8", errors="replace") if raw else ""
                blob = f"{text} {data}".lower()
                if any(k in blob for k in ("پرداخت انجام", "واریز کردم", "i paid", "paid", "ارسال رسید")):
                    continue
                if any(k in blob for k in ("لغو", "cancel", "بازگشت", "back", "انصراف")):
                    if raw:
                        out = await click_inline(client, entity, msg, data)
                        steps.append({"step": "cancelled_inline", "clicked": text, "message": msg_dump(out)})
                        return
                    out = await send_text(client, entity, text)
                    steps.append({"step": "cancelled_reply", "clicked": text, "message": msg_dump(out)})
                    return
    # Fallback home
    out = await send_text(client, entity, "/start")
    steps.append({"step": "cancelled_via_start", "message": msg_dump(out)})


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return

    entity = await client.get_entity(BOT)
    steps = []

    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, "/start")
    await asyncio.sleep(3.5)
    await wait_bot(client, entity, before)

    wallet = await send_text(client, entity, "🏦 کیف پول + شارژ")
    steps.append({"step": "wallet", "message": msg_dump(wallet)})
    await asyncio.sleep(2.5)

    ask = await click_inline(client, entity, wallet, "Add_Balance")
    steps.append({"step": "ask_amount", "message": msg_dump(ask)})
    await asyncio.sleep(2.5)

    # Send minimum amount as plain text
    pay_screen = await send_text(client, entity, AMOUNT_TO_TRY)
    steps.append({"step": "after_amount", "sent_amount": AMOUNT_TO_TRY, "message": msg_dump(pay_screen)})
    if pay_screen:
        steps.append({"step": "pay_extract", **extract_pay_info(pay_screen.message or "")})

    # If it offers payment method choices, click non-confirm options one by one carefully
    current = pay_screen
    for i in range(5):
        if not current or not current.buttons:
            break
        advanced = False
        for row in current.buttons:
            for btn in row:
                text = btn.text or ""
                raw = getattr(btn, "data", None)
                data = raw.decode("utf-8", errors="replace") if raw else ""
                url = getattr(btn, "url", None)
                blob = f"{text} {data}".lower()
                # Never confirm payment
                if any(k in blob for k in ("پرداخت انجام", "واریز کردم", "i paid", "paid", "ارسال رسید", "تصویر رسید")):
                    steps.append({"step": "saw_confirm_button_skipped", "button": {"text": text, "callback_data": data, "url": url}})
                    continue
                # Record URL payment methods without opening external browsers aggressively
                if url:
                    steps.append({"step": f"payment_url_option_{i}", "text": text, "url": url})
                    continue
                # Click method selectors / show card
                if raw and any(k in blob for k in ("کارت", "card", "درگاه", "gate", "روش", "method", "کارت به کارت", "cardtocard", "ctc", "pay")):
                    steps.append({"step": f"click_method_{i}", "clicked": {"text": text, "callback_data": data}})
                    current = await click_inline(client, entity, current, data)
                    steps.append({"step": f"method_result_{i}", "message": msg_dump(current)})
                    if current:
                        steps.append({"step": f"method_extract_{i}", **extract_pay_info(current.message or "")})
                    advanced = True
                    await asyncio.sleep(3.0)
                    break
            if advanced:
                break
        if not advanced:
            break

    # Capture final screen buttons for documentation, then cancel
    if current:
        steps.append({"step": "final_pre_transfer_screen", "message": msg_dump(current)})
        await cancel_out(client, entity, current, steps)

    OUT.write_text(json.dumps({"bot": BOT, "steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
