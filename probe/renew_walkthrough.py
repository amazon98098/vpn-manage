"""Walk NetPlus renew flow for one username. Stop at payment instructions."""
from __future__ import annotations

import asyncio
import json
import os
import random
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
OUT = BASE / "renew_walkthrough_258.json"


def log(msg: str) -> None:
    print(msg, flush=True)


async def pause(a=2.5, b=4.0):
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


def find_data(msg, needle: str, exact: bool = False) -> str | None:
    if not msg or not msg.buttons:
        return None
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if not raw:
                continue
            data = raw.decode("utf-8", errors="replace")
            if exact and data == needle:
                return data
            if not exact and data.startswith(needle):
                return data
    return None


def find_btn_text(msg, substr: str) -> str | None:
    if not msg or not msg.buttons:
        return None
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if raw and substr in (btn.text or ""):
                return raw.decode("utf-8", errors="replace")
    return None


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
    await pause(3.0, 4.5)
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
                except DataInvalidError as exc:
                    return None, exc
                except FloodWaitError as exc:
                    log(f"FLOODWAIT:{exc.seconds}")
                    await asyncio.sleep(int(exc.seconds) + 5)
                    result = await btn.click()
                await pause()
                edited = await client.get_messages(entity, ids=msg.id)
                if edited and (
                    (edited.message or "") != (msg.message or "")
                    or buttons_of(edited) != buttons_of(msg)
                ):
                    return edited, result
                return await wait_bot(client, entity, before), result
    return None, None


