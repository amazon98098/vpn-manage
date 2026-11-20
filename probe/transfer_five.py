"""Transfer NetPlus subscriptions from main account to destination Telegram ID.

Success reply observed:
✅ انتقال سرویس با موفقیت انجام شد.
No confirm button — just send destination numeric ID after transfer prompt.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import DataInvalidError

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = str(ROOT / "data" / "sessions" / "main_account")
BOT = os.getenv("NETPLUS_BOT", "@Bombejokerbot")
DEST_ID = "7355569488"
# Continue the interrupted batch: first one already OK.
TARGETS = [
    "559_zahra_zare",
    "080_dinakani",
    "700_fatemeh_ebrahimi",
    "517_mohammad_roshan",
]
OUT = BASE / "transfer_five_report.json"
SUCCESS_TEXT = "انتقال سرویس با موفقیت انجام شد"


def log(msg: str) -> None:
    print(msg, flush=True)


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


async def wait_bot(client, entity, after_id: int, timeout: float = 18.0):
    deadline = asyncio.get_event_loop().time() + timeout
    latest = None
    while asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(entity, limit=8)
        for m in msgs:
            if m and m.id > after_id and not m.out:
                return m
            if m and not m.out:
                latest = m
        await asyncio.sleep(1.0)
    return latest


async def send_text(client, entity, text: str):
    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, text)
    await asyncio.sleep(3.0)
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
                await asyncio.sleep(3.0)
                edited = await client.get_messages(entity, ids=msg.id)
                if edited and (
                    (edited.message or "") != (msg.message or "")
                    or buttons_of(edited) != buttons_of(msg)
                ):
                    return edited, result
                return await wait_bot(client, entity, before), result
    return None, None


async def get_list(client, entity):
    msg = await send_text(client, entity, "/services")
    if msg and "عبارت جستجو" in (msg.message or ""):
        msg = await send_text(client, entity, "🛍 مشاهده سرویس های خریداری شده من")
    return msg


async def find_product(client, entity, username: str):
    list_msg = await get_list(client, entity)
    # search
    if list_msg and list_msg.buttons:
        for row in list_msg.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                if raw and raw.decode() == "searchservice":
                    await btn.click()
                    await asyncio.sleep(2.5)
                    before = (await client.get_messages(entity, limit=1))[0].id
                    await client.send_message(entity, username)
                    await asyncio.sleep(3.5)
                    list_msg = await wait_bot(client, entity, before)
                    break

    if not list_msg or not list_msg.buttons:
        return None, None, list_msg

    # exact / contains match on product buttons
    candidates = []
    for row in list_msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if not raw:
                continue
            data = raw.decode("utf-8", errors="replace")
            if not data.startswith("product_"):
                continue
            text = (btn.text or "").strip("✨ ").strip()
            if text.lower() == username.lower() or username.lower() in text.lower():
                candidates.append((text, data))
    if not candidates:
        return None, None, list_msg
    text, data = candidates[0]
    status, _ = await click_data(client, entity, list_msg, data)
    return {"text": text, "callback_data": data, "id": data.split("_", 1)[1]}, status, list_msg


async def transfer_one(client, entity, username: str, dest: str):
    info = {"username": username, "events": []}
    log(f"OPEN:{username}")
    product, status, list_msg = await find_product(client, entity, username)
    info["events"].append({"step": "list_or_search", "message": dump(list_msg)})
    info["events"].append({"step": "status", "product": product, "message": dump(status)})
    if not product or not status:
        info["result"] = "FAIL_OPEN"
        return info

    transfer_data = None
    for row in status.buttons or []:
        for btn in row:
            raw = getattr(btn, "data", None)
            if raw and raw.decode("utf-8", errors="replace").startswith("transfer_"):
                transfer_data = raw.decode("utf-8", errors="replace")
                break
        if transfer_data:
            break
    if not transfer_data:
        info["result"] = "FAIL_NO_TRANSFER_BUTTON"
        return info

    prompt, click_res = await click_data(client, entity, status, transfer_data)
    info["events"].append(
        {
            "step": "prompt",
            "alert": getattr(click_res, "message", None) if click_res and not isinstance(click_res, Exception) else str(click_res),
            "message": dump(prompt),
        }
    )
    if not prompt or "شناسه کاربری اکانت مقصد" not in (prompt.message or ""):
        # maybe edited status message
        if prompt and "شناسه" not in (prompt.message or ""):
            info["result"] = "FAIL_UNEXPECTED_PROMPT"
            info["preview"] = (prompt.message or "")[:500]
            return info

    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, dest)
    await asyncio.sleep(4.0)
    after = await wait_bot(client, entity, before)
    recent = [m for m in await client.get_messages(entity, limit=6) if m and not m.out]
    info["events"].append({"step": "after_id", "message": dump(after), "recent": [dump(m) for m in recent[:3]]})

    texts = [(m.message or "") for m in recent if m]
    if after and after.message:
        texts.insert(0, after.message)
    joined = "\n".join(texts)
    if SUCCESS_TEXT in joined:
        info["result"] = "OK"
    elif "یافت نشد" in joined:
        info["result"] = "FAIL_USER_NOT_FOUND"
    else:
        info["result"] = "UNKNOWN"
        info["preview"] = joined[:1000]
    return info


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    client.flood_sleep_threshold = 120
    log("CONNECTING")
    await client.connect()
    if not await client.is_user_authorized():
        log("NOT_AUTHORIZED")
        return
    me = await client.get_me()
    log(f"AUTH:{me.id}:{me.username}")
    entity = await client.get_entity(BOT)

    await send_text(client, entity, "/start")
    await asyncio.sleep(2)

    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dest": DEST_ID,
        "already_done": [{"username": "516_azizi", "result": "OK"}],
        "targets": TARGETS,
        "transfers": [],
    }

    for i, username in enumerate(TARGETS, 1):
        log(f"TRANSFER_START:{i}:{username}")
        result = await transfer_one(client, entity, username, DEST_ID)
        report["transfers"].append(result)
        log(f"TRANSFER_RESULT:{i}:{result.get('result')}")
        await asyncio.sleep(4)
        await send_text(client, entity, "/start")
        await asyncio.sleep(2.5)

    ok = 1 + sum(1 for t in report["transfers"] if t.get("result") == "OK")
    report["ok_total_including_first"] = ok
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE_OK:{ok}/5")
    log(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
