"""FineArts inventory probe: history scrape + live «سرویس‌های من» walk.

Exports raw dump + normalized service rows for import planning.
Uses main reseller session (same as NetPlus ops).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

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
OUT_DIR = BASE
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


def find_data(msg, needle: str, exact: bool = False) -> str | None:
    for b in flat_buttons(msg):
        d = b.get("callback_data") or ""
        if exact and d == needle:
            return d
        if not exact and d.startswith(needle):
            return d
    return None


def redact_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r"(https?://[^/]+/)", url)
    if m:
        return m.group(1) + "…redacted…"
    return "…redacted…"


class FineArtsProbe:
    def __init__(self, client: TelegramClient, bot: str):
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

    async def click_data(self, msg, data: str):
        entity = await self.ensure()
        if not msg or not msg.buttons:
            return None
        for row in msg.buttons:
            for btn in row:
                raw = getattr(btn, "data", None)
                if not raw or raw.decode("utf-8", errors="replace") != data:
                    continue
                while True:
                    try:
                        before = (await self.client.get_messages(entity, limit=1))[0].id
                        old_text = msg.message or ""
                        old_btns = buttons_of(msg)
                        msg_id = msg.id
                        # Don't block on callback answer (same lesson as NetPlus).
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

                        deadline = time.monotonic() + 12.0
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

    async def dump_history(self, limit: int = 400) -> list[dict]:
        entity = await self.ensure()
        msgs = await self.client.get_messages(entity, limit=limit)
        out = []
        for m in msgs:
            if not m:
                continue
            out.append(dump_msg(m))
        return out

    def list_service_buttons(self, msg) -> list[dict]:
        """Collect likely service-row buttons from a my-services screen."""
        items = []
        for b in flat_buttons(msg):
            d = b.get("callback_data") or ""
            t = (b.get("text") or "").strip()
            if not d:
                continue
            # Skip pure navigation
            if any(k in t for k in ("بازگشت", "منو", "خرید", "خانه", "قبلی", "بعدی")):
                # keep pagination separately
                if "بعدی" in t or "قبلی" in t or "next" in d.lower() or "page" in d.lower():
                    items.append(
                        {"kind": "nav", "text": t, "callback_data": d}
                    )
                continue
            if d in {"buy", "backorder", "account", "helpbtns", "supportbtns", "backuser"}:
                continue
            items.append({"kind": "service", "text": t, "callback_data": d})
        return items

    def parse_status_fields(self, text: str) -> dict:
        t = text or ""
        out: dict = {"raw_preview": t[:500]}

        def field(pat: str):
            m = re.search(pat, t)
            return m.group(1).strip() if m else None

        out["username"] = (
            field(r"نام (?:کاربری )?سرویس\s*:\s*(\S+)")
            or field(r"یوزرنیم\s*:\s*(\S+)")
            or field(r"Username\s*:\s*(\S+)")
        )
        out["product_raw"] = field(r"نام محصول\s*:\s*(.+)") or field(
            r"پلن\s*:\s*(.+)"
        )
        out["expire_raw"] = field(r"تاریخ اتمام\s*:\s*([^\n]+)") or field(
            r"انقضا\s*:\s*([^\n]+)"
        )
        out["traffic_raw"] = field(r"ترافیک\s*:\s*([^\n]+)")
        out["used_raw"] = field(r"حجم مصرفی\s*:\s*([^\n]+)")
        out["remain_raw"] = field(r"حجم باقی مانده\s*:\s*([^\n]+)")
        out["status_raw"] = field(r"وضعیت(?: سرویس)?\s*:\s*([^\n]+)")
        urls = re.findall(r"https?://\S+", t)
        if urls:
            out["urls_in_text"] = [redact_url(u.rstrip(").,]")) for u in urls]
            out["url_full_first"] = urls[0].rstrip(").,]")  # kept only in private export
        return out


async def main() -> None:
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED")
        return

    probe = FineArtsProbe(client, BOT)
    entity = await probe.ensure()
    me = await client.get_me()
    report: dict = {
        "bot": BOT,
        "bot_id": entity.id,
        "account": me.username or me.first_name,
        "account_id": me.id,
        "steps": [],
        "services_from_list": [],
        "status_samples": [],
        "link_samples": [],
        "compat_notes": [],
    }

    print("HISTORY dump…")
    history = await probe.dump_history(limit=500)
    (OUT_DIR / "finearts_history_main.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report["history_count"] = len(history)
    print(f"HISTORY {len(history)} msgs")

    # Live walk
    print("START menu…")
    menu = await probe.send_text("/start")
    report["steps"].append({"step": "start", "message": dump_msg(menu)})

    my_cb = None
    if menu:
        for b in flat_buttons(menu):
            t = b.get("text") or ""
            d = b.get("callback_data") or ""
            if "سرویس" in t and d:
                my_cb = d
                break
        if not my_cb:
            my_cb = find_data(menu, "backorder", exact=True) or find_data(
                menu, "order", exact=False
            )

    print(f"MY SERVICES callback={my_cb}")
    services_msg = await probe.click_data(menu, my_cb) if menu and my_cb else None
    report["steps"].append({"step": "my_services", "message": dump_msg(services_msg)})

    # Paginate and collect service buttons
    seen_pages = 0
    collected: dict[str, dict] = {}
    cur = services_msg
    for page in range(30):
        if not cur:
            break
        seen_pages += 1
        items = probe.list_service_buttons(cur)
        report["steps"].append(
            {
                "step": f"services_page_{page}",
                "items": items,
                "text_preview": ((cur.message or "")[:200]),
            }
        )
        next_cb = None
        for it in items:
            if it["kind"] == "nav" and "بعدی" in (it.get("text") or ""):
                next_cb = it["callback_data"]
            if it["kind"] == "service":
                key = it["callback_data"]
                if key not in collected:
                    collected[key] = it
        if not next_cb:
            # also try callback containing next/page
            for b in flat_buttons(cur):
                t = b.get("text") or ""
                d = b.get("callback_data") or ""
                if "بعدی" in t and d:
                    next_cb = d
                    break
        if not next_cb:
            break
        before_ids = {it["callback_data"] for it in items if it["kind"] == "service"}
        nxt = await probe.click_data(cur, next_cb)
        if not nxt:
            break
        cur = nxt
        after_ids = {
            it["callback_data"]
            for it in probe.list_service_buttons(cur)
            if it["kind"] == "service"
        }
        if after_ids and after_ids == before_ids:
            break
        await asyncio.sleep(0.5)

    report["services_from_list"] = list(collected.values())
    report["pages_walked"] = seen_pages
    print(f"LIST services={len(collected)} pages={seen_pages}")

    # Open up to 5 services for status + link
    samples = list(collected.values())[:5]
    for i, svc in enumerate(samples):
        print(f"STATUS {i+1}/{len(samples)} {svc.get('text')!r}…")
        # Re-open my services if needed
        if i > 0:
            menu2 = await probe.send_text("/start")
            await asyncio.sleep(0.4)
            services_msg = (
                await probe.click_data(menu2, my_cb) if menu2 and my_cb else None
            )
            # walk pages until we find this callback
            cur = services_msg
            found_msg = None
            for _ in range(30):
                if not cur:
                    break
                if find_data(cur, svc["callback_data"], exact=True):
                    found_msg = cur
                    break
                next_cb = None
                for b in flat_buttons(cur):
                    if "بعدی" in (b.get("text") or "") and b.get("callback_data"):
                        next_cb = b["callback_data"]
                        break
                if not next_cb:
                    break
                cur = await probe.click_data(cur, next_cb)
            services_msg = found_msg or services_msg

        status = await probe.click_data(services_msg, svc["callback_data"])
        parsed = probe.parse_status_fields(status.message if status else "")
        link_btns = []
        if status:
            for b in flat_buttons(status):
                t = b.get("text") or ""
                d = b.get("callback_data") or ""
                if any(k in t for k in ("لینک", "اشتراک", "config", "Config", "QR")) or (
                    "sub" in d.lower() or "link" in d.lower() or "url" in d.lower()
                ):
                    link_btns.append({"text": t, "callback_data": d, "url": b.get("url")})

        sample = {
            "list_button": svc,
            "status": dump_msg(status),
            "parsed": {k: v for k, v in parsed.items() if k != "url_full_first"},
            "link_buttons": link_btns,
        }

        # Try open subscription link if callback (not url)
        for lb in link_btns[:2]:
            if lb.get("url"):
                sample["link_direct_url"] = redact_url(lb["url"])
                continue
            if not lb.get("callback_data") or not status:
                continue
            after = await probe.click_data(status, lb["callback_data"])
            sample["link_click"] = dump_msg(after)
            if after and after.message:
                urls = re.findall(r"https?://\S+", after.message)
                if urls:
                    sample["subscription_url_redacted"] = redact_url(
                        urls[0].rstrip(").,]")
                    )
                    sample["subscription_url_host"] = re.match(
                        r"https?://([^/]+)", urls[0]
                    ).group(1)
                    # Keep full URL only in a sidecars file for import (local)
                    report.setdefault("_private_urls", {})[
                        parsed.get("username") or svc["callback_data"]
                    ] = urls[0].rstrip(").,]")
            break

        report["status_samples"].append(sample)
        await asyncio.sleep(0.8)

    # Compat notes from what we saw
    if not collected:
        report["compat_notes"].append(
            "Live my-services list empty on this account (same as Nov probe on test account, or no FineArts inventory)."
        )
    if collected and not report["status_samples"]:
        report["compat_notes"].append("Had list buttons but could not open status.")
    if any(s.get("subscription_url_redacted") for s in report["status_samples"]):
        report["compat_notes"].append("Subscription URL obtainable — URL userinfo may be possible.")
    else:
        report["compat_notes"].append(
            "No subscription URL captured in samples — may need different button or bot-only refresh."
        )

    # History-derived usernames / products
    hist_users = {}
    for m in history:
        text = m.get("text") or ""
        if "نام" in text and ("سرویس" in text or "محصول" in text):
            fields = probe.parse_status_fields(text)
            u = fields.get("username")
            if u:
                hist_users[u] = {
                    "username": u,
                    "product_raw": fields.get("product_raw"),
                    "expire_raw": fields.get("expire_raw"),
                    "from_message_id": m.get("id"),
                }
    report["services_from_history"] = list(hist_users.values())
    print(f"HISTORY users={len(hist_users)}")

    private = report.pop("_private_urls", {})
    (OUT_DIR / "finearts_inventory_probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if private:
        (OUT_DIR / "finearts_private_urls.json").write_text(
            json.dumps(private, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"WROTE {OUT_DIR / 'finearts_inventory_probe.json'}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
