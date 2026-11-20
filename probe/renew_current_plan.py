"""Renew one NetPlus sub via 'تمدید پلن فعلی'. Stop at payment instructions."""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import DataInvalidError, FloodWaitError

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = str(ROOT / "data" / "sessions" / "main_account")
BOT = "@Bombejokerbot"
USERNAME = "258_keyvan_heydari_1"
OUT = BASE / "renew_current_plan_258.json"


def log(msg: str) -> None:
    print(msg, flush=True)


async def pause(a=2.8, b=4.5):
    await asyncio.sleep(random.uniform(a, b))


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
            items.append(item)
        rows.append(items)
    return rows


def dump(msg):
    if not msg:
        return None
    return {
        "id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "text": msg.message or "",
        "buttons": buttons_of(msg),
    }


def flat_buttons(msg):
    return [b for row in buttons_of(msg) for b in row]


def find_data(msg, needle: str, exact: bool = False) -> str | None:
    for b in flat_buttons(msg):
        d = b.get("callback_data") or ""
        if exact and d == needle:
            return d
        if not exact and d.startswith(needle):
            return d
    return None


def is_payment(msg) -> bool:
    if not msg or not msg.message:
        return False
    t = msg.message
    return ("شماره کارت" in t and "ریال" in t) or (
        "مبلغ" in t and "ریال" in t and ("کارت" in t or find_data(msg, "sendresidcart"))
    )


async def wait_bot(client, entity, after_id: int, timeout: float = 25.0):
    deadline = asyncio.get_event_loop().time() + timeout
    latest = None
    while asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(entity, limit=10)
        for m in msgs:
            if m and m.id > after_id and not m.out:
                return m
            if m and not m.out:
                latest = m
        await asyncio.sleep(1.1)
    return latest


async def send_text(client, entity, text: str):
    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, text)
    await pause(3.0, 4.8)
    return await wait_bot(client, entity, before)


async def click_data(client, entity, msg, data: str):
    if not msg or not msg.buttons:
        return None, None
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if raw and raw.decode("utf-8", errors="replace") == data:
                before = (await client.get_messages(entity, limit=1))[0].id
                try:
                    result = await btn.click()
                except FloodWaitError as exc:
                    log(f"FLOODWAIT:{exc.seconds}")
                    await asyncio.sleep(int(exc.seconds) + 5)
                    result = await btn.click()
                except DataInvalidError as exc:
                    return None, exc
                await pause()
                edited = await client.get_messages(entity, ids=msg.id)
                if edited and (
                    (edited.message or "") != (msg.message or "")
                    or buttons_of(edited) != buttons_of(msg)
                ):
                    return edited, result
                return await wait_bot(client, entity, before), result
    return None, None


def extract_pay(text: str) -> dict:
    amount = None
    card = None
    holder = None
    m = re.search(r"مبلغ(?:\s*دقیق)?\s*:\s*([\d,]+)\s*ریال", text or "")
    if m:
        amount = m.group(1).replace(",", "")
    m = re.search(r"شماره کارت\s*:\s*(\d{16})", text or "")
    if m:
        card = m.group(1)
    if not card:
        m = re.search(r"\b(\d{16})\b", text or "")
        card = m.group(1) if m else None
    m = re.search(r"به نام:\s*(.+)", text or "")
    if m:
        holder = m.group(1).strip()
    return {"amount_rial": amount, "card": card, "holder": holder}


