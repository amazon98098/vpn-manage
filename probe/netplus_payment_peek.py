"""Peek NetPlus (Bombejokerbot) top-up/payment screens. Stop before confirming any transfer."""
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
OUT = BASE / "netplus_payment_peek.json"

STOP_WORDS = (
    "پرداخت انجام",
    "واریز کردم",
    "پرداخت شدم",
    "تایید پرداخت",
    "confirm pay",
    "i paid",
    "paid",
    "ارسال رسید",
)


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


def looks_like_payment_confirm(text: str) -> bool:
    t = (text or "").lower()
    return any(w.lower() in t for w in STOP_WORDS)


async def wait_bot(client, entity, after_id: int, timeout: float = 15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    latest = None
    while asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(entity, limit=8)
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
                await asyncio.sleep(3.0)
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
    await asyncio.sleep(3.2)
    return await wait_bot(client, entity, before)


def pick_safe_next(msg):
    """Choose a next inline button that advances top-up but is not 'I paid'."""
    if not msg or not msg.buttons:
        return None
    candidates = []
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if not raw:
                continue
            text = btn.text or ""
            data = raw.decode("utf-8", errors="replace")
            blob = f"{text} {data}".lower()
            if looks_like_payment_confirm(text) or looks_like_payment_confirm(data):
                continue
            if any(x in blob for x in ("لغو", "cancel", "بازگشت", "back", "منو")):
                # keep as fallback only
                candidates.append((10, text, data))
                continue
            # Prefer amount / card / gate / continue style actions
            score = 50
            if any(k in blob for k in ("افزایش", "add_balance", "balance", "شارژ", "کارت", "card", "درگاه", "payexa", "aban", "amount", "مبلغ", "تومان", "ریال")):
                score = 1
            elif any(k in blob for k in ("ادامه", "next", "confirm", "انتخاب", "select")):
                score = 2
            candidates.append((score, text, data))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    # Do not auto-click cancel/back if better options exist
    best = candidates[0]
    if best[0] >= 10:
        return None
    return {"text": best[1], "callback_data": best[2]}


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return

    entity = await client.get_entity(BOT)
    steps = []

    # Reset to main menu
    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, "/start")
    await asyncio.sleep(3.5)
    menu = await wait_bot(client, entity, before)
    steps.append({"step": "main_menu", "message": msg_dump(menu)})

    # Open wallet via reply keyboard
    wallet = await send_text(client, entity, "🏦 کیف پول + شارژ")
    steps.append({"step": "wallet", "message": msg_dump(wallet)})
    await asyncio.sleep(2.5)

    # Add balance
    add = await click_inline(client, entity, wallet, "Add_Balance")
    steps.append({"step": "add_balance", "message": msg_dump(add)})
    await asyncio.sleep(2.5)

    # Walk a few safe steps deeper collecting card/amount screens
    current = add
    for i in range(6):
        if not current:
            break
        text = current.message or ""
        steps.append({"step": f"observe_{i}", "message": msg_dump(current)})

        # Extract likely card/amount patterns for the report summary
        cards = re.findall(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", text)
        amounts = re.findall(r"[\d۰-۹]{1,3}(?:[.,٬]?[\d۰-۹]{3})+(?:\s*(?:تومان|ریال|تومن))?", text)
        if cards or amounts:
            steps.append({"step": f"extracted_{i}", "cards": cards, "amounts": amounts})

        # Stop if screen already shows payment instructions with confirm-paid button
        dangerous = []
        safe_buttons = []
        if current.buttons:
            for row in current.buttons:
                for btn in row:
                    raw = getattr(btn, "data", None)
                    data = raw.decode("utf-8", errors="replace") if raw else None
                    info = {"text": btn.text, "callback_data": data, "url": getattr(btn, "url", None)}
                    if looks_like_payment_confirm(btn.text or "") or (data and looks_like_payment_confirm(data)):
                        dangerous.append(info)
                    else:
                        safe_buttons.append(info)
        if dangerous:
            steps.append({
                "step": "reached_pre_transfer_screen",
                "note": "Payment instructions visible; NOT clicking paid/confirm",
                "dangerous_buttons": dangerous,
                "safe_buttons": safe_buttons,
                "message": msg_dump(current),
            })
            break

        nxt = pick_safe_next(current)
        if not nxt:
            steps.append({"step": "no_safe_next", "message": msg_dump(current)})
            break

        steps.append({"step": f"click_{i}", "clicked": nxt})
        current = await click_inline(client, entity, current, nxt["callback_data"])
        await asyncio.sleep(3.0)

    # Also inspect buy duration -> package list (no purchase)
    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, "/start")
    await asyncio.sleep(3.2)
    await wait_bot(client, entity, before)

    buy = await send_text(client, entity, "🔐 خرید اشتراک")
    steps.append({"step": "buy_duration", "message": msg_dump(buy)})
    await asyncio.sleep(2.5)
    if buy:
        one_month = await click_inline(client, entity, buy, "productmonth_31")
        steps.append({"step": "buy_one_month_packages", "message": msg_dump(one_month)})
        await asyncio.sleep(2.5)
        # Back out if possible
        if one_month:
            back = await click_inline(client, entity, one_month, "backuser")
            if not back:
                # try common back callbacks from buttons
                for row in one_month.buttons or []:
                    for btn in row:
                        raw = getattr(btn, "data", None)
                        if not raw:
                            continue
                        data = raw.decode("utf-8", errors="replace")
                        if "back" in data.lower() or "بازگشت" in (btn.text or ""):
                            back = await click_inline(client, entity, one_month, data)
                            break
            steps.append({"step": "buy_backed_out", "message": msg_dump(back)})

    # Quick look at renew + search entry (no services expected)
    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, "/start")
    await asyncio.sleep(3.2)
    await wait_bot(client, entity, before)
    services = await send_text(client, entity, "🛍 مشاهده سرویس های خریداری شده من")
    steps.append({"step": "my_services", "message": msg_dump(services)})
    if services:
        search = await click_inline(client, entity, services, "searchservice")
        steps.append({"step": "quick_search_prompt", "message": msg_dump(search)})
        # Do not type a username; just capture prompt then cancel/back if available
        if search and search.buttons:
            for row in search.buttons:
                for btn in row:
                    raw = getattr(btn, "data", None)
                    if not raw:
                        continue
                    data = raw.decode("utf-8", errors="replace")
                    if "back" in data.lower() or "cancel" in data.lower() or "لغو" in (btn.text or "") or "بازگشت" in (btn.text or ""):
                        backed = await click_inline(client, entity, search, data)
                        steps.append({"step": "search_backed_out", "message": msg_dump(backed)})
                        break

    OUT.write_text(json.dumps({"bot": BOT, "steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE:{OUT}")
    print(f"STEPS:{len(steps)}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
