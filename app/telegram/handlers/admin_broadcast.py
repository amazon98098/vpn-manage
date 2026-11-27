"""Admin UI: broadcast a message to customer-bot users."""
from __future__ import annotations

import json
import logging

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings
from app.database import repositories as repo
from app.database.database import get_session
from app.services import broadcast as bc
from app.telegram.handlers.admin import is_admin, _ui_show
from app.telegram.ui_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()


def _draft_key(uid: int) -> str:
    return f"broadcast_draft:{uid}"


def _load_draft(uid: int) -> dict:
    with get_session() as session:
        raw = (repo.get_state(session, _draft_key(uid)) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_draft(uid: int, data: dict) -> None:
    with get_session() as session:
        repo.set_state(
            session, _draft_key(uid), json.dumps(data, ensure_ascii=False)
        )


def _clear_draft(uid: int) -> None:
    with get_session() as session:
        repo.set_state(session, _draft_key(uid), "")
        repo.set_state(session, f"admin_flow:{uid}", "")


@router.callback_query(F.data == "settings:broadcast")
async def cb_settings_broadcast(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    status = bc.get_broadcast_status()
    running = bc.is_broadcast_running()
    status_line = ""
    if running or status.get("state") == "running":
        status_line = (
            f"\n⏳ در حال ارسال: {status.get('sent', 0)}/"
            f"{status.get('total', '?')} "
            f"(خطا {status.get('failed', 0)} · رد {status.get('skipped', 0)})\n"
        )
    elif status.get("state") in {"done", "cancelled"}:
        status_line = (
            f"\nآخرین ارسال: <b>{status.get('state')}</b> — "
            f"{status.get('sent', 0)}/{status.get('total', 0)} "
            f"(خطا {status.get('failed', 0)} · رد {status.get('skipped', 0)})\n"
        )
    rows = [
        [
            InlineKeyboardButton(
                text=f"📣 {label}",
                callback_data=f"broadcast:aud:{key}",
            )
        ]
        for key, label in bc.AUDIENCE_LABELS.items()
    ]
    if running:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⏹ توقف ارسال فعلی",
                    callback_data="broadcast:cancel",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ مشتری و کانال",
                callback_data="settings:hub:customer",
            )
        ]
    )
    await _ui_show(
        callback.message,
        "📣 <b>پیام همگانی به مشتریان</b>\n\n"
        "پیام از ربات مشتری ارسال می‌شود.\n"
        "مخاطب را انتخاب کنید، متن را بفرستید، بعد تایید کنید."
        f"{status_line}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        edit=True,
    )


