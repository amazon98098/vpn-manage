"""Deep FineArts inventory: open every listed service + subscription URL.

Fixes in-place list pagination (same message id) so product clicks use fresh buttons.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

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
POLL = 0.25
SKIP_DATA = {
    "searchservice",
    "page_info",
    "backuser",
    "backorder",
    "buy",
    "account",
    "helpbtns",
    "supportbtns",
}


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


def dump_msg(msg):
    if not msg:
        return None
    return {
        "id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "out": bool(msg.out),
        "text": msg.message or "",
        "buttons": buttons_of(msg),
    }


def redact_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r"(https?://[^/]+/)", url)
    if m:
        return m.group(1) + "…redacted…"
    return "…redacted…"


def username_from_list_text(text: str) -> str:
    t = (text or "").strip()
    t = t.strip("✨⭐️⭐ ").strip()
    return t


def parse_status(text: str) -> dict:
    t = text or ""
    out: dict = {}

    def field(*pats: str):
        for pat in pats:
            m = re.search(pat, t, re.MULTILINE)
            if m:
                return m.group(1).strip()
        return None

    out["status_raw"] = field(r"وضعیت سرویس\s*:\s*([^\n]+)")
    out["username"] = field(
        r"نام سرویس\s*:\s*(\S+)",
        r"نام کاربری(?: سرویس)?\s*:\s*(\S+)",
    )
    out["location_raw"] = field(r"موقعیت سرویس\s*:\s*([^\n]+)")
    out["product_raw"] = field(r"نام محصول\s*:\s*([^\n]+)")
    out["traffic_raw"] = field(r"ترافیک\s*:\s*([^\n]+)")
    out["used_raw"] = field(r"حجم مصرفی\s*:\s*([^\n]+)")
    out["remain_raw"] = field(r"حجم باقی مانده\s*:\s*([^\n]+)")
    out["expire_raw"] = field(r"تاریخ اتمام\s*:\s*([^\n]+)")
    out["last_connect_raw"] = field(r"اخرین زمان اتصال\s*:\s*([^\n]+)")
    # GB numbers
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
    # expire unlimited?
    exp = out.get("expire_raw") or ""
    if "نامحدود" in exp:
        out["expire_unlimited"] = True
        m = re.search(r"\((\d+)\)", exp)
        if m:
            out["expire_days_hint"] = int(m.group(1))
    return out


def parse_sub_url(url: str) -> dict:
    info = {"url_redacted": redact_url(url), "host": None, "userinfo_hint": None}
    try:
        p = urlparse(url)
        info["host"] = p.hostname
        info["scheme"] = p.scheme
        info["path"] = p.path
        # userinfo in URL path token (common for sub panels)
        if p.username:
            info["userinfo_hint"] = p.username
        # token after last slash often opaque
        token = (p.path or "").rstrip("/").split("/")[-1]
        if token:
            info["token_len"] = len(token)
            decoded = unquote(token)
            # sometimes username embedded
            if "_" in decoded and re.search(r"[a-zA-Z]", decoded):
                info["token_looks_like_username"] = False
    except Exception:
        pass
    return info


class Probe:
    def __init__(self, client, bot):
        self.client = client
        self.bot = bot
        self.entity = None

    async def ensure(self):
        if self.entity is None:
            self.entity = await self.client.get_entity(self.bot)
        return self.entity

    async def wait_bot(self, after_id: int, timeout: float = 10.0):
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

    async def send_text(self, text: str):
        entity = await self.ensure()
        while True:
            try:
                before = (await self.client.get_messages(entity, limit=1))[0].id
                await self.client.send_message(entity, text)
                return await self.wait_bot(before, timeout=10.0)
            except FloodWaitError as exc:
                await asyncio.sleep(int(exc.seconds) + 1)

    async def click_data(self, msg, data: str, timeout: float = 12.0):
        entity = await self.ensure()
        if not msg or not msg.buttons:
            return None
        # Always re-fetch message so button objects match current keyboard
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

    def product_buttons(self, msg):
        out = []
        for b in flat_buttons(msg):
            d = b.get("callback_data") or ""
            t = b.get("text") or ""
            if not d.startswith("product_"):
                continue
            if d in SKIP_DATA:
                continue
            out.append(
                {
                    "username": username_from_list_text(t),
                    "list_text": t,
                    "callback_data": d,
                    "vendor_service_id": d.removeprefix("product_"),
                }
            )
        return out

    def next_page_cb(self, msg):
        for b in flat_buttons(msg):
            t = b.get("text") or ""
            d = b.get("callback_data") or ""
            if "بعدی" in t and d.startswith("servicespage_"):
                return d
        return None


async def main() -> None:
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return

    probe = Probe(client, BOT)
    entity = await probe.ensure()
    me = await client.get_me()

    inventory: list[dict] = []
    errors: list[dict] = []
    private_urls: dict[str, str] = {}

    print("OPEN menu…")
    menu = await probe.send_text("/start")
    await asyncio.sleep(0.3)
    list_msg = await probe.click_data(menu, "backorder") if menu else None
    if not list_msg:
        print("NO_LIST")
        await client.disconnect()
        return

    total_hint = None
    m = re.search(r"کل:\s*(\d+)", list_msg.message or "")
    if m:
        total_hint = int(m.group(1))
    print(f"LIST open total_hint={total_hint}")

    # Collect all product ids across pages first (without opening)
    catalog: list[dict] = []
    cur = list_msg
    for page in range(20):
        page_items = probe.product_buttons(cur)
        print(f"PAGE {page+1} products={len(page_items)}")
        for it in page_items:
            if not any(x["callback_data"] == it["callback_data"] for x in catalog):
                catalog.append({**it, "list_page": page + 1})
        nxt = probe.next_page_cb(cur)
        if not nxt:
            break
        cur = await probe.click_data(cur, nxt)
        if not cur:
            break
        await asyncio.sleep(0.35)

    print(f"CATALOG {len(catalog)}")

    # Re-open list from start for clean page 1
    menu = await probe.send_text("/start")
    await asyncio.sleep(0.3)
    list_msg = await probe.click_data(menu, "backorder") if menu else None
    cur = list_msg
    page_idx = 1

    async def ensure_on_page(target_page: int):
        nonlocal cur, page_idx, list_msg
        if cur is None:
            menu2 = await probe.send_text("/start")
            cur = await probe.click_data(menu2, "backorder") if menu2 else None
            page_idx = 1
        if target_page == page_idx:
            # refresh live
            cur = await client.get_messages(entity, ids=cur.id)
            return cur
        if target_page < page_idx:
            # restart list
            menu2 = await probe.send_text("/start")
            await asyncio.sleep(0.25)
            cur = await probe.click_data(menu2, "backorder") if menu2 else None
            page_idx = 1
        while page_idx < target_page and cur:
            nxt = probe.next_page_cb(cur)
            if not nxt:
                break
            cur = await probe.click_data(cur, nxt)
            page_idx += 1
            await asyncio.sleep(0.3)
        if cur:
            cur = await client.get_messages(entity, ids=cur.id)
        return cur

    for i, item in enumerate(catalog):
        print(
            f"OPEN {i+1}/{len(catalog)} p{item['list_page']} {item['username']}…"
        )
        try:
            cur = await ensure_on_page(item["list_page"])
            if not cur:
                errors.append({**item, "error": "no_list_page"})
                continue
            # verify product on page
            if not any(
                b.get("callback_data") == item["callback_data"]
                for b in flat_buttons(cur)
            ):
                # search by walking from page 1
                menu2 = await probe.send_text("/start")
                cur = await probe.click_data(menu2, "backorder") if menu2 else None
                page_idx = 1
                found = False
                for _ in range(20):
                    if not cur:
                        break
                    if any(
                        b.get("callback_data") == item["callback_data"]
                        for b in flat_buttons(cur)
                    ):
                        found = True
                        break
                    nxt = probe.next_page_cb(cur)
                    if not nxt:
                        break
                    cur = await probe.click_data(cur, nxt)
                    page_idx += 1
                if not found:
                    errors.append({**item, "error": "product_not_on_list"})
                    continue

            status = await probe.click_data(cur, item["callback_data"], timeout=15.0)
            if not status or "وضعیت سرویس" not in (status.message or ""):
                errors.append(
                    {
                        **item,
                        "error": "status_open_failed",
                        "got_preview": ((status.message or "")[:120] if status else None),
                    }
                )
                # try recover list
                if status:
                    back = await probe.click_data(status, "backorder")
                    if back:
                        cur = back
                        page_idx = 1
                continue

            parsed = parse_status(status.message or "")
            row = {
                **item,
                "status_text": status.message or "",
                "parsed": parsed,
                "status_buttons": [
                    {
                        "text": b.get("text"),
                        "callback_data": b.get("callback_data"),
                        "url": b.get("url"),
                    }
                    for b in flat_buttons(status)
                ],
            }

            # subscription URL
            sub_cb = None
            for b in flat_buttons(status):
                d = b.get("callback_data") or ""
                if d.startswith("subscriptionurl_"):
                    sub_cb = d
                    break
            if sub_cb:
                link_msg = await probe.click_data(status, sub_cb, timeout=15.0)
                row["link_message"] = dump_msg(link_msg)
                urls = re.findall(
                    r"https?://\S+", (link_msg.message if link_msg else "") or ""
                )
                # also url buttons
                if link_msg:
                    for b in flat_buttons(link_msg):
                        if b.get("url"):
                            urls.append(b["url"])
                urls = [u.rstrip(").,]") for u in urls]
                if urls:
                    url = urls[0]
                    private_urls[item["username"]] = url
                    row["subscription"] = parse_sub_url(url)
                # after link screen, return to status then list
                # Prefer back to product if present, else backorder
                back_target = link_msg or status
                back_cb = None
                for b in flat_buttons(back_target):
                    d = b.get("callback_data") or ""
                    t = b.get("text") or ""
                    if d.startswith("product_") or "مشخصات" in t:
                        back_cb = d
                        break
                if back_cb:
                    status2 = await probe.click_data(back_target, back_cb)
                    status = status2 or status
                list_back = await probe.click_data(status, "backorder")
                if list_back:
                    cur = list_back
                    # backorder returns to page 1 typically
                    page_idx = 1
            else:
                list_back = await probe.click_data(status, "backorder")
                if list_back:
                    cur = list_back
                    page_idx = 1

            # export-friendly strip of huge text optional
            export_row = {
                "vendor": "finearts",
                "bot": BOT,
                "username": parsed.get("username") or item["username"],
                "vendor_service_id": item["vendor_service_id"],
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
                "subscription_host": (row.get("subscription") or {}).get("host"),
                "subscription_url_redacted": (row.get("subscription") or {}).get(
                    "url_redacted"
                ),
                "has_subscription_url": bool(row.get("subscription")),
                "list_page": item["list_page"],
                "callbacks": {
                    "product": item["callback_data"],
                    "extend": f"extend_{item['vendor_service_id']}",
                    "subscriptionurl": f"subscriptionurl_{item['vendor_service_id']}",
                    "updateproduct": f"updateproduct_{item['vendor_service_id']}",
                    "changelink": f"changelink_{item['vendor_service_id']}",
                    "transfer": f"transfer_{item['vendor_service_id']}",
                },
            }
            inventory.append(export_row)
            print(
                f"  OK user={export_row['username']} remain={export_row.get('remain_gb')} "
                f"sub={export_row['has_subscription_url']}"
            )
        except Exception as exc:
            errors.append({**item, "error": repr(exc)})
            print(f"  ERR {exc!r}")
        await asyncio.sleep(0.5)

    report = {
        "bot": BOT,
        "bot_id": entity.id,
        "account": me.username or me.first_name,
        "account_id": me.id,
        "total_hint": total_hint,
        "catalog_count": len(catalog),
        "inventory_count": len(inventory),
        "error_count": len(errors),
        "inventory": inventory,
        "errors": errors,
        "catalog": catalog,
    }
    (BASE / "finearts_inventory_export.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if private_urls:
        (BASE / "finearts_private_urls.json").write_text(
            json.dumps(private_urls, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        f"DONE inventory={len(inventory)} errors={len(errors)} "
        f"urls={len(private_urls)}"
    )
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
