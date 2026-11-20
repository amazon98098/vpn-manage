"""Shared Telegram UI helpers (safe callback answers, LTR labels, pagination)."""
from __future__ import annotations

import logging
from typing import Iterable

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton

log = logging.getLogger(__name__)

# Left-to-right mark so Latin usernames align correctly in RTL Persian UI.
_LTR = "\u200e"

SUB_STATUS_FA: dict[str, str] = {
    "active": "✅ فعال",
    "warning": "⚠️ هشدار",
    "expired": "🔚 پایان زمان سرویس",
    "volume_exhausted": "🚫 پایان حجم",
    "disabled": "🚫 غیرفعال",
    "removed_by_vendor": "🗑 حذف‌شده",
    "unknown": "❔ نامشخص",
    "deleted": "🗑 حذف‌شده",
}


def ltr(text: str | None) -> str:
    """Force left-to-right rendering for service names / Latin identifiers."""
    value = (text or "").strip()
    if not value:
        return "—"
    if value.startswith(_LTR):
        return value
    return f"{_LTR}{value}"


def ltr_html(text: str | None, *, tag: str = "code") -> str:
    value = ltr(text)
    if value == "—":
        return "—"
    return f"<{tag}>{value}</{tag}>"


def status_fa(status: str | None) -> str:
    key = (status or "").strip().lower()
    if not key:
        return "❔ نامشخص"
    return SUB_STATUS_FA.get(key, f"▫️ {status}")


async def safe_answer(
    callback: CallbackQuery | None,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    """Answer a callback; ignore Telegram's stale/invalid query errors."""
    if callback is None:
        return
    try:
        if text is None:
            await callback.answer()
        else:
            await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        msg = str(exc).lower()
        if (
            "query is too old" in msg
            or "query id is invalid" in msg
            or "response timeout expired" in msg
        ):
            log.debug("Ignoring stale callback answer: %s", exc)
            return
        raise
    except Exception:
        log.debug("callback.answer failed", exc_info=True)


def total_pages(total: int, page_size: int) -> int:
    if total <= 0:
        return 1
    return max(1, (total + page_size - 1) // page_size)


def clamp_page(page: int, total: int, page_size: int) -> int:
    return max(0, min(int(page), total_pages(total, page_size) - 1))


def pagination_row(
    *,
    page: int,
    total: int,
    page_size: int,
    callback_prefix: str,
) -> list[InlineKeyboardButton]:
    """Prev/Next buttons labeled with current/total page numbers."""
    pages = total_pages(total, page_size)
    page = clamp_page(page, total, page_size)
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(
            InlineKeyboardButton(
                text=f"◀️ قبلی {page}/{pages}",
                callback_data=f"{callback_prefix}:{page - 1}",
            )
        )
    if page + 1 < pages:
        row.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{pages} بعدی ▶️",
                callback_data=f"{callback_prefix}:{page + 1}",
            )
        )
    elif pages > 1 and page > 0:
        # Last page: keep a non-nav page indicator so the ratio stays visible.
        row.append(
            InlineKeyboardButton(
                text=f"📄 {page + 1}/{pages}",
                callback_data=f"{callback_prefix}:{page}",
            )
        )
    return row


def slice_page(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    """Return (chunk, clamped_page, total)."""
    total = len(items)
    page = clamp_page(page, total, page_size)
    start = page * page_size
    return items[start : start + page_size], page, total