@router.callback_query(F.data.startswith("broadcast:aud:"))
async def cb_broadcast_audience(
    callback: CallbackQuery, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    if bc.is_broadcast_running():
        await callback.answer("یک ارسال همگانی در جریان است", show_alert=True)
        return
    audience = (callback.data or "").rsplit(":", 1)[-1]
    if audience not in bc.AUDIENCE_LABELS:
        await callback.answer("نامعتبر", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        count = len(repo.list_broadcast_recipient_ids(session, audience))
        repo.set_state(session, f"admin_flow:{uid}", f"broadcast_compose:{audience}")
    _save_draft(uid, {"audience": audience})
    label = bc.AUDIENCE_LABELS[audience]
    await callback.message.answer(
        f"📣 مخاطب: <b>{label}</b> — <b>{count}</b> نفر\n\n"
        "متن پیام همگانی را بفرستید (حداکثر ۴۰۰۰ کاراکتر).\n"
        "می‌توانید عکس با کپشن هم بفرستید.\n"
        "انصراف: <code>-</code>"
    )
    await callback.answer()


@router.callback_query(F.data == "broadcast:cancel")
async def cb_broadcast_cancel(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    if bc.request_broadcast_cancel():
        await callback.answer("درخواست توقف ثبت شد", show_alert=True)
    else:
        await callback.answer("ارسالی در جریان نیست", show_alert=True)


@router.callback_query(F.data == "broadcast:abort_draft")
async def cb_broadcast_abort_draft(
    callback: CallbackQuery, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    _clear_draft(uid)
    await safe_answer(callback)
    await cb_settings_broadcast(callback, settings)


@router.callback_query(F.data == "broadcast:confirm")
async def cb_broadcast_confirm(
    callback: CallbackQuery, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    draft = _load_draft(uid)
    audience = str(draft.get("audience") or "")
    text = str(draft.get("text") or "")
    photo = str(draft.get("photo_file_id") or "") or None
    if audience not in bc.AUDIENCE_LABELS or (not text and not photo):
        await callback.answer("پیش‌نویس نامعتبر است", show_alert=True)
        return
    if bc.is_broadcast_running():
        await callback.answer("یک ارسال همگانی در جریان است", show_alert=True)
        return
    _clear_draft(uid)
    result = await bc.start_broadcast_background(
        audience=audience,
        text=text,
        photo_file_id=photo,
        requested_by=uid,
    )
    if not result.get("ok"):
        err = result.get("error") or "fail"
        await callback.answer(f"ناموفق: {err}", show_alert=True)
        return
    await callback.answer("شروع شد", show_alert=True)
    if callback.message:
        await callback.message.answer(
            f"📣 ارسال همگانی در صف قرار گرفت.\n"
            f"مخاطب: <b>{bc.AUDIENCE_LABELS.get(audience, audience)}</b>\n"
            f"تعداد: <b>{result.get('total', 0)}</b>\n"
            "پس از اتمام، گزارش می‌آید.\n"
            "برای توقف: تنظیمات ← مشتری و کانال ← پیام همگانی ← توقف."
        )


async def handle_broadcast_admin_text(
    message: Message, uid: int, flow: str, query: str
) -> bool:
    """Return True if handled. Supports text compose; photos handled separately."""
    if not flow.startswith("broadcast_compose:"):
        return False
    audience = flow.split(":", 1)[1]
    if audience not in bc.AUDIENCE_LABELS:
        with get_session() as session:
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer("مخاطب نامعتبر — دوباره از منو شروع کنید.")
        return True
    if query.strip() in {"-", "—"}:
        _clear_draft(uid)
        await message.answer("ارسال همگانی لغو شد.")
        return True
    body = (query or "").strip()
    if not body:
        await message.answer("متن خالی است. دوباره بفرستید یا <code>-</code> برای انصراف.")
        return True
    if len(body) > 4000:
        await message.answer("متن طولانی است (حداکثر ۴۰۰۰ کاراکتر).")
        return True
    with get_session() as session:
        count = len(repo.list_broadcast_recipient_ids(session, audience))
        repo.set_state(session, f"admin_flow:{uid}", "")
    _save_draft(uid, {"audience": audience, "text": body, "photo_file_id": ""})
    label = bc.AUDIENCE_LABELS[audience]
    preview = body if len(body) <= 800 else body[:800] + "…"
    await message.answer(
        f"📣 <b>پیش‌نمایش پیام همگانی</b>\n"
        f"مخاطب: <b>{label}</b> — <b>{count}</b> نفر\n\n"
        f"{preview}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ تایید و ارسال",
                        callback_data="broadcast:confirm",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ انصراف",
                        callback_data="broadcast:abort_draft",
                    )
                ],
            ]
        ),
    )
    return True


async def handle_broadcast_admin_photo(message: Message, uid: int, flow: str) -> bool:
    if not flow.startswith("broadcast_compose:"):
        return False
    audience = flow.split(":", 1)[1]
    if audience not in bc.AUDIENCE_LABELS:
        with get_session() as session:
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer("مخاطب نامعتبر — دوباره از منو شروع کنید.")
        return True
    photo = message.photo[-1] if message.photo else None
    if not photo:
        return False
    caption = (message.caption or "").strip()
    with get_session() as session:
        count = len(repo.list_broadcast_recipient_ids(session, audience))
        repo.set_state(session, f"admin_flow:{uid}", "")
    _save_draft(
        uid,
        {
            "audience": audience,
            "text": caption,
            "photo_file_id": photo.file_id,
        },
    )
    label = bc.AUDIENCE_LABELS[audience]
    await message.answer(
        f"📣 <b>پیش‌نمایش پیام همگانی (عکس)</b>\n"
        f"مخاطب: <b>{label}</b> — <b>{count}</b> نفر\n"
        f"کپشن: {caption or '—'}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ تایید و ارسال",
                        callback_data="broadcast:confirm",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ انصراف",
                        callback_data="broadcast:abort_draft",
                    )
                ],
            ]
        ),
    )
    return True
