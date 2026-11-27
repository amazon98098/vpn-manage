"""Mass-send messages to customer-bot users (admin-triggered)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.database.database import get_session
from app.database import repositories as repo
from app.telegram import customer_bridge

log = logging.getLogger(__name__)

AUDIENCE_ALL = "all"
AUDIENCE_WITH_SERVICE = "with_service"
AUDIENCE_FRIENDS = "friends"
AUDIENCE_COLLEAGUES = "colleagues"

AUDIENCE_LABELS = {
    AUDIENCE_ALL: "همه مشتریان",
    AUDIENCE_WITH_SERVICE: "مشتریان دارای سرویس",
    AUDIENCE_FRIENDS: "دوستان",
    AUDIENCE_COLLEAGUES: "همکاران",
}

# Telegram flood-careful pacing
_SEND_DELAY_SEC = 0.05
_PROGRESS_EVERY = 25

_lock = asyncio.Lock()
_cancel = asyncio.Event()
_running = False


def is_broadcast_running() -> bool:
    return _running


def request_broadcast_cancel() -> bool:
    """Ask the active broadcast to stop. Returns False if nothing is running."""
    if not _running:
        return False
    _cancel.set()
    return True


def _set_status(payload: dict[str, Any]) -> None:
    with get_session() as session:
        repo.set_state(
            session,
            "broadcast_status_json",
            __import__("json").dumps(payload, ensure_ascii=False),
        )


def get_broadcast_status() -> dict[str, Any]:
    import json

    with get_session() as session:
        raw = (repo.get_state(session, "broadcast_status_json") or "").strip()
    if not raw:
        return {"state": "idle"}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"state": "idle"}
    except json.JSONDecodeError:
        return {"state": "idle"}


async def _notify_admin(admin_id: int | None, text: str) -> None:
    if not admin_id:
        return
    bot = customer_bridge.admin_bot
    if not bot:
        return
    try:
        await bot.send_message(int(admin_id), text)
    except Exception:
        log.exception("broadcast admin progress notify failed")


async def run_broadcast(
    *,
    audience: str,
    text: str,
    photo_file_id: str | None = None,
    requested_by: int | None = None,
) -> dict[str, Any]:
    """Send to audience via customer bot. Safe to call from bot or web loop."""
    global _running
    audience = (audience or AUDIENCE_ALL).strip().lower()
    if audience not in AUDIENCE_LABELS:
        return {"ok": False, "error": "bad_audience"}
    body = (text or "").strip()
    if not body and not photo_file_id:
        return {"ok": False, "error": "empty"}
    if len(body) > 4000:
        return {"ok": False, "error": "too_long"}

    async with _lock:
        if _running:
            return {"ok": False, "error": "busy"}
        _running = True
        _cancel.clear()

    with get_session() as session:
        recipients = repo.list_broadcast_recipient_ids(session, audience)
    total = len(recipients)
    label = AUDIENCE_LABELS.get(audience, audience)
    started = datetime.utcnow().isoformat(timespec="seconds")
    _set_status(
        {
            "state": "running",
            "audience": audience,
            "total": total,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "started_at": started,
            "requested_by": requested_by,
        }
    )
    await _notify_admin(
        requested_by,
        f"📣 ارسال همگانی شروع شد\nمخاطب: <b>{label}</b>\nتعداد: <b>{total}</b>",
    )

    bot = customer_bridge.customer_bot
    sent = failed = skipped = 0
    if not bot:
        _running = False
        _set_status(
            {
                "state": "failed",
                "error": "no_customer_bot",
                "audience": audience,
                "total": total,
                "sent": 0,
                "failed": 0,
                "finished_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
        )
        return {"ok": False, "error": "no_customer_bot", "total": total}

    try:
        for idx, tg_id in enumerate(recipients, start=1):
            if _cancel.is_set():
                break
            try:
                if photo_file_id:
                    await bot.send_photo(
                        int(tg_id), photo_file_id, caption=body or None
                    )
                else:
                    await bot.send_message(int(tg_id), body)
                sent += 1
            except Exception as exc:
                err = str(exc).lower()
                # Blocked / deactivated / chat not found → skip, not hard fail spam
                if any(
                    x in err
                    for x in (
                        "blocked",
                        "deactivated",
                        "chat not found",
                        "user is deactivated",
                        "forbidden",
                        "bot was blocked",
                    )
                ):
                    skipped += 1
                else:
                    failed += 1
                    log.warning("broadcast send failed tg=%s: %s", tg_id, exc)
            if idx % _PROGRESS_EVERY == 0 or idx == total:
                _set_status(
                    {
                        "state": "running" if not _cancel.is_set() else "cancelling",
                        "audience": audience,
                        "total": total,
                        "sent": sent,
                        "failed": failed,
                        "skipped": skipped,
                        "started_at": started,
                        "requested_by": requested_by,
                        "progress": idx,
                    }
                )
            await asyncio.sleep(_SEND_DELAY_SEC)
    finally:
        cancelled = _cancel.is_set()
        _running = False
        _cancel.clear()
        finished = datetime.utcnow().isoformat(timespec="seconds")
        _set_status(
            {
                "state": "cancelled" if cancelled else "done",
                "audience": audience,
                "total": total,
                "sent": sent,
                "failed": failed,
                "skipped": skipped,
                "started_at": started,
                "finished_at": finished,
                "requested_by": requested_by,
            }
        )
        await _notify_admin(
            requested_by,
            (
                f"{'⏹ ارسال همگانی متوقف شد' if cancelled else '✅ ارسال همگانی تمام شد'}\n"
                f"مخاطب: <b>{label}</b>\n"
                f"ارسال‌شده: <b>{sent}</b> / {total}\n"
                f"رد شده (بلاک و …): <b>{skipped}</b>\n"
                f"خطا: <b>{failed}</b>"
            ),
        )

    return {
        "ok": True,
        "cancelled": cancelled,
        "audience": audience,
        "total": total,
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
    }


async def start_broadcast_background(
    *,
    audience: str,
    text: str,
    photo_file_id: str | None = None,
    requested_by: int | None = None,
) -> dict[str, Any]:
    """Queue broadcast on the bot loop without blocking the caller for the full send."""
    if is_broadcast_running():
        return {"ok": False, "error": "busy"}
    audience = (audience or AUDIENCE_ALL).strip().lower()
    if audience not in AUDIENCE_LABELS:
        return {"ok": False, "error": "bad_audience"}
    body = (text or "").strip()
    if not body and not photo_file_id:
        return {"ok": False, "error": "empty"}

    async def _runner() -> None:
        try:
            await run_broadcast(
                audience=audience,
                text=body,
                photo_file_id=photo_file_id,
                requested_by=requested_by,
            )
        except Exception:
            log.exception("broadcast background failed")

    loop = customer_bridge.bot_loop
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if loop is not None and current is not loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(_runner(), loop)
    else:
        asyncio.create_task(_runner())
    with get_session() as session:
        count = len(repo.list_broadcast_recipient_ids(session, audience))
    return {"ok": True, "queued": True, "total": count, "audience": audience}