async def main():
    steps = []
    client = TelegramClient(SESSION, API_ID, API_HASH)
    client.flood_sleep_threshold = 120
    log("CONNECTING")
    await client.connect()
    entity = await client.get_entity(BOT)

    msg = await send_text(client, entity, "/start")
    steps.append({"step": "1_start", "message": dump(msg)})
    log("1_start")

    msg = await send_text(client, entity, "/services")
    steps.append({"step": "2_services", "message": dump(msg)})
    log("2_services")

    # Search
    if msg and find_data(msg, "searchservice", exact=True):
        before = (await client.get_messages(entity, limit=1))[0].id
        for row in msg.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                if raw and raw.decode() == "searchservice":
                    await btn.click()
                    break
        await pause()
        msg = await wait_bot(client, entity, before)
        steps.append({"step": "3_search_prompt", "message": dump(msg)})
        log("3_search_prompt")

    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, USERNAME)
    await pause(3.5, 5.0)
    msg = await wait_bot(client, entity, before)
    steps.append({"step": "4_after_search", "message": dump(msg)})
    log("4_after_search")

    # Open product if needed
    if msg and not find_data(msg, "extend_"):
        for b in flat_buttons(msg):
            d = b.get("callback_data") or ""
            t = (b.get("text") or "").strip("✨ ").strip()
            if d.startswith("product_") and USERNAME.lower() in t.lower():
                msg, _ = await click_data(client, entity, msg, d)
                steps.append({"step": "5_status", "message": dump(msg)})
                log("5_status_from_list")
                break
    else:
        steps.append({"step": "5_status", "message": dump(msg)})
        log("5_status_direct")

    if not msg or not find_data(msg, "extend_"):
        steps.append({"step": "FAIL_NO_EXTEND", "message": dump(msg)})
        OUT.write_text(json.dumps({"steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
        log("FAIL_NO_EXTEND")
        await client.disconnect()
        return

    extend = find_data(msg, "extend_")
    msg, _ = await click_data(client, entity, msg, extend)
    steps.append({"step": "6_extend_menu", "clicked": extend, "message": dump(msg)})
    log("6_extend_menu")

    # CURRENT PLAN path
    same = find_data(msg, "exntedagei", exact=True)
    if not same:
        for b in flat_buttons(msg):
            if "تمدید پلن فعلی" in (b.get("text") or "") and b.get("callback_data"):
                same = b["callback_data"]
                break
    if not same:
        steps.append({"step": "FAIL_NO_CURRENT_PLAN", "message": dump(msg)})
        OUT.write_text(json.dumps({"steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
        log("FAIL_NO_CURRENT_PLAN")
        await client.disconnect()
        return

    msg, res = await click_data(client, entity, msg, same)
    steps.append(
        {
            "step": "7_current_plan",
            "clicked": same,
            "alert": getattr(res, "message", None) if res and not isinstance(res, Exception) else str(res),
            "message": dump(msg),
        }
    )
    log("7_current_plan")

    # Walk until payment
    for i in range(8):
        if not msg:
            break
        text = msg.message or ""
        btns = flat_buttons(msg)
        labels = [b.get("text") for b in btns]
        datas = [b.get("callback_data") or "" for b in btns]
        log(f"BRANCH:{i}:{text[:90].replace(chr(10), ' | ')}")
        log(f"BUTTONS:{labels}")

        if is_payment(msg):
            pay = extract_pay(text)
            steps.append({"step": "PAYMENT", "payment": pay, "message": dump(msg)})
            log(f"PAYMENT:{json.dumps(pay, ensure_ascii=False)}")
            break

        # Also check recent messages for payment (sometimes new msg)
        recent = [m for m in await client.get_messages(entity, limit=5) if m and not m.out]
        pay_msg = next((m for m in recent if is_payment(m)), None)
        if pay_msg:
            pay = extract_pay(pay_msg.message or "")
            steps.append({"step": "PAYMENT", "payment": pay, "message": dump(pay_msg)})
            log(f"PAYMENT:{json.dumps(pay, ensure_ascii=False)}")
            msg = pay_msg
            break

        if "فاکتور تمدید" in text:
            steps.append({"step": f"invoice_{i}", "message": dump(msg)})
            clicked = None
            for cand in ("confirmserivce", "confirmandgetservice", "cart_to_offline"):
                if cand in datas:
                    clicked = cand
                    break
            if not clicked:
                for b in btns:
                    t = b.get("text") or ""
                    d = b.get("callback_data")
                    if d and any(k in t for k in ("تایید", "پرداخت", "تمدید")) and "بازگشت" not in t:
                        clicked = d
                        break
            if clicked:
                msg, res = await click_data(client, entity, msg, clicked)
                steps.append({"step": f"invoice_click_{clicked}", "message": dump(msg), "alert": getattr(res, "message", None) if res and not isinstance(res, Exception) else str(res)})
                continue
            break

        if "روش پرداخت" in text or "cart_to_offline" in datas:
            steps.append({"step": f"pay_methods_{i}", "message": dump(msg)})
            if "cart_to_offline" in datas:
                msg, res = await click_data(client, entity, msg, "cart_to_offline")
                alert = getattr(res, "message", None) if res and not isinstance(res, Exception) else str(res)
                steps.append({"step": "card_to_card", "message": dump(msg), "alert": alert})
                log(f"CARD_ALERT:{alert}")
                await pause(3.5, 5.5)
                recent = [m for m in await client.get_messages(entity, limit=6) if m and not m.out]
                steps.append({"step": "after_card_recent", "recent": [dump(m) for m in recent[:4]]})
                for m in recent:
                    if is_payment(m):
                        msg = m
                        break
                else:
                    # keep msg; maybe edited
                    pass
                continue
            break

        if "موجودی حساب شما کافی نیست" in text:
            steps.append({"step": f"insufficient_{i}", "message": dump(msg)})
            if "cart_to_offline" in datas:
                msg, res = await click_data(client, entity, msg, "cart_to_offline")
                steps.append({"step": "insufficient_card", "message": dump(msg), "alert": getattr(res, "message", None) if res and not isinstance(res, Exception) else str(res)})
                await pause(3.5, 5.5)
                recent = [m for m in await client.get_messages(entity, limit=6) if m and not m.out]
                for m in recent:
                    if is_payment(m):
                        msg = m
                        break
                continue
            for b in btns:
                d = b.get("callback_data")
                t = b.get("text") or ""
                if d and "بازگشت" not in t and "بستن" not in t:
                    msg, _ = await click_data(client, entity, msg, d)
                    break
            continue

        if "confirmserivce" in datas:
            msg, _ = await click_data(client, entity, msg, "confirmserivce")
            steps.append({"step": f"confirmserivce_{i}", "message": dump(msg)})
            continue

        steps.append({"step": f"unhandled_{i}", "message": dump(msg)})
        log(f"UNHANDLED:{i}")
        break

    OUT.write_text(
        json.dumps(
            {
                "username": USERNAME,
                "path": "current_plan",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "steps": steps,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
