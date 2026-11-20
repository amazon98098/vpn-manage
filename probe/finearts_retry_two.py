"""Retry the two FineArts services that failed list navigation."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import DataInvalidError, FloodWaitError

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(BASE / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = str(ROOT / "data" / "sessions" / "main_account")
BOT = os.getenv("FINEARTS_BOT", os.getenv("VENDOR_BOT_2", "@FineArtsVPNBot"))
TARGETS = [
    ("064_borzoyipur_7957042", "product_96fd61a9", "96fd61a9"),
    ("3395_amirali_2934687", "product_1906545e", "1906545e"),
]
POLL = 0.25


def buttons_of(msg):
    rows = []
    if not msg or not msg.buttons:
        return rows
    for row in msg.buttons:
        items = []
        for btn in row:
            item = {"text": btn.text or ""}
            if getattr(btn, "data", None) is not None:
                item["callback_data"] = btn.data.decode("utf-8", errors="replace")
            if getattr(btn, "url", None):
                item["url"] = btn.url
            items.append(item)
        rows.append(items)
    return rows


def flat_buttons(msg):
    return [b for row in buttons_of(msg) for b in row]


def parse_status(text: str) -> dict:
    t = text or ""
    out = {}

    def field(*pats):
        for pat in pats:
            m = re.search(pat, t, re.MULTILINE)
            if m:
                return m.group(1).strip()
        return None

    out["status_raw"] = field(r"وضعیت سرویس\s*:\s*([^\n]+)")
    out["username"] = field(r"نام سرویس\s*:\s*(\S+)", r"نام کاربری(?: سرویس)?\s*:\s*(\S+)")
    out["location_raw"] = field(r"موقعیت سرویس\s*:\s*([^\n]+)")
    out["product_raw"] = field(r"نام محصول\s*:\s*([^\n]+)")
    out["traffic_raw"] = field(r"ترافیک\s*:\s*([^\n]+)")
    out["used_raw"] = field(r"حجم مصرفی\s*:\s*([^\n]+)")
    out["remain_raw"] = field(r"حجم باقی مانده\s*:\s*([^\n]+)")
    out["expire_raw"] = field(r"تاریخ اتمام\s*:\s*([^\n]+)")
    for key, src in (
        ("traffic_gb", out.get("traffic_raw")),
        ("used_gb", out.get("used_raw")),
        ("remain_gb", out.get("remain_raw")),
    ):
        if not src:
            continue
        m = re.search(r"([\d.]+)\s*گیگ", src)
        if m:
            out[key] = float(m.group(1))
    exp = out.get("expire_raw") or ""
    if "نامحدود" in exp:
        out["expire_unlimited"] = True
    return out


def redact_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r"(https?://[^/]+/)", url)
    return (m.group(1) + "…redacted…") if m else "…redacted…"


class Probe:
    def __init__(self, client, bot):
        self.client = client
        self.bot = bot
        self.entity = None

    async def ensure(self):
        if self.entity is None:
            self.entity = await self.client.get_entity(self.bot)
        return self.entity

    async def wait_bot(self, after_id, timeout=10.0):
        entity = await self.ensure()
        fut = self.client.loop.create_future()

        async def on_new(event):
            msg = event.message
            if msg and msg.id > after_id and not event.out and not fut.done():
                fut.set_result(msg)

        self.client.add_event_handler(
            on_new, events.NewMessage(chats=entity, incoming=True)
        )
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if fut.done():
                    return fut.result()
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), timeout=POLL)
                except asyncio.TimeoutError:
                    msgs = await self.client.get_messages(entity, limit=6)
                    for m in msgs:
                        if m and m.id > after_id and not m.out:
                            return m
            return None
        finally:
            self.client.remove_event_handler(on_new)

    async def send_text(self, text):
        entity = await self.ensure()
        while True:
            try:
                before = (await self.client.get_messages(entity, limit=1))[0].id
                await self.client.send_message(entity, text)
                return await self.wait_bot(before)
            except FloodWaitError as exc:
                await asyncio.sleep(int(exc.seconds) + 1)

    async def click_data(self, msg, data, timeout=15.0):
        entity = await self.ensure()
        live = await self.client.get_messages(entity, ids=msg.id)
        if not live or not live.buttons:
            return None
        for row in live.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                if not raw or raw.decode("utf-8", errors="replace") != data:
                    continue
                while True:
                    try:
                        before = (await self.client.get_messages(entity, limit=1))[0].id
                        old_text = live.message or ""
                        old_btns = buttons_of(live)
                        msg_id = live.id

                        async def _click():
                            try:
                                await btn.click()
                            except Exception:
                                pass

                        task = asyncio.create_task(_click())
                        try:
                            await asyncio.wait_for(asyncio.shield(task), timeout=0.4)
                        except asyncio.TimeoutError:
                            pass
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            await asyncio.sleep(POLL)
                            edited = await self.client.get_messages(entity, ids=msg_id)
                            if edited and (
                                (edited.message or "") != old_text
                                or buttons_of(edited) != old_btns
                            ):
                                return edited
                            msgs = await self.client.get_messages(entity, limit=5)
                            for m in msgs:
                                if m and m.id > before and not m.out:
                                    return m
                        return None
                    except FloodWaitError as exc:
                        await asyncio.sleep(int(exc.seconds) + 1)
                    except DataInvalidError:
                        return None
        return None


async def open_via_search(probe, username, product_cb, sid):
    menu = await probe.send_text("/start")
    lst = await probe.click_data(menu, "backorder")
    # Prefer search
    search = await probe.click_data(lst, "searchservice") if lst else None
    if search:
        # bot likely asks for username
        ask = search
        reply = await probe.send_text(username)
        # reply might be status or list filtered
        msg = reply
        if msg and "وضعیت سرویس" in (msg.message or ""):
            return msg
        # maybe need to click product
        if msg and any(b.get("callback_data") == product_cb for b in flat_buttons(msg)):
            return await probe.click_data(msg, product_cb)
        # try clicking first product_* 
        for b in flat_buttons(msg or ask):
            d = b.get("callback_data") or ""
            if d.startswith("product_"):
                return await probe.click_data(msg or ask, d)
    # fallback: walk pages
    cur = lst
    for _ in range(10):
        if not cur:
            break
        if any(b.get("callback_data") == product_cb for b in flat_buttons(cur)):
            return await probe.click_data(cur, product_cb)
        nxt = None
        for b in flat_buttons(cur):
            if "بعدی" in (b.get("text") or "") and (b.get("callback_data") or "").startswith(
                "servicespage_"
            ):
                nxt = b["callback_data"]
                break
        if not nxt:
            break
        cur = await probe.click_data(cur, nxt)
    return None


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    probe = Probe(client, BOT)
    export = json.loads((BASE / "finearts_inventory_export.json").read_text())
    private = {}
    if (BASE / "finearts_private_urls.json").exists():
        private = json.loads((BASE / "finearts_private_urls.json").read_text())

    for username, product_cb, sid in TARGETS:
        print("RETRY", username)
        status = await open_via_search(probe, username, product_cb, sid)
        if not status or "وضعیت سرویس" not in (status.message or ""):
            print(" FAIL open", (status.message if status else None))
            # keep error
            continue
        parsed = parse_status(status.message or "")
        row = {
            "vendor": "finearts",
            "bot": BOT,
            "username": parsed.get("username") or username,
            "vendor_service_id": sid,
            "product_raw": parsed.get("product_raw"),
            "location_raw": parsed.get("location_raw"),
            "status_raw": parsed.get("status_raw"),
            "traffic_gb": parsed.get("traffic_gb"),
            "used_gb": parsed.get("used_gb"),
            "remain_gb": parsed.get("remain_gb"),
            "traffic_raw": parsed.get("traffic_raw"),
            "used_raw": parsed.get("used_raw"),
            "remain_raw": parsed.get("remain_raw"),
            "expire_raw": parsed.get("expire_raw"),
            "expire_unlimited": parsed.get("expire_unlimited"),
            "subscription_host": None,
            "subscription_url_redacted": None,
            "has_subscription_url": False,
            "list_page": None,
            "callbacks": {
                "product": product_cb,
                "extend": f"extend_{sid}",
                "subscriptionurl": f"subscriptionurl_{sid}",
                "updateproduct": f"updateproduct_{sid}",
                "changelink": f"changelink_{sid}",
                "transfer": f"transfer_{sid}",
            },
        }
        sub_cb = None
        for b in flat_buttons(status):
            d = b.get("callback_data") or ""
            if d.startswith("subscriptionurl_"):
                sub_cb = d
                break
        if sub_cb:
            link_msg = await probe.click_data(status, sub_cb)
            urls = re.findall(r"https?://\S+", (link_msg.message if link_msg else "") or "")
            if link_msg:
                for b in flat_buttons(link_msg):
                    if b.get("url"):
                        urls.append(b["url"])
            urls = [u.rstrip(").,]") for u in urls]
            if urls:
                url = urls[0]
                private[username] = url
                p = urlparse(url)
                row["subscription_host"] = p.hostname
                row["subscription_url_redacted"] = redact_url(url)
                row["has_subscription_url"] = True
        # upsert into inventory
        export["inventory"] = [
            x for x in export["inventory"] if x.get("username") != row["username"]
        ]
        export["inventory"].append(row)
        export["errors"] = [
            e for e in export["errors"] if e.get("username") != username
        ]
        export["inventory_count"] = len(export["inventory"])
        export["error_count"] = len(export["errors"])
        print(
            " OK",
            row["username"],
            row.get("remain_gb"),
            "sub",
            row["has_subscription_url"],
        )
        await asyncio.sleep(0.5)

    (BASE / "finearts_inventory_export.json").write_text(
        json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (BASE / "finearts_private_urls.json").write_text(
        json.dumps(private, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("DONE", export["inventory_count"], "errors", export["error_count"])
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