async def main():
    steps = []
    client = TelegramClient(SESSION, API_ID, API_HASH)
    client.flood_sleep_threshold = 120
    log("CONNECTING")
    await client.connect()
    entity = await client.get_entity(BOT)

    msg = await send_text(client, entity, "/start")
    steps.append({"step": "start", "message": dump(msg)})
    log("STEP:start")

    msg = await send_text(client, entity, "/services")
    steps.append({"step": "services", "message": dump(msg)})
    log("STEP:services")

    # Search
    if msg and find_data(msg, "searchservice", exact=True) or (
        msg and any(
            (b.get("callback_data") == "searchservice")
            for row in (dump(msg) or {}).get("buttons", [])
            for b in row
        )
    ):
        data = "searchservice"
        before = (await client.get_messages(entity, limit=1))[0].id
        for row in msg.buttons or []:
            for btn in row:
                raw = getattr(btn, "data", None)
                if raw and raw.decode() == "searchservice":
                    await btn.click()
                    break
        await pause()
        msg = await wait_bot(client, entity, before)
        steps.append({"step": "search_prompt", "message": dump(msg)})
        log("STEP:search_prompt")

    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, USERNAME)
    await pause(3.5, 5.0)
    msg = await wait_bot(client, entity, before)
    steps.append({"step": "search_result_or_status", "message": dump(msg)})
    log("STEP:after_username")

    # If list of results, click product
    if msg and msg.buttons and not find_data(msg, "extend_"):
        for row in msg.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                if not raw:
                    continue
                data = raw.decode("utf-8", errors="replace")
                text = (btn.text or "").strip("✨ ").strip()
                if data.startswith("product_") and USERNAME.lower() in text.lower():
                    msg, _ = await click_data(client, entity, msg, data)
                    steps.append({"step": "opened_status", "message": dump(msg)})
                    log("STEP:opened_status")
                    break

    if not msg or not find_data(msg, "extend_"):
        # maybe already status
        if not (msg and find_data(msg, "extend_")):
            steps.append({"step": "FAIL_NO_EXTEND", "message": dump(msg)})
            OUT.write_text(json.dumps({"username": USERNAME, "steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
            log("FAIL_NO_EXTEND")
            await client.disconnect()
            return

    steps.append({"step": "status_before_extend", "message": dump(msg)})
    extend_data = find_data(msg, "extend_")
    msg, res = await click_data(client, entity, msg, extend_data)
    steps.append(
        {
            "step": "after_extend_click",
            "clicked": extend_data,
            "alert": getattr(res, "message", None) if res and not isinstance(res, Exception) else str(res),
            "message": dump(msg),
        }
    )
    log("STEP:after_extend")

    # Possible branches: choose duration, choose package, confirm current plan, invoice, wallet insufficient -> pay methods
    for i in range(8):
        if not msg:
            break
        text = msg.message or ""
        btns = buttons_of(msg)
        flat = [b for row in btns for b in row]
        datas = [b.get("callback_data") or "" for b in flat]
        labels = [b.get("text") or "" for b in flat]
        log(f"BRANCH:{i}:text={text[:80].replace(chr(10),' ')}")
        log(f"BRANCH:{i}:buttons={labels}")

        # Payment instructions reached
        if ("شماره کارت" in text and "ریال" in text) or (
            "مبلغ" in text and "ریال" in text and find_data(msg, "sendresidcart")
        ):
            steps.append({"step": "PAYMENT_INSTRUCTIONS", "message": dump(msg)})
            log("REACHED_PAYMENT")
            break

        # Renew invoice with confirm
        if "فاکتور تمدید" in text:
            steps.append({"step": f"invoice_{i}", "message": dump(msg)})
            # Prefer confirm renew / pay buttons
            clicked = None
            for candidate in (
                "confirmserivce",
                "confirmandgetservice",
                "exntedagei",
                "cart_to_offline",
            ):
                if any(d == candidate or d.startswith(candidate) for d in datas):
                    clicked = next(d for d in datas if d == candidate or d.startswith(candidate))
                    break
            # Also match by Persian button text
            if not clicked:
                for b in flat:
                    t = b.get("text") or ""
                    d = b.get("callback_data")
                    if d and any(k in t for k in ("تایید", "تمدید", "پرداخت", "کارت")):
                        if "بازگشت" in t or "لغو" in t:
                            continue
                        clicked = d
                        break
            if clicked:
                msg, res = await click_data(client, entity, msg, clicked)
                steps.append({"step": f"invoice_click_{clicked}", "message": dump(msg), "alert": getattr(res, "message", None) if res and not isinstance(res, Exception) else None})
                continue
            break

        # Duration selection
        if "مدت زمان" in text or any(d.startswith("productextendmonths_") or d.startswith("productmonth_") for d in datas):
            steps.append({"step": f"duration_{i}", "message": dump(msg)})
            # Prefer one month
            one = next((d for d in datas if d.endswith("_31") or d.endswith("_30")), None)
            if not one:
                one = next((d for d in datas if d.startswith("productextendmonths_") or d.startswith("productmonth_")), None)
            if one:
                msg, _ = await click_data(client, entity, msg, one)
                steps.append({"step": f"duration_clicked_{one}", "message": dump(msg)})
                continue
            break

        # Package selection
        if "سرویسی که می‌خواهید" in text or any(
            d.startswith("serviceextendselect_") or d.startswith("prodcutservices_") for d in datas
        ):
            steps.append({"step": f"packages_{i}", "message": dump(msg)})
            # Prefer a 25GB-ish / cheapest first package, else first package button
            pkg = None
            for b in flat:
                d = b.get("callback_data") or ""
                t = b.get("text") or ""
                if d.startswith(("serviceextendselect_", "prodcutservices_")):
                    if "25" in t:
                        pkg = d
                        break
                    if not pkg:
                        pkg = d
            if pkg:
                msg, _ = await click_data(client, entity, msg, pkg)
                steps.append({"step": f"package_clicked_{pkg}", "message": dump(msg)})
                continue
            break

        # Extend same plan button
        if any(d == "exntedagei" for d in datas) or any("تمدید پلن فعلی" in t for t in labels):
            steps.append({"step": f"same_plan_{i}", "message": dump(msg)})
            data = "exntedagei" if "exntedagei" in datas else find_btn_text(msg, "تمدید پلن فعلی")
            msg, _ = await click_data(client, entity, msg, data)
            steps.append({"step": "same_plan_clicked", "message": dump(msg)})
            continue

        # Payment method
        if "روش پرداخت" in text or any(d == "cart_to_offline" for d in datas):
            steps.append({"step": f"pay_method_{i}", "message": dump(msg)})
            if "cart_to_offline" in datas:
                msg, res = await click_data(client, entity, msg, "cart_to_offline")
                steps.append(
                    {
                        "step": "card_to_card_clicked",
                        "message": dump(msg),
                        "alert": getattr(res, "message", None) if res and not isinstance(res, Exception) else str(res),
                    }
                )
                # alert may contain rejection; also wait for new message
                await pause(3, 5)
                recent = [m for m in await client.get_messages(entity, limit=5) if m and not m.out]
                steps.append({"step": "after_card_recent", "recent": [dump(m) for m in recent[:3]]})
                # Prefer payment instruction message
                for m in recent:
                    if m.message and ("شماره کارت" in m.message or "ریال" in m.message):
                        msg = m
                        break
                continue
            break

        # Insufficient balance / choose payment
        if "موجودی حساب شما کافی نیست" in text or "روش‌های پرداخت" in text:
            steps.append({"step": f"need_pay_{i}", "message": dump(msg)})
            if "cart_to_offline" in datas:
                msg, res = await click_data(client, entity, msg, "cart_to_offline")
                steps.append({"step": "need_pay_card", "message": dump(msg), "alert": getattr(res, "message", None) if res and not isinstance(res, Exception) else str(res)})
                await pause(3, 5)
                recent = [m for m in await client.get_messages(entity, limit=5) if m and not m.out]
                steps.append({"step": "need_pay_recent", "recent": [dump(m) for m in recent[:3]]})
                for m in recent:
                    if m.message and ("شماره کارت" in m.message or "ریال" in m.message):
                        msg = m
                        break
                continue
            # click first non-back pay-ish button
            for b in flat:
                d = b.get("callback_data")
                t = b.get("text") or ""
                if d and "بازگشت" not in t and "بستن" not in t:
                    msg, _ = await click_data(client, entity, msg, d)
                    steps.append({"step": f"need_pay_click_{d}", "message": dump(msg)})
                    break
            continue

        # Confirm service button alone
        if any(d == "confirmserivce" for d in datas):
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
