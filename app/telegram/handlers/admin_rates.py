"""Admin UI: rate tiers (friends/colleagues), referral %, searchable pickers, invites."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Settings
from app.database import repositories as repo
from app.database.database import get_session
from app.telegram.handlers.admin import is_admin, _ui_show
from app.telegram.ui_helpers import pagination_row, safe_answer

log = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 8


def _profile_label(p) -> str:
    """Show telegram id | display name for picker + search."""
    tid = p.telegram_id
    name = (p.full_name or "").strip() or (
        f"@{p.username}" if p.username else ""
    )
    if name:
        return f"{tid} | {name}"
    return f"{tid}"


@router.callback_query(F.data == "settings:rates")
async def cb_settings_rates(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    with get_session() as session:
        pct = repo.get_referral_percent(session)
        friends = repo.list_customers_by_rate_tier(session, "friends")
        colleagues = repo.list_customers_by_rate_tier(session, "colleagues")
    await _ui_show(
        callback.message,
        "👥 <b>نرخ دوستان / همکاران و مجموعه‌گیری</b>\n\n"
        f"درصد کمیسیون معرف: <b>{pct}</b>٪\n"
        f"دوستان: <b>{len(friends)}</b> نفر · همکاران: <b>{len(colleagues)}</b> نفر\n\n"
        "لیست‌ها را مدیریت کنید، درصد کمیسیون را تنظیم کنید، "
        "یا لینک یک‌بارمصرف بسازید.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="👫 لیست دوستان",
                        callback_data="rates:list:friends:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="👔 لیست همکاران",
                        callback_data="rates:list:colleagues:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="٪ درصد مجموعه‌گیری",
                        callback_data="rates:pct",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔗 لینک دعوت دوستان",
                        callback_data="rates:invite:friends",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔗 لینک دعوت همکاران",
                        callback_data="rates:invite:colleagues",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="menu:settings")],
            ]
        ),
        edit=True,
    )


@router.callback_query(F.data == "rates:pct")
async def cb_rates_pct(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        pct = repo.get_referral_percent(session)
        repo.set_state(session, f"admin_flow:{uid}", "referral_pct_edit")
    await callback.message.answer(
        f"درصد کمیسیون مجموعه‌گیری را بفرستید (۰ تا ۱۰۰).\nفعلی: <code>{pct}</code>"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rates:invite:"))
async def cb_rates_invite(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    tier = (callback.data or "").rsplit(":", 1)[-1]
    if tier not in ("friends", "colleagues"):
        await callback.answer("نامعتبر", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        token = repo.create_rate_tier_invite(session, tier, created_by=uid)
    me = await callback.bot.get_me()
    uname = me.username or ""
    link = f"https://t.me/{uname}?start=rt_{token}" if uname else f"/start rt_{token}"
    label = repo.RATE_TIER_LABELS.get(tier, tier)
    await callback.message.answer(
        f"🔗 لینک یک‌بارمصرف نرخ «{label}»:\n"
        f"<code>{link}</code>\n\n"
        "با باز کردن لینک، کاربر به لیست مربوط اضافه می‌شود و "
        "قیمت ویژه را می‌بیند."
    )
    await callback.answer()


async def _show_tier_list(
    message: Message,
    *,
    tier: str,
    page: int = 0,
    query: str | None = None,
    edit: bool = True,
) -> None:
    with get_session() as session:
        members = repo.list_customers_by_rate_tier(session, tier)
        member_ids = {int(p.telegram_id) for p in members}
        rows, total = repo.search_customer_profiles_page(
            session, query=query, page=page, page_size=PAGE_SIZE
        )
        # When no search, prefer showing current tier members first page.
        if not (query or "").strip():
            rows = members[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
            total = len(members)
    label = repo.RATE_TIER_LABELS.get(tier, tier)
    qnote = f"\nجستجو: <code>{query}</code>" if query else ""
    lines = [
        f"👥 <b>لیست {label}</b> — صفحه {page + 1}"
        f"{qnote}\n"
        "روی هر نفر بزنید تا اضافه/حذف شود."
    ]
    kb: list[list[InlineKeyboardButton]] = []
    for p in rows:
        in_list = int(p.telegram_id) in member_ids
        mark = "✅ حذف" if in_list else "➕ افزودن"
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} · {_profile_label(p)[:40]}",
                    callback_data=f"rates:tog:{tier}:{p.telegram_id}:{page}",
                )
            ]
        )
    nav = pagination_row(
        page=page,
        total=total,
        page_size=PAGE_SIZE,
        callback_prefix=f"rates:list:{tier}",
    )
    if nav:
        kb.append(nav)
    kb.append(
        [
            InlineKeyboardButton(
                text="🔎 جستجو",
                callback_data=f"rates:search:{tier}",
            )
        ]
    )
    if query:
        kb.append(
            [
                InlineKeyboardButton(
                    text="📋 نمایش فقط اعضای لیست",
                    callback_data=f"rates:list:{tier}:0",
                )
            ]
        )
    kb.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="settings:rates")])
    await _ui_show(
        message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        edit=edit,
    )


@router.callback_query(F.data.startswith("rates:list:"))
async def cb_rates_list(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    parts = (callback.data or "").split(":")
    # rates:list:friends:0
    tier = parts[2] if len(parts) > 2 else "friends"
    try:
        page = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        page = 0
    await _show_tier_list(callback.message, tier=tier, page=max(0, page), edit=True)


@router.callback_query(F.data.startswith("rates:search:"))
async def cb_rates_search(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    tier = (callback.data or "").rsplit(":", 1)[-1]
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", f"rates_search:{tier}")
    await callback.message.answer(
        "عبارت جستجو را بفرستید (نام یا شناسه عددی — مثل <code>123 | علی</code>).\n"
        "برای انصراف: <code>-</code>"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rates:tog:"))
async def cb_rates_toggle(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    parts = (callback.data or "").split(":")
    # rates:tog:friends:12345:0
    if len(parts) < 4:
        await callback.answer("نامعتبر", show_alert=True)
        return
    tier = parts[2]
    tg_id = int(parts[3])
    try:
        page = int(parts[4]) if len(parts) > 4 else 0
    except ValueError:
        page = 0
    with get_session() as session:
        cur = repo.get_customer_rate_tier(session, tg_id)
        if cur == tier:
            repo.set_customer_rate_tier(session, tg_id, "default")
            msg = "از لیست حذف شد"
        else:
            repo.set_customer_rate_tier(session, tg_id, tier)
            msg = "به لیست اضافه شد"
    await callback.answer(msg)
    await _show_tier_list(callback.message, tier=tier, page=page, edit=True)


# ─── Vendor-direct username picker ───────────────────────────────────────────


async def _show_vd_picker(
    message: Message,
    *,
    page: int = 0,
    query: str | None = None,
    edit: bool = True,
    tab: str = "on",
) -> None:
    """Two lists: enabled for vendor-direct pay vs not yet enabled."""
    tab = "off" if tab == "off" else "on"
    with get_session() as session:
        selected = {u.lower() for u in repo.get_direct_vendor_card_usernames(session)}
        # Pull a wide page then filter by tab so both lists stay accurate.
        items, _ = repo.list_usable_usernames_with_owners_page(
            session, query=query, page=0, page_size=2000
        )
    if tab == "on":
        filtered = [it for it in items if it["username"].lower() in selected]
        # Also keep selected usernames that may not appear in usable inventory.
        with get_session() as session:
            listed = repo.get_direct_vendor_card_usernames(session)
        seen = {it["username"].lower() for it in filtered}
        for u in listed:
            if u.lower() not in seen:
                filtered.append(
                    {
                        "username": u,
                        "owner_label": "—",
                    }
                )
                seen.add(u.lower())
        title = "✅ فعال‌شده برای پرداخت مستقیم"
        empty = "هنوز نام کاربری‌ای فعال نشده."
    else:
        filtered = [it for it in items if it["username"].lower() not in selected]
        title = "➕ هنوز فعال‌نشده"
        empty = "همه نام‌ها فعال‌اند یا نتیجه‌ای نیست."
    total = len(filtered)
    start = max(0, page) * PAGE_SIZE
    page_items = filtered[start : start + PAGE_SIZE]
    qnote = f"\nجستجو: <code>{query}</code>" if query else ""
    lines = [
        f"💳 <b>پرداخت مستقیم فروشنده — انتخاب نام کاربری</b>\n"
        f"<b>{title}</b>{qnote}\n"
        "روی هر مورد بزنید تا بین دو لیست جابه‌جا شود."
    ]
    if not page_items:
        lines.append(f"\n{empty}")
    kb: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=("✅ فعال‌ها" if tab == "on" else "فعال‌ها"),
                callback_data="vduser:tab:on:0",
            ),
            InlineKeyboardButton(
                text=("➕ غیرفعال‌ها" if tab == "off" else "غیرفعال‌ها"),
                callback_data="vduser:tab:off:0",
            ),
        ]
    ]
    for it in page_items:
        uname = it["username"]
        in_list = uname.lower() in selected
        mark = "✅ حذف" if in_list else "➕ افزودن"
        owner = it.get("owner_label") or "—"
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} · {uname} ({owner})"[:64],
                    callback_data=f"vduser:tog:{uname[:40]}:{page}:{tab}",
                )
            ]
        )
    nav = pagination_row(
        page=page,
        total=total,
        page_size=PAGE_SIZE,
        callback_prefix=f"vduser:tab:{tab}",
    )
    if nav:
        kb.append(nav)
    kb.append(
        [InlineKeyboardButton(text="🔎 جستجو", callback_data=f"vduser:search:{tab}")]
    )
    if query:
        kb.append(
            [
                InlineKeyboardButton(
                    text="📋 نمایش همه",
                    callback_data=f"vduser:tab:{tab}:0",
                )
            ]
        )
    kb.append(
        [InlineKeyboardButton(text="⬅️ تایید سفارش", callback_data="settings:approval")]
    )
    await _ui_show(
        message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        edit=edit,
    )


@router.callback_query(F.data == "approval:direct:users")
async def cb_vd_users_picker(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    await _show_vd_picker(callback.message, page=0, tab="on", edit=True)


@router.callback_query(F.data.startswith("vduser:tab:"))
async def cb_vd_tab(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    parts = (callback.data or "").split(":")
    # vduser:tab:on:0
    tab = parts[2] if len(parts) > 2 else "on"
    try:
        page = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        page = 0
    await _show_vd_picker(callback.message, page=max(0, page), tab=tab, edit=True)


@router.callback_query(F.data.startswith("vduser:list:"))
async def cb_vd_list(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    await safe_answer(callback)
    try:
        page = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        page = 0
    await _show_vd_picker(callback.message, page=max(0, page), tab="on", edit=True)


@router.callback_query(F.data.startswith("vduser:search"))
async def cb_vd_search(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    uid = callback.from_user.id if callback.from_user else 0
    parts = (callback.data or "").split(":")
    tab = parts[2] if len(parts) > 2 else "on"
    with get_session() as session:
        repo.set_state(session, f"admin_flow:{uid}", f"vduser_search:{tab}")
    await callback.message.answer(
        "نام کاربری سرویس یا نام مالک را برای جستجو بفرستید.\n"
        "انصراف: <code>-</code>"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vduser:tog:"))
async def cb_vd_toggle(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    parts = (callback.data or "").split(":")
    # vduser:tog:username:page[:tab]
    if len(parts) < 3:
        await callback.answer("نامعتبر", show_alert=True)
        return
    uname = parts[2]
    try:
        page = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        page = 0
    tab = parts[4] if len(parts) > 4 else "on"
    with get_session() as session:
        now_on = repo.toggle_direct_vendor_card_username(session, uname)
    await callback.answer("اضافه شد" if now_on else "حذف شد")
    await _show_vd_picker(callback.message, page=page, tab=tab, edit=True)


async def handle_rates_admin_text(message: Message, uid: int, flow: str, query: str) -> bool:
    """Return True if this flow was handled (caller should return)."""
    if flow == "referral_pct_edit":
        raw = query.replace("%", "").strip()
        if not raw.isdigit():
            await message.answer("فقط عدد ۰ تا ۱۰۰ بفرستید.")
            return True
        with get_session() as session:
            pct = repo.set_referral_percent(session, int(raw))
            repo.set_state(session, f"admin_flow:{uid}", "")
        await message.answer(f"✅ درصد مجموعه‌گیری: <b>{pct}</b>٪")
        return True

    if flow.startswith("rates_search:"):
        tier = flow.split(":", 1)[1]
        with get_session() as session:
            repo.set_state(session, f"admin_flow:{uid}", "")
        if query.strip() in {"-", "—"}:
            await _show_tier_list(message, tier=tier, page=0, edit=False)
            return True
        await _show_tier_list(
            message, tier=tier, page=0, query=query.strip(), edit=False
        )
        return True

    if flow == "vduser_search" or flow.startswith("vduser_search:"):
        tab = flow.split(":", 1)[1] if ":" in flow else "on"
        with get_session() as session:
            repo.set_state(session, f"admin_flow:{uid}", "")
        if query.strip() in {"-", "—"}:
            await _show_vd_picker(message, page=0, tab=tab, edit=False)
            return True
        await _show_vd_picker(
            message, page=0, query=query.strip(), tab=tab, edit=False
        )
        return True

    return False
