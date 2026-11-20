"""Transfer remaining NetPlus subscriptions to a destination Telegram ID.

This-operation rules (per operator):
- Stay on first page only: transferred items leave the list and new ones fill in.
- Do not paginate.
- On success, click بازگشت immediately.
- No artificial delays: click as soon as NetPlus options appear.
"""
from __future__ import annotations

import asyncio
import json
import os
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
DEST_ID = "9206910824"
SUCCESS = "انتقال سرویس با موفقیت انجام شد"
CONFIRM_DATA = "confrimtransfers"
STATE = BASE / "transfer_all_state.json"
REPORT = BASE / "transfer_all_report.json"

# Only poll interval while waiting for NetPlus to answer — no extra sleeps
POLL = 0.05


def log(msg: str) -> None:
    print(msg, flush=True)


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {
        "dest": DEST_ID,
        "done": [],
        "failed": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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


def product_buttons(msg):
    out = []
    if not msg or not msg.buttons:
        return out
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if not raw:
                continue
            data = raw.decode("utf-8", errors="replace")
            if data.startswith("product_"):
                out.append(
                    {
                        "username": (btn.text or "").strip("✨ ").strip(),
                        "callback_data": data,
                        "id": data.split("_", 1)[1],
                    }
                )
    return out


def is_list(msg) -> bool:
    return bool(msg and msg.message and "اشتراک های خریداری شده" in msg.message)


def is_status(msg) -> bool:
    return bool(
        msg and msg.message and "وضعیت سرویس" in msg.message and find_data(msg, "transfer_")
    )


async def flood_sleep(exc: FloodWaitError) -> None:
    wait = int(exc.seconds) + 3
    log(f"FLOODWAIT:{wait}s")
    await asyncio.sleep(wait)


async def wait_bot(client, entity, after_id: int, timeout: float = 20.0):
    deadline = asyncio.get_event_loop().time() + timeout
    latest = None
    while asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(entity, limit=8)
        for m in msgs:
            if m and m.id > after_id and not m.out:
                return m
            if m and not m.out:
                latest = m
        await asyncio.sleep(POLL)
    return latest


async def send_text(client, entity, text: str):
    while True:
        try:
            before = (await client.get_messages(entity, limit=1))[0].id
            await client.send_message(entity, text)
            return await wait_bot(client, entity, before)
        except FloodWaitError as exc:
            await flood_sleep(exc)


async def click_data(client, entity, msg, data: str):
    if not msg or not msg.buttons:
        return None, None
    for row in msg.buttons:
        for btn in row:
            raw = getattr(btn, "data", None)
            if not raw or raw.decode("utf-8", errors="replace") != data:
                continue
            while True:
                try:
                    before = (await client.get_messages(entity, limit=1))[0].id
                    old_text = msg.message or ""
                    old_buttons = buttons_of(msg)
                    result = await btn.click()
                    # No sleep: check edit immediately, then wait for new bot message
                    for _ in range(40):
                        edited = await client.get_messages(entity, ids=msg.id)
                        if edited and (
                            (edited.message or "") != old_text
                            or buttons_of(edited) != old_buttons
                        ):
                            return edited, result
                        nxt = await client.get_messages(entity, limit=1)
                        if nxt and nxt[0] and nxt[0].id > before and not nxt[0].out:
                            return nxt[0], result
                        await asyncio.sleep(POLL)
                    return await wait_bot(client, entity, before), result
                except FloodWaitError as exc:
                    await flood_sleep(exc)
                except DataInvalidError as exc:
                    return None, exc
    return None, None


async def ensure_list(client, entity):
    latest = (await client.get_messages(entity, limit=1))[0]
    if latest and not latest.out and is_list(latest) and product_buttons(latest):
        return latest
    msg = await send_text(client, entity, "/services")
    if msg and msg.message and "عبارت جستجو" in msg.message:
        msg = await send_text(client, entity, "🛍 مشاهده سرویس های خریداری شده من")
    return msg


async def click_back_to_list(client, entity, success_msg=None):
    candidates = []
    if success_msg:
        candidates.append(success_msg)
    candidates.extend([m for m in await client.get_messages(entity, limit=8) if m and not m.out])
    for msg in candidates:
        if not msg or not msg.buttons:
            continue
        if SUCCESS not in (msg.message or "") and msg is not success_msg:
            continue
        for row in msg.buttons:
            for btn in row:
                text = btn.text or ""
                raw = getattr(btn, "data", None)
                data = raw.decode("utf-8", errors="replace") if raw else ""
                if raw and ("بازگشت" in text or data == "backorder"):
                    log(f"BACK_TO_LIST:{text}:{data}")
                    new_msg, _ = await click_data(client, entity, msg, data)
                    if new_msg and is_list(new_msg):
                        return new_msg
                    latest = (await client.get_messages(entity, limit=1))[0]
                    if latest and not latest.out and is_list(latest):
                        return latest
                    return new_msg
    log("BACK_FALLBACK_SERVICES")
    return await ensure_list(client, entity)


async def transfer_opened(client, entity, status, product: dict):
    info = {"username": product["username"], "service_id": product["id"], "result": None}
    transfer_data = find_data(status, "transfer_")
    if not transfer_data:
        info["result"] = "FAIL_NO_TRANSFER"
        return info, None

    prompt, _ = await click_data(client, entity, status, transfer_data)
    if not prompt or "شناسه کاربری اکانت مقصد" not in (prompt.message or ""):
        info["result"] = "FAIL_PROMPT"
        return info, None

    before = (await client.get_messages(entity, limit=1))[0].id
    await client.send_message(entity, DEST_ID)
    after = await wait_bot(client, entity, before)

    success_msg = None
    if after and SUCCESS in (after.message or ""):
        info["result"] = "OK"
        success_msg = after
    else:
        confirm_msg = after
        if not (confirm_msg and find_data(confirm_msg, CONFIRM_DATA, exact=True)):
            for m in await client.get_messages(entity, limit=6):
                if m and not m.out and find_data(m, CONFIRM_DATA, exact=True):
                    confirm_msg = m
                    break
        if confirm_msg and find_data(confirm_msg, CONFIRM_DATA, exact=True):
            done, _ = await click_data(client, entity, confirm_msg, CONFIRM_DATA)
            recent = [m for m in await client.get_messages(entity, limit=6) if m and not m.out]
            joined = "\n".join((m.message or "") for m in recent)
            if SUCCESS in joined or (done and SUCCESS in (done.message or "")):
                info["result"] = "OK"
                success_msg = (
                    done
                    if (done and SUCCESS in (done.message or ""))
                    else next((m for m in recent if m.message and SUCCESS in m.message), done)
                )
            else:
                info["result"] = "FAIL_NO_SUCCESS_AFTER_CONFIRM"
                return info, None
        elif after and "یافت نشد" in (after.message or ""):
            info["result"] = "FAIL_USER_NOT_FOUND"
            return info, None
        else:
            info["result"] = "FAIL_UNKNOWN"
            return info, None

    # Immediate back to list after success
    list_msg = await click_back_to_list(client, entity, success_msg)
    return info, list_msg


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    client.flood_sleep_threshold = 24 * 60 * 60
    log("CONNECTING")
    await client.connect()
    if not await client.is_user_authorized():
        log("NOT_AUTHORIZED")
        return
    me = await client.get_me()
    log(f"AUTH:{me.id}:{me.username}")
    log("DELAY_MODE:none click_asap no_pagination first_page_only")
    entity = await client.get_entity(BOT)

    state = load_state()
    if state.get("dest") != DEST_ID:
        state = {
            "dest": DEST_ID,
            "done": [],
            "failed": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    done_names = {x.get("username") for x in state.get("done", [])}
    failed_names = {x.get("username") for x in state.get("failed", [])}
    skip_names = done_names | failed_names
    log(f"RESUME_DONE:{len(done_names)} FAILED:{len(failed_names)}")

    list_msg = await ensure_list(client, entity)
    idle_rounds = 0

    while True:
        if not list_msg or not is_list(list_msg):
            list_msg = await ensure_list(client, entity)

        # Always work from whatever is on the current (first) page.
        # Transferred items disappear and others fill in — no pagination.
        products = [p for p in product_buttons(list_msg) if p["username"] not in skip_names]
        log(f"PAGE_PRODUCTS:{len(products)} visible={len(product_buttons(list_msg))}")

        if not products:
            idle_rounds += 1
            # Refresh list once in case UI lagged
            list_msg = await ensure_list(client, entity)
            products = [p for p in product_buttons(list_msg) if p["username"] not in skip_names]
            if not products:
                log("NO_MORE_ON_FIRST_PAGE")
                break
            idle_rounds = 0

        product = products[0]
        log(f"OPEN:{product['username']}")
        status, _ = await click_data(client, entity, list_msg, product["callback_data"])
        if not status or not is_status(status):
            info = {
                "username": product["username"],
                "service_id": product["id"],
                "result": "FAIL_OPEN",
            }
            state.setdefault("failed", []).append(info)
            skip_names.add(product["username"])
            save_state(state)
            log(f"RESULT:{product['username']}:FAIL_OPEN")
            list_msg = await ensure_list(client, entity)
            continue

        try:
            result, list_msg = await transfer_opened(client, entity, status, product)
        except FloodWaitError as exc:
            await flood_sleep(exc)
            list_msg = await ensure_list(client, entity)
            continue
        except Exception as exc:
            log(f"ERROR:{product['username']}:{exc}")
            result = {
                "username": product["username"],
                "service_id": product["id"],
                "result": f"ERROR:{exc}",
            }
            list_msg = await ensure_list(client, entity)

        log(f"RESULT:{product['username']}:{result.get('result')}")
        if result.get("result") == "OK":
            state.setdefault("done", []).append(result)
            skip_names.add(product["username"])
        else:
            state.setdefault("failed", []).append(result)
            skip_names.add(product["username"])
            list_msg = await ensure_list(client, entity)
        save_state(state)
        log(
            f"PROGRESS:done={len(state.get('done', []))} failed={len(state.get('failed', []))}"
        )

    REPORT.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"FINISHED:done={len(state.get('done', []))}:failed={len(state.get('failed', []))}")
    log(f"WROTE:{REPORT}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
