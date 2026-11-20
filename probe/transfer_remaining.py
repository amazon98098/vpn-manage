"""Transfer remaining 4 NetPlus subs, including the confirm button step."""
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
BOT = "@Bombejokerbot"
DEST_ID = "7355569488"
TARGETS = [
    "559_zahra_zare",
    "080_dinakani",
    "700_fatemeh_ebrahimi",
    "517_mohammad_roshan",
]
OUT = BASE / "transfer_remaining_report.json"
SUCCESS = "انتقال سرویس با موفقیت انجام شد"
CONFIRM_DATA = "confrimtransfers"  # NetPlus typo


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


def find_data(msg, prefix_or_exact: str, exact: bool = False) -> str | None:
    if not msg or not msg.buttons:
        return None
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if not raw:
                continue
            data = raw.decode("utf-8", errors="replace")
            if exact and data == prefix_or_exact:
                return data
            if not exact and data.startswith(prefix_or_exact):
                return data
    return None


def is_status(msg) -> bool:
    return bool(
        msg
        and msg.message
        and "وضعیت سرویس" in msg.message
        and find_data(msg, "transfer_")
    )


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
                await asyncio.sleep(3.2)
                edited = await client.get_messages(entity, ids=msg.id)
                if edited and (
                    (edited.message or "") != (msg.message or "")
                    or buttons_of(edited) != buttons_of(msg)
                ):
                    return edited, result
                return await wait_bot(client, entity, before), result
    return None, None


async def open_status(client, entity, username: str):
    msg = await send_text(client, entity, "/services")
    if msg and msg.message and "اشتراک های خریداری شده" in msg.message:
        if msg.buttons:
            for row in msg.buttons:
                for btn in row:
                    raw = getattr(btn, "data", None)
                    if raw and raw.decode() == "searchservice":
                        before = (await client.get_messages(entity, limit=1))[0].id
                        await btn.click()
                        await asyncio.sleep(2.5)
                        msg = await wait_bot(client, entity, before)
                        break
    if msg and msg.message and "عبارت جستجو" in msg.message:
        before = (await client.get_messages(entity, limit=1))[0].id
        await client.send_message(entity, username)
        await asyncio.sleep(3.5)
        msg = await wait_bot(client, entity, before)
    if is_status(msg):
        return msg
    if msg and msg.buttons:
        for row in msg.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                if not raw:
                    continue
                data = raw.decode("utf-8", errors="replace")
                if data.startswith("product_") and username.lower() in (btn.text or "").lower():
                    status, _ = await click_data(client, entity, msg, data)
                    return status
    return msg if is_status(msg) else None


async def transfer_one(client, entity, username: str):
    info = {"username": username, "events": []}
    log(f"OPEN:{username}")
    status = await open_status(client, entity, username)
    info["events"].append({"step": "status", "message": dump(status)})
    if not status:
        info["result"] = "FAIL_OPEN"
        return info

    transfer_data = find_data(status, "transfer_")
    prompt, _ = await click_data(client, entity, status, transfer_data)
    info["events"].append({"step": "prompt", "message": dump(prompt)})
    if not prompt or "شناسه کاربری اکانت مقصد" not in (prompt.message or ""):
        info["result"] = "FAIL_PROMPT"
        return info

    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, DEST_ID)
    await asyncio.sleep(4.0)
    after = await wait_bot(client, entity, before)
    info["events"].append({"step": "after_id", "message": dump(after)})

    # Confirm step (required in current NetPlus flow)
    confirm_msg = after
    if confirm_msg and SUCCESS in (confirm_msg.message or ""):
        info["result"] = "OK"
        return info

    if not confirm_msg or CONFIRM_DATA not in json.dumps(buttons_of(confirm_msg), ensure_ascii=False):
        # maybe among recent
        for m in await client.get_messages(entity, limit=5):
            if m and not m.out and find_data(m, CONFIRM_DATA, exact=True):
                confirm_msg = m
                break

    if confirm_msg and find_data(confirm_msg, CONFIRM_DATA, exact=True):
        done, res = await click_data(client, entity, confirm_msg, CONFIRM_DATA)
        info["events"].append(
            {
                "step": "confirm",
                "message": dump(done),
                "alert": getattr(res, "message", None) if res and not isinstance(res, Exception) else str(res),
            }
        )
        recent = [m for m in await client.get_messages(entity, limit=6) if m and not m.out]
        joined = "\n".join((m.message or "") for m in recent)
        if SUCCESS in joined or (done and SUCCESS in (done.message or "")):
            info["result"] = "OK"
        else:
            info["result"] = "UNKNOWN_AFTER_CONFIRM"
            info["preview"] = joined[:800]
        return info

    info["result"] = "FAIL_NO_CONFIRM"
    info["preview"] = dump(after)
    return info


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    client.flood_sleep_threshold = 120
    log("CONNECTING")
    await client.connect()
    entity = await client.get_entity(BOT)
    await send_text(client, entity, "/start")
    await asyncio.sleep(2)

    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dest": DEST_ID,
        "already_ok": ["516_azizi"],
        "transfers": [],
    }
    for i, username in enumerate(TARGETS, 1):
        log(f"TRANSFER_START:{i}:{username}")
        result = await transfer_one(client, entity, username)
        report["transfers"].append(result)
        log(f"TRANSFER_RESULT:{i}:{result.get('result')}")
        await asyncio.sleep(4)
        await send_text(client, entity, "/start")
        await asyncio.sleep(2.5)

    ok = 1 + sum(1 for t in report["transfers"] if t.get("result") == "OK")
    report["ok_total"] = ok
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE_OK:{ok}/5")
    log(f"WROTE:{OUT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
