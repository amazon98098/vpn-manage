"""Customer shop bot — panel purchase flow, wallet, services, support."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.config import Settings
from app.database.database import get_session
from app.database import repositories as repo
from app.services.aban import AbanError, aban_enabled, create_invoice
from app.services.customer_fulfill import enqueue_fulfillment
from app.services.variz import VarizError, create_payment, variz_enabled
from app.telegram import customer_bridge
from app.telegram.ui_helpers import (
    ltr,
    pagination_row,
    safe_answer,
    status_fa,
    total_pages,
)
from app.services.subscription_refresh import refresh_subscription_userinfo
from app.utils.subscription_qr import subscription_qr_png
from app.utils.text import (
    format_expire_display,
    format_jalali_date,
    format_mb,
)
from app.vendors.registry import (
    DEFAULT_VENDOR_KEY,
    get_vendor_capabilities,
    get_vendor_driver,
)

router = Router(name="customer")
log = logging.getLogger(__name__)

BTN_START = "🏠 شروع"  # legacy; not shown on reply keyboard (Menu covers /start)
BTN_BUY = "🛒 خرید اشتراک"
BTN_MINE = "🛍 سرویس های من"
BTN_ORDERS = "📋 سفارش‌ها"  # kept for deep-links; removed from main menu
BTN_SUPPORT_SEND = "🎟 ارسال پیام به پشتیبانی"
BTN_MY_TICKETS = "🎫 تیکت‌های من"
SUPPORT_SUBJECT_MAX = 30
BTN_WALLET = "💰 کیف پول"
BTN_PRICES = "💵 تعرفه‌ها"
BTN_GUIDE = "📚 آموزش"
BTN_SUPPORT = "💬 پشتیبانی"
PAGE_SIZE = 10

# Buy/renew categories: monthly economy vs time-unlimited volume
PANELS = (
    {
        "key": "economy",
        "title": "✨ پلن اقتصادی",
        "blurb": "مدت‌دار ماهانه",
        "duration_mode": "timed",  # duration_days > 0
    },
    {
        "key": "volume",
        "title": "💎 پلن حجمی مدت نامحدود",
        "blurb": "حجمی با زمان نامحدود",
        "duration_mode": "unlimited",  # duration_days == 0
    },
)
_DURATION_LABELS = {
    0: "💎 نامحدود زمانی",
    31: "📅 ۱ ماه",
    30: "📅 ۱ ماه",
    61: "📅 ۲ ماه",
    62: "📅 ۲ ماه",
    91: "📅 ۳ ماه",
    92: "📅 ۳ ماه",
}
ICON_ECONOMY = "✨"
ICON_VOLUME = "💎"
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

STATUS_FA = {
    "awaiting_payment": "⏳ منتظر پرداخت",
    "receipt_window": "📤 منتظر رسید",
    "awaiting_review": "🔎 در حال بررسی",
    "approved": "✅ تایید شده",
    "rejected": "❌ رد شده",
    "fulfilling": "⚙️ در حال فعال‌سازی",
    "done": "✅ انجام شده",
    "cancelled": "🚫 لغو شده",
    "expired": "⏰ منقضی",
}
LEDGER_FA = {
    "charge": "➕ شارژ",
    "spend": "➖ برداشت",
    "adjust": "↕️ تعدیل",
}

USERNAME_HELP = (
    "یک نام کاربری دلخواه ارسال کنید\n"
    "⚠️ نام کاربری باید بدون کاراکترهای اضافه مانند @ ، فاصله ، خط تیره باشد.\n"
    "⚠️ نام کاربری باید انگلیسی باشد.\n"
    "✅ نام‌های صحیح: <code>ali12</code> | <code>mahdi</code> | <code>ws1_ksdf</code>\n"
    "❌ نادرست: <code>ali_</code> | <code>tele@</code> | <code>_mahdi</code> | فارسی\n"
    "📏 بین ۳ تا ۳۲ کاراکتر؛ فقط حروف، اعداد و خط زیر."
)


def customer_menu() -> ReplyKeyboardMarkup:
    from app.telegram.menu_config import build_reply_menu

    return build_reply_menu("customer")


def _cust_labels(action: str) -> set[str]:
    from app.telegram.menu_config import labels_for

    return labels_for("customer", action)


def support_reply_menu() -> ReplyKeyboardMarkup:
    """NetPlus-style support submenu (separate keyboard «window»)."""
    from app.telegram.menu_config import build_reply_menu

    return build_reply_menu("customer_support")


def _support_labels(action: str) -> set[str]:
    from app.telegram.menu_config import labels_for

    return labels_for("customer_support", action)


def customer_home_inline() -> InlineKeyboardMarkup:
    """Inline hub restored when navigating Back from sub-menus."""
    from app.telegram.menu_config import build_customer_inline_menu

    return build_customer_inline_menu("home")


async def _show_customer_home(message: Message, *, edit: bool = False) -> None:
    await _ui_show(
        message,
        "🏠 <b>منوی اصلی</b>\nیک گزینه را انتخاب کنید:",
        reply_markup=customer_home_inline(),
        edit=edit,
    )


async def _ui_show(
    message: Message,
    text: str,
    *,
    reply_markup=None,
    edit: bool = False,
) -> None:
    """Edit inline menus in place; fall back to a new message after Clear History."""
    if edit and message is not None:
        try:
            await message.edit_text(text, reply_markup=reply_markup)
            return
        except Exception as exc:
            err = str(exc).lower()
            if "message is not modified" in err:
                return
            log.debug("edit_text failed; sending new message: %s", exc)
    await message.answer(text, reply_markup=reply_markup)


def _telegram_identity_label(uid: int, user=None) -> str:
    """@username, else display name, else numeric id."""
    if user is not None:
        if getattr(user, "username", None):
            return f"@{user.username}"
        name = (
            ((user.first_name or "") + " " + (user.last_name or "")).strip()
            if user
            else ""
        )
        if name:
            return name
    with get_session() as session:
        profile = repo.get_customer_profile(session, uid)
        if profile:
            if profile.username:
                return f"@{profile.username}"
            if profile.full_name:
                return profile.full_name
    return str(uid)


def _price_for_uid(session, plan, uid: int) -> int:
    return repo.sell_price_for_customer(session, plan, uid)


def _parse_amount_toman(text: str) -> int | None:
    raw = (text or "").translate(_DIGIT_MAP)
    raw = (
        raw.replace(",", "")
        .replace("،", "")
        .replace("تومان", "")
        .replace("تومن", "")
        .strip()
    )
    if raw.isdigit():
        return int(raw)
    return None


def _status_fa(status: str) -> str:
    return STATUS_FA.get(status, status)


def _ledger_fa(kind: str) -> str:
    return LEDGER_FA.get(kind, kind)


def _deadline_text(expires_at: datetime | None) -> str:
    if not expires_at:
        return "—"
    return expires_at.strftime("%Y-%m-%d %H:%M")


def _back_kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    body = list(rows)
    body.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cust:back")])
    return InlineKeyboardMarkup(inline_keyboard=body)


def _track(message: Message) -> None:
    u = message.from_user
    if not u:
        return
    with get_session() as session:
        repo.upsert_customer_profile(
            session,
            u.id,
            username=u.username,
            full_name=((u.first_name or "") + " " + (u.last_name or "")).strip() or None,
        )


def _get_flow(uid: int) -> str:
    with get_session() as session:
        return repo.get_state(session, f"cust_flow:{uid}")


def _set_flow(uid: int, value: str) -> None:
    with get_session() as session:
        repo.set_state(session, f"cust_flow:{uid}", value)


def _get_draft(uid: int) -> dict:
    raw = _get_flow(uid)
    if raw.startswith("draft:"):
        try:
            return json.loads(raw[6:])
        except json.JSONDecodeError:
            return {}
    return {}


def _set_draft(uid: int, data: dict) -> None:
    _set_flow(uid, "draft:" + json.dumps(data, ensure_ascii=False))


def _card(settings: Settings) -> tuple[str, str]:
    with get_session() as session:
        return repo.get_payment_card(
            session, settings.reseller_card_number, settings.reseller_card_holder
        )


def _can_take_payment(settings: Settings, *, username: str | None = None) -> bool:
    with get_session() as session:
        if username and repo.is_direct_vendor_card_for_username(session, username):
            return True
    provider = (settings.payment_provider or "").lower()
    if provider == "aban" and aban_enabled(settings):
        return True
    if provider == "variz" and variz_enabled(settings):
        return True
    card, _ = _card(settings)
    return bool(card)


def _valid_username(name: str) -> str | None:
    u = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", u):
        return None
    if u.startswith("_") or u.endswith("_"):
        return None
    return u


def _scrub_vendor_branding(text: str) -> str:
    """Never show the upstream vendor name to customers."""
    t = text or ""
    for bad in ("نت پلاس", "نت‌پلاس", "NetPlus", "netplus", "Net Plus"):
        t = t.replace(bad, "سرویس")
    return t


def _payment_kb(
    order_id: int,
    amount_rial: str,
    card: str,
    *,
    checkout_url: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if checkout_url:
        rows.append([InlineKeyboardButton(text="🌐 صفحه پرداخت", url=checkout_url)])
    copy_row: list[InlineKeyboardButton] = []
    if card:
        copy_row.append(
            InlineKeyboardButton(
                text="💳 کپی کارت",
                copy_text=CopyTextButton(text=card),
            )
        )
    copy_row.append(
        InlineKeyboardButton(
            text="💵 کپی مبلغ",
            copy_text=CopyTextButton(text=amount_rial),
        )
    )
    rows.append(copy_row)
    rows.append(
        [
            InlineKeyboardButton(
                text="📤 ارسال رسید",
                callback_data=f"cust:receipt:{order_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🚫 لغو سفارش",
                callback_data=f"cust:cancel:{order_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _msg(key: str, **fmt) -> str:
    with get_session() as session:
        return repo.get_bot_message(session, key, **fmt)


def _c2c_instructions(
    *,
    amount_rial: str,
    card: str,
    holder: str,
    checkout_url: str | None = None,
    expires_at: datetime | None = None,
    order_id: int | None = None,
    vendor_direct: bool = False,
) -> str:
    _ = expires_at, order_id
    lines = [
        "⚠️ مبلغ را دقیق و تا رقم آخر واریز کنید؛ رند نکنید.",
        "",
        "🛑 فقط کارت‌به‌کارت کنید؛ از «پل» واریز نکنید.",
        "",
        f"💰 مبلغ: <code>{amount_rial}</code> ریال",
        "",
        f"💳 شماره کارت: <code>{card}</code>",
        "",
        f"👤 به نام: {holder or '—'}",
        "",
        "👆 برای کپی، روی مبلغ یا شماره کارت بزنید.",
        "",
    ]
    if vendor_direct:
        lines.extend(
            [
                "📤 بعد از واریز، حتماً <b>رسید پرداخت</b> را با دکمه زیر ارسال کنید.",
                "",
                "⏳ اگر تا چند دقیقه پرداخت خودکار تایید نشد، رسید شما برای پیگیری ارسال می‌شود.",
            ]
        )
    else:
        lines.extend(
            [
                "✅ با واریز مبلغ دقیق، پرداخت شما لحظه‌ای و خودکار تأیید می‌شود؛ نیازی به ارسال رسید نیست.",
                "",
                "🔴 اگر مبلغ را رند واریز کردید، تأیید دستی زمان می‌برد. نیازی به واریز مابه‌التفاوت نیست؛ لطفاً منتظر تأیید بمانید.",
            ]
        )
    if checkout_url and not vendor_direct:
        lines.extend(["", "می‌توانید از دکمه «صفحه پرداخت» هم استفاده کنید."])
    return "\n".join(lines)


async def _attach_gateway_if_possible(settings: Settings, order_id: int) -> None:
    provider = (settings.payment_provider or "").lower()
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            return
        if order.gateway_invoice_id or order.variz_payment_id:
            return
        payable = repo.order_payable_toman(order)
        if payable <= 0:
            return
        kind = order.kind
        uname = order.service_username
        tg = order.telegram_id
        description = repo.format_invoice_description(
            session, order_id=order_id, kind=kind, username=uname
        )
        amount_base = payable * 10

    if provider == "aban" and aban_enabled(settings):
        try:
            inv = await create_invoice(
                settings,
                amount_rial=amount_base,
                order_id=f"ORD-{order_id}",
                description=description,
                metadata={
                    "telegram_id": str(tg),
                    "order_id": str(order_id),
                    "kind": kind,
                },
            )
        except AbanError as exc:
            log.warning("Aban create failed for order %s: %s", order_id, exc)
            return
        with get_session() as session:
            order = repo.get_customer_order(session, order_id)
            if not order:
                return
            order.gateway_invoice_id = inv.invoice_id
            order.checkout_url = inv.payment_url or None
            order.amount_rial = str(inv.payable_rial)
            order.pay_card_number = inv.card_number
            order.pay_card_holder = inv.card_holder
            order.note = (order.note or "") + "|aban_created"
            repo.touch_customer_order(order)
        return

    if provider == "variz" and variz_enabled(settings):
        try:
            pay = await create_payment(
                settings,
                amount_rial=amount_base,
                order_id=f"ORD-{order_id}",
                description=description,
                payer_name=str(tg),
                metadata={
                    "telegram_id": str(tg),
                    "order_id": str(order_id),
                    "kind": kind,
                },
            )
        except VarizError as exc:
            log.warning("Variz create failed for order %s: %s", order_id, exc)
            return
        with get_session() as session:
            order = repo.get_customer_order(session, order_id)
            if not order:
                return
            order.variz_payment_id = pay.id
            order.checkout_url = pay.checkout_url or None
            if pay.amount_final:
                order.amount_rial = pay.amount_final
            order.note = (order.note or "") + "|variz_created"
            repo.touch_customer_order(order)


def _track_order_ui_message(order_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    key = f"cust_ui_msgs:{order_id}"
    with get_session() as session:
        raw = (repo.get_state(session, key) or "").strip()
        ids: list[int] = []
        if raw:
            try:
                ids = [int(x) for x in json.loads(raw)]
            except (json.JSONDecodeError, TypeError, ValueError):
                ids = []
        if message_id not in ids:
            ids.append(int(message_id))
        repo.set_state(session, key, json.dumps(ids[-30:]))


def _pop_order_ui_message_ids(order_id: int) -> list[int]:
    key = f"cust_ui_msgs:{order_id}"
    with get_session() as session:
        raw = (repo.get_state(session, key) or "").strip()
        repo.set_state(session, key, "")
    if not raw:
        return []
    try:
        return [int(x) for x in json.loads(raw)]
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


async def cleanup_customer_order_ui(telegram_id: int, order_id: int) -> None:
    """Delete prior invoice/C2C/pay confirm messages when fulfillment finishes."""
    bot = customer_bridge.customer_bot
    if not bot:
        return
    for mid in _pop_order_ui_message_ids(order_id):
        try:
            await bot.delete_message(telegram_id, mid)
        except Exception:
            log.debug("delete customer ui msg failed tg=%s mid=%s", telegram_id, mid)


def wallet_credit_customer_text(amount_toman: int) -> str:
    return (
        f"💎 کاربر عزیز مبلغ {int(amount_toman)} تومان "
        "به موجودی کیف پول تان اضافه گردید."
    )


async def notify_wallet_credited(
    telegram_id: int, amount_toman: int, *, order_id: int | None = None
) -> None:
    """Remove deposit/pay UI for the order, then show wallet credit success."""
    if order_id is not None:
        await cleanup_customer_order_ui(telegram_id, order_id)
    bot = customer_bridge.customer_bot
    if not bot:
        return
    try:
        await bot.send_message(
            telegram_id, wallet_credit_customer_text(amount_toman)
        )
    except Exception:
        log.exception(
            "wallet credit notify failed tg=%s amount=%s",
            telegram_id,
            amount_toman,
        )


async def _send_pay_for_order(message: Message, settings: Settings, order_id: int) -> None:
    vendor_direct = False
    with get_session() as session:
        order0 = repo.get_customer_order(session, order_id)
        if not order0:
            await message.answer("سفارش یافت نشد.")
            return
        uname0 = order0.service_username
        vendor_direct = (
            order0.kind in ("create", "renew")
            and repo.is_direct_vendor_card_for_username(session, uname0)
        )

    if vendor_direct:
        wait = await message.answer(
            "⏳ در حال آماده‌سازی اطلاعات کارت برای پرداخت…\nلطفاً چند لحظه صبر کنید."
        )
        _track_order_ui_message(order_id, wait.message_id if wait else None)
        from app.services.vendor_direct_pay import prepare_vendor_direct_payment

        prep = await prepare_vendor_direct_payment(order_id)
        if not prep.get("ok"):
            await message.answer(
                "آماده‌سازی پرداخت مستقیم فروشنده ناموفق بود.\n"
                f"جزئیات: {prep.get('error') or '—'}\n"
                "لطفاً کمی بعد دوباره تلاش کنید یا به پشتیبانی پیام دهید.",
                reply_markup=customer_menu(),
            )
            return
    else:
        await _attach_gateway_if_possible(settings, order_id)

    local_card, local_holder = _card(settings)
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order:
            await message.answer("سفارش یافت نشد.")
            return
        if not order.instructions_at:
            order.instructions_at = datetime.utcnow()
            repo.touch_customer_order(order)
        checkout = None if vendor_direct else order.checkout_url
        card = order.pay_card_number or (None if vendor_direct else local_card)
        holder = order.pay_card_holder or (None if vendor_direct else local_holder)
        amount = order.amount_rial or ""
        applied = int(order.wallet_applied_toman or 0)
        expires_at = order.expires_at
        is_vd = repo.order_is_vendor_direct(order) or vendor_direct
        kb = _payment_kb(order.id, amount, card or "", checkout_url=checkout)
        text = _c2c_instructions(
            amount_rial=amount,
            card=card or "—",
            holder=holder or "—",
            checkout_url=checkout,
            expires_at=expires_at,
            order_id=order.id,
            vendor_direct=is_vd,
        )
        oid = order.id
        _ = applied
    if not card and not checkout:
        await message.answer(
            "پرداخت کارت‌به‌کارت هنوز پیکربندی نشده. به پشتیبانی پیام دهید.",
            reply_markup=customer_menu(),
        )
        return
    sent = await message.answer(text, reply_markup=kb)
    _track_order_ui_message(oid, sent.message_id if sent else None)
    _set_flow(message.from_user.id if message.from_user else 0, f"wait_receipt:{oid}")


# ─── Main menu ───────────────────────────────────────────────────────────────


@router.message(CommandStart())
@router.message(F.text.in_({BTN_START, "شروع"}))
async def cmd_start(message: Message) -> None:
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    _set_flow(uid, "")
    # Deep-link: /start rt_TOKEN (one-time friends/colleagues rate) or /start ref_TGID
    payload = ""
    if message.text and message.text.startswith("/start"):
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()
    invite_note = ""
    if payload and uid:
        with get_session() as session:
            user = message.from_user
            repo.upsert_customer_profile(
                session,
                uid,
                username=getattr(user, "username", None) if user else None,
                full_name=(
                    ((user.first_name or "") + " " + (user.last_name or "")).strip()
                    if user
                    else None
                ),
            )
            if payload.startswith("rt_"):
                tier = repo.consume_rate_tier_invite(session, payload[3:], uid)
                if tier:
                    label = repo.RATE_TIER_LABELS.get(tier, tier)
                    invite_note = (
                        f"\n\n✅ نرخ ویژه «{label}» برای شما فعال شد."
                    )
                else:
                    invite_note = "\n\n⚠️ لینک دعوت نامعتبر یا قبلاً استفاده شده."
            elif payload.startswith("ref_"):
                digits = "".join(ch for ch in payload[4:] if ch.isdigit())
                if digits:
                    ok = repo.set_referred_by_if_empty(session, uid, int(digits))
                    if ok:
                        invite_note = (
                            "\n\n✅ زیرمجموعه ثبت شد؛ "
                            "با خرید/تمدید، کمیسیون به معرف واریز می‌شود."
                        )
    await message.answer(
        _msg("welcome") + invite_note, reply_markup=customer_menu()
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    _track(message)
    # Refresh persistent reply keyboard, then show inline hub on the next message.
    await message.answer("⌨️ منوی پایین به‌روز شد.", reply_markup=customer_menu())
    await _show_customer_home(message, edit=False)


@router.message(F.text.func(lambda t: bool(t) and t in _cust_labels("hide_menu")))
async def hide_main_menu(message: Message) -> None:
    """NetPlus-style: hide the bottom reply keyboard until /menu or /start."""
    _track(message)
    await message.answer(
        "منوی پایین مخفی شد.\n"
        "برای نمایش دوباره از /menu یا /start استفاده کنید.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.callback_query(F.data == "cust:channel:check")
async def cust_channel_check(callback: CallbackQuery) -> None:
    from app.telegram.channel_gate import (
        get_required_channel,
        join_channel_keyboard,
        user_is_channel_member,
    )

    uid = callback.from_user.id if callback.from_user else 0
    channel = get_required_channel()
    bot = callback.bot
    ok = await user_is_channel_member(bot, uid, channel) if bot else False
    if not ok:
        await callback.answer("هنوز عضو کانال نیستید", show_alert=True)
        await callback.message.answer(
            "لطفاً اول عضو کانال شوید، بعد دوباره امتحان کنید.",
            reply_markup=join_channel_keyboard(channel),
        )
        return
    await callback.answer("عضویت تایید شد ✅")
    await callback.message.answer(
        "✅ عضویت تایید شد. از منوی پایین استفاده کنید.",
        reply_markup=customer_menu(),
    )
    await _show_customer_home(callback.message, edit=False)


@router.message(Command("help"))
@router.message(F.text.func(lambda t: bool(t) and t in _cust_labels("guide")))
async def guide_start(message: Message) -> None:
    _track(message)
    await _show_education_menu(message)


async def _show_education_lesson(
    message: Message,
    lesson_id: int,
    *,
    edit: bool = False,
    back_callback: str | None = None,
) -> bool:
    """Render one education lesson; return False if missing/disabled."""
    with get_session() as session:
        lesson = repo.get_education_lesson(session, lesson_id)
        if not lesson or not lesson.enabled:
            return False
        title = lesson.title
        body = lesson.body
        cat_id = lesson.category_id
        media_type = (getattr(lesson, "media_type", None) or "").strip().lower()
        media_file_id = (getattr(lesson, "media_file_id", None) or "").strip()
        media_caption = getattr(lesson, "media_caption", None)
    back_cb = back_callback or f"cust:edu:cat:{cat_id}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb)],
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="cust:home")],
        ]
    )
    text = f"📚 <b>{title}</b>\n\n{body}"
    if media_file_id and media_type in {"video", "photo", "animation", "document"}:
        # Prefer a fresh message for media (edit cannot always swap media types).
        caption = (media_caption or "").strip() or title
        if len(caption) > 900:
            caption = caption[:900] + "…"
        try:
            if media_type == "video":
                await message.answer_video(
                    media_file_id, caption=caption, reply_markup=kb
                )
            elif media_type == "photo":
                await message.answer_photo(
                    media_file_id, caption=caption, reply_markup=kb
                )
            elif media_type == "animation":
                await message.answer_animation(
                    media_file_id, caption=caption, reply_markup=kb
                )
            else:
                await message.answer_document(
                    media_file_id, caption=caption, reply_markup=kb
                )
            if body.strip():
                await message.answer(text, reply_markup=kb)
            return True
        except Exception:
            log.exception("education media send failed lesson=%s", lesson_id)
    await _ui_show(message, text, reply_markup=kb, edit=edit)
    return True


async def _show_education_menu(
    message: Message, *, parent_id: int | None = None, edit: bool = False
) -> None:
    with get_session() as session:
        cats = repo.list_education_categories(
            session, parent_id=parent_id, enabled_only=True
        )
        lessons = (
            repo.list_education_lessons(session, parent_id, enabled_only=True)
            if parent_id is not None
            else []
        )
        empty = _msg("guide_empty")
        back_cb = "cust:edu:root"
        if parent_id is not None:
            cat = repo.get_education_category(session, parent_id)
            if cat and cat.parent_id:
                back_cb = f"cust:edu:cat:{cat.parent_id}"
        # Android / iPhone: one guide → open it immediately with the app title.
        if parent_id is not None and not cats and len(lessons) == 1:
            lesson_id = lessons[0].id
            # Avoid looping: back must leave this single-lesson category.
            auto_back = back_cb
        else:
            lesson_id = None
            auto_back = None
    if lesson_id is not None:
        ok = await _show_education_lesson(
            message, lesson_id, edit=edit, back_callback=auto_back
        )
        if ok:
            return
    rows: list[list[InlineKeyboardButton]] = []
    for c in cats:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📁 {c.title}",
                    callback_data=f"cust:edu:cat:{c.id}",
                )
            ]
        )
    for lesson in lessons:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {lesson.title}",
                    callback_data=f"cust:edu:lesson:{lesson.id}",
                )
            ]
        )
    if not rows:
        await _ui_show(message, empty, reply_markup=customer_menu(), edit=edit)
        return
    if parent_id is not None:
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb)])
    else:
        rows.append([InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="cust:home")])
    await _ui_show(
        message,
        "📚 <b>آموزش</b>\nیک بخش را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
    )


@router.callback_query(F.data == "cust:edu:root")
async def edu_root(callback: CallbackQuery) -> None:
    await _show_education_menu(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("cust:edu:cat:"))
async def edu_category(callback: CallbackQuery) -> None:
    cat_id = int((callback.data or "").rsplit(":", 1)[1])
    await _show_education_menu(callback.message, parent_id=cat_id, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("cust:edu:lesson:"))
async def edu_lesson(callback: CallbackQuery) -> None:
    lesson_id = int((callback.data or "").rsplit(":", 1)[1])
    ok = await _show_education_lesson(callback.message, lesson_id, edit=True)
    if not ok:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()


# ─── Pricing (admin-editable text) ────────────────────────────────────────────


@router.message(F.text.func(lambda t: bool(t) and t in _cust_labels("prices")))
@router.message(Command("prices"))
async def show_prices(message: Message) -> None:
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    with get_session() as session:
        text = repo.get_pricing_text(session)
        plans = repo.list_price_plans(session, enabled_only=True)
        tier = repo.get_customer_rate_tier(session, uid) if uid else "default"
        # If admin left default short text, append live plan table
        if text.strip() == repo.DEFAULT_PRICING_TEXT.strip() and plans:
            lines = [text, ""]
            tier_fa = repo.RATE_TIER_LABELS.get(tier, tier)
            if tier != "default":
                lines.append(f"🏷 نرخ فعال شما: <b>{tier_fa}</b>\n")
            for idx, p in enumerate(plans):
                if idx:
                    lines.append("────────────")
                gb = f"{int(p.traffic_gb)} گیگ" if p.traffic_gb else "حجم توافقی"
                icon = ICON_VOLUME if int(p.duration_days or 0) == 0 else ICON_ECONOMY
                dur = (
                    "نامحدود زمانی"
                    if int(p.duration_days or 0) == 0
                    else f"{p.duration_days} روز"
                )
                price = _price_for_uid(session, p, uid)
                lines.append(
                    f"{icon} <b>{p.title}</b>\n"
                    f"  {gb} · {dur}\n"
                    f"  💵 <code>{price:,}</code> تومان"
                )
            text = "\n".join(lines)
    await message.answer(text, reply_markup=customer_menu())


# ─── Buy flow ────────────────────────────────────────────────────────────────


@router.message(Command("buy"))
@router.message(F.text.func(lambda t: bool(t) and t in _cust_labels("buy")))
async def buy_start(message: Message) -> None:
    _track(message)
    await _buy_start_ui(message, edit=False)


async def _buy_start_ui(message: Message, *, edit: bool = False) -> None:
    uid = message.from_user.id if message.from_user else 0
    with get_session() as session:
        opens = repo.list_customer_orders_for_user(
            session,
            uid,
            statuses=("awaiting_payment", "receipt_window", "awaiting_review"),
            limit=3,
        )
    warn = ""
    if opens and not edit:
        warn = (
            f"⚠️ شما {len(opens)} سفارش باز دارید. "
            "پرداخت/رسید همان سفارش‌های باز را از پیام‌های قبلی پیگیری کنید.\n"
            "ادامه خرید سفارش جدید می‌سازد.\n\n"
        )
    _set_draft(uid, {"step": "panel"})
    rows = [
        [
            InlineKeyboardButton(
                text=p["title"],
                callback_data=f"cust:buy:panel:{p['key']}",
            )
        ]
        for p in PANELS
    ]
    await _ui_show(
        message,
        warn + "🛒 <b>خرید اشتراک</b>\nپلن را انتخاب کنید:",
        reply_markup=_back_kb(*rows),
        edit=edit,
    )


@router.callback_query(F.data == "cust:back")
async def cust_back(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    flow = _get_flow(uid)
    if flow.startswith("ticket:"):
        await _ui_show(
            callback.message,
            "💬 خروج از گفتگوی پشتیبانی؟",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ بله، خروج",
                            callback_data="cust:ticket:leave",
                        ),
                        InlineKeyboardButton(
                            text="💬 ادامه گفتگو",
                            callback_data="cust:ticket:stay",
                        ),
                    ]
                ]
            ),
            edit=True,
        )
        return
    draft = _get_draft(uid)
    step = draft.get("step")
    if step in (None, "panel"):
        _set_flow(uid, "")
        await _show_customer_home(callback.message, edit=True)
        return
    if step == "duration":
        draft["step"] = "panel"
        _set_draft(uid, draft)
        await _buy_start_ui(callback.message, edit=True)
        return
    if step == "volume":
        panel = draft.get("panel") or "economy"
        mode = next(
            (p.get("duration_mode") for p in PANELS if p["key"] == panel), "timed"
        )
        if mode == "unlimited":
            draft["step"] = "panel"
            _set_draft(uid, draft)
            await _buy_start_ui(callback.message, edit=True)
        else:
            draft["step"] = "duration"
            _set_draft(uid, draft)
            await _show_durations(callback.message, panel, edit=True)
        return
    if step == "username":
        draft["step"] = "volume"
        _set_draft(uid, draft)
        await _show_volumes(callback.message, draft, edit=True)
        return
    if step == "invoice":
        if draft.get("kind") == "renew":
            username = str(draft.get("username") or "")
            if draft.get("plan_mode") == "change" and draft.get("duration_days"):
                draft["step"] = "renew_volume"
                _set_draft(uid, draft)
                await _show_renew_volumes(callback.message, draft, edit=True)
            elif username:
                draft["step"] = "renew_choice"
                _set_draft(uid, draft)
                await _show_renew_choices(callback.message, username, edit=True)
            else:
                await _render_services_page(callback.message, uid=uid, page=0, edit=True)
            return
        draft["step"] = "username"
        _set_draft(uid, draft)
        await _ui_show(
            callback.message, USERNAME_HELP, reply_markup=_back_kb(), edit=True
        )
        return
    if step == "renew_volume":
        username = str(draft.get("username") or "")
        if username:
            draft["step"] = "renew_choice"
            _set_draft(uid, draft)
            await _show_renew_choices(callback.message, username, edit=True)
        else:
            await _render_services_page(callback.message, uid=uid, page=0, edit=True)
        return
    if step == "renew_choice":
        username = str(draft.get("username") or "")
        if username:
            sub = _load_service_for_display(username)
            await _ui_show(
                callback.message,
                _format_service_info(sub, username),
                reply_markup=_service_actions_kb(username),
                edit=True,
            )
        else:
            await _render_services_page(callback.message, uid=uid, page=0, edit=True)
        return
    if step == "wallet_amount":
        _set_flow(uid, "")
        await _wallet_home_ui(callback.message, uid=uid, edit=True)
        return
    if step in ("service", "svc_search"):
        await _render_services_page(callback.message, uid=uid, page=0, edit=True)
        return
    _set_flow(uid, "")
    await _show_customer_home(callback.message, edit=True)


def _plans_for_panel(plans: list, panel_key: str) -> list:
    """Filter sell plans by catalog panel (economy timed vs volume unlimited)."""
    mode = "timed"
    for p in PANELS:
        if p["key"] == panel_key:
            mode = p.get("duration_mode") or "timed"
            break
    out = []
    for plan in plans:
        days = int(plan.duration_days) if plan.duration_days is not None else 31
        if mode == "unlimited":
            if days == 0:
                out.append(plan)
        elif days > 0:
            out.append(plan)
    return out


async def _show_durations(
    message: Message, panel_key: str, *, edit: bool = False
) -> None:
    with get_session() as session:
        plans = _plans_for_panel(
            repo.list_price_plans(session, enabled_only=True), panel_key
        )
    days_list = sorted(
        {int(p.duration_days) for p in plans if p.duration_days is not None}
    )
    if not days_list:
        await _ui_show(
            message,
            "پلنی برای فروش فعال نیست.",
            reply_markup=customer_menu(),
            edit=edit,
        )
        return
    rows = [
        [
            InlineKeyboardButton(
                text=_DURATION_LABELS.get(days, f"{days} روز"),
                callback_data=f"cust:buy:dur:{days}",
            )
        ]
        for days in days_list
    ]
    await _ui_show(
        message,
        f"⏱ مدت اشتراک را انتخاب کنید:\nپلن: <b>{_panel_title(panel_key)}</b>",
        reply_markup=_back_kb(*rows),
        edit=edit,
    )


def _panel_title(key: str) -> str:
    for p in PANELS:
        if p["key"] == key:
            return p["title"]
    return key


@router.callback_query(F.data.startswith("cust:buy:panel:"))
async def buy_panel(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    panel = (callback.data or "").rsplit(":", 1)[1]
    mode = next(
        (p.get("duration_mode") for p in PANELS if p["key"] == panel), "timed"
    )
    if mode == "unlimited":
        draft = {"step": "volume", "panel": panel, "duration_days": 0}
        _set_draft(uid, draft)
        await _show_volumes(callback.message, draft, edit=True)
    else:
        draft = {"step": "duration", "panel": panel}
        _set_draft(uid, draft)
        await _show_durations(callback.message, panel, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("cust:buy:dur:"))
async def buy_duration(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    days = int((callback.data or "").rsplit(":", 1)[1])
    draft = _get_draft(uid)
    draft.update({"step": "volume", "duration_days": days})
    _set_draft(uid, draft)
    await _show_volumes(callback.message, draft, edit=True)
    await callback.answer()


async def _show_volumes(message: Message, draft: dict, *, edit: bool = False) -> None:
    raw_days = draft.get("duration_days")
    days = int(raw_days) if raw_days is not None else 31
    panel = str(draft.get("panel") or "economy")
    uid = message.from_user.id if message.from_user else 0
    rows: list[list[InlineKeyboardButton]] = []
    icon = ICON_VOLUME if days == 0 else ICON_ECONOMY
    with get_session() as session:
        plans = [
            p
            for p in _plans_for_panel(
                repo.list_price_plans(session, enabled_only=True), panel
            )
            if int(p.duration_days) == days
        ]
        for plan in plans:
            gb = int(plan.traffic_gb) if plan.traffic_gb else 0
            price = _price_for_uid(session, plan, uid)
            label = (
                f"{icon} {gb} گیگ — {price:,} ت"
                if gb
                else f"{icon} {plan.title}"
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"cust:buy:vol:{plan.id}",
                    )
                ]
            )
    if not rows:
        await _ui_show(
            message,
            "پلنی برای این مدت تعریف نشده.",
            reply_markup=customer_menu(),
            edit=edit,
        )
        return
    await _ui_show(
        message,
        "🔋 حجم اشتراک را انتخاب کنید:",
        reply_markup=_back_kb(*rows),
        edit=edit,
    )


@router.callback_query(F.data.startswith("cust:buy:vol:"))
async def buy_volume(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    plan_id = int((callback.data or "").rsplit(":", 1)[1])
    draft = _get_draft(uid)
    draft.update({"step": "username", "plan_id": plan_id})
    _set_draft(uid, draft)
    await _ui_show(
        callback.message, USERNAME_HELP, reply_markup=_back_kb(), edit=True
    )
    await callback.answer()


async def _show_invoice(message: Message, uid: int, draft: dict, *, edit: bool = False) -> None:
    plan_id = int(draft["plan_id"])
    username = draft["username"]
    kind = draft.get("kind") or "create"
    with get_session() as session:
        plan = repo.get_price_plan(session, plan_id)
        bal = repo.get_wallet_toman(session, uid)
        price = _price_for_uid(session, plan, uid) if plan else 0
        if kind == "create":
            ok, reason = repo.is_username_available_for_create(session, username, uid)
            if not ok:
                await _ui_show(
                    message,
                    "❌ این نام کاربری در دسترس نیست. نام دیگری انتخاب کنید.",
                    reply_markup=_back_kb(),
                    edit=edit,
                )
                draft["step"] = "username"
                _set_draft(uid, draft)
                return
    if not plan:
        await _ui_show(
            message, "پلن نامعتبر.", reply_markup=customer_menu(), edit=edit
        )
        return
    draft["step"] = "invoice"
    _set_draft(uid, draft)
    gb = f"{int(plan.traffic_gb)} گیگ" if plan.traffic_gb else "—"
    title = "🧾 فاکتور تمدید" if kind == "renew" else "🧾 فاکتور خرید"
    mode_line = ""
    if kind == "renew":
        if draft.get("plan_mode") == "current":
            mode_line = "مسیر: <b>تمدید پلن فعلی</b>\n"
        else:
            mode_line = "مسیر: <b>تغییر پلن / مدت</b>\n"
    plan_icon = ICON_VOLUME if int(plan.duration_days or 0) == 0 else ICON_ECONOMY
    dur_label = (
        "نامحدود زمانی"
        if int(plan.duration_days or 0) == 0
        else f"{plan.duration_days} روز"
    )
    text = (
        f"📜 <b>{title}</b>\n\n"
        f"{mode_line}"
        f"{plan_icon} نام اشتراک: <b>{plan.title}</b>\n"
        f"👤 نام کاربری: <b>{username}</b>\n"
        f"⏱ مدت: <b>{dur_label}</b>\n"
        f"🔋 حجم: <b>{gb}</b>\n"
        f"💸 مبلغ: <b>{price:,}</b> تومان\n"
        f"💰 موجودی کیف پول: <b>{bal:,}</b> تومان\n\n"
        "روش پرداخت را انتخاب کنید:"
    )
    prefix = "cust:renew:pay" if kind == "renew" else "cust:buy:pay"
    rows: list[list[InlineKeyboardButton]] = []
    if bal >= price:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💰 پرداخت کامل از کیف پول",
                    callback_data=f"{prefix}:full",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="💳 فقط کارت‌به‌کارت",
                    callback_data=f"{prefix}:card",
                )
            ]
        )
    elif bal > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"💰💳 کیف پول ({bal:,}) + کارت",
                    callback_data=f"{prefix}:mix",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="💳 فقط کارت‌به‌کارت",
                    callback_data=f"{prefix}:card",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💳 پرداخت کارت‌به‌کارت",
                    callback_data=f"{prefix}:card",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cust:back")])
    await _ui_show(
        message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
    )


async def _execute_shop_pay(
    callback: CallbackQuery,
    settings: Settings,
    *,
    kind: str,
    mode: str,
) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    flow = _get_flow(uid)
    if flow.startswith("paying:"):
        await callback.answer("در حال پردازش… لطفاً صبر کنید.", show_alert=True)
        return
    draft = _get_draft(uid)
    if not draft.get("plan_id") or not draft.get("username"):
        await callback.answer("فاکتور منقضی شده؛ دوباره شروع کنید.", show_alert=True)
        return
    if draft.get("step") != "invoice":
        await callback.answer("فاکتور منقضی شده؛ دوباره شروع کنید.", show_alert=True)
        return
    plan_id = int(draft["plan_id"])
    username = str(draft["username"])
    _set_flow(uid, f"paying:{kind}")
    try:
        with get_session() as session:
            plan = repo.get_price_plan(session, plan_id)
            if not plan:
                await callback.answer("پلن نامعتبر", show_alert=True)
                return
            if kind == "create":
                ok, _reason = repo.is_username_available_for_create(
                    session, username, uid
                )
                if not ok:
                    await callback.answer("نام کاربری در دسترس نیست", show_alert=True)
                    return
            bal = repo.get_wallet_toman(session, uid)
            price = _price_for_uid(session, plan, uid)
            if mode == "full":
                apply = price
            elif mode == "mix":
                apply = min(bal, price)
            else:
                apply = 0
            if apply > bal:
                await callback.answer("موجودی کیف پول کافی نیست", show_alert=True)
                return
            remain_preview = price - apply
            if remain_preview > 0 and not _can_take_payment(
                settings, username=username
            ):
                card, _ = _card(settings)
                if not card:
                    await callback.answer(
                        "پرداخت کارت‌به‌کارت آماده نیست؛ اول کیف پول را شارژ کنید.",
                        show_alert=True,
                    )
                    return
            renew_mode = None
            if kind == "renew":
                renew_mode = (
                    "change"
                    if str(draft.get("plan_mode") or "").lower() == "change"
                    else "current"
                )
            order = repo.create_customer_order(
                session,
                telegram_id=uid,
                kind=kind,
                plan_id=plan.id,
                service_username=username,
                price_toman=price,
                plan_mode=renew_mode,
            )
            if apply > 0:
                try:
                    repo.debit_wallet(
                        session,
                        uid,
                        apply,
                        kind="spend",
                        order_id=order.id,
                        note=f"{kind}_apply",
                    )
                except ValueError:
                    order.status = "cancelled"
                    order.note = (order.note or "") + "|debit_failed"
                    repo.touch_customer_order(order)
                    await callback.answer("موجودی کافی نیست", show_alert=True)
                    return
                order.wallet_applied_toman = apply
            repo.set_order_payable_amount(order, session)
            remain = repo.order_payable_toman(order)
            oid = order.id
            if remain <= 0:
                order.status = "awaiting_review"
                order.paid_at = datetime.utcnow()
                order.pay_source = "wallet"
                order.amount_rial = "0"
                repo.touch_customer_order(order)
                full_wallet = True
            else:
                order.status = "awaiting_payment"
                order.instructions_at = datetime.utcnow()
                repo.touch_customer_order(order)
                full_wallet = False
        _set_flow(uid, "")
        await callback.answer()
        if callback.message:
            _track_order_ui_message(oid, callback.message.message_id)
        if full_wallet:
            sent = await callback.message.answer(
                f"✅ مبلغ کامل از کیف پول پرداخت شد ({price:,} تومان).\n"
                "در حال آماده‌سازی سرویس…",
                reply_markup=customer_menu(),
            )
            _track_order_ui_message(oid, sent.message_id if sent else None)
            await enqueue_fulfillment(oid, requested_by=uid)
            return
        if apply > 0:
            sent = await callback.message.answer(
                f"💸 <code>{apply:,}</code> تومان از کیف پول کسر شد.\n"
                f"باقی‌مانده: <code>{remain:,}</code> تومان — کارت‌به‌کارت کنید.",
                reply_markup=customer_menu(),
            )
            _track_order_ui_message(oid, sent.message_id if sent else None)
        await _send_pay_for_order(callback.message, settings, oid)
    finally:
        if _get_flow(uid).startswith("paying:"):
            _set_flow(uid, "")


@router.callback_query(F.data.startswith("cust:buy:pay"))
async def buy_pay(callback: CallbackQuery, settings: Settings) -> None:
    data = callback.data or "cust:buy:pay:mix"
    mode = data.rsplit(":", 1)[-1]
    if mode not in ("full", "mix", "card"):
        mode = "mix"
    await _execute_shop_pay(callback, settings, kind="create", mode=mode)


# ─── My services ─────────────────────────────────────────────────────────────


@router.message(Command("services"))
@router.message(F.text.func(lambda t: bool(t) and t in _cust_labels("mine")))
async def my_services(message: Message) -> None:
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    await _render_services_page(message, uid=uid, page=0, edit=False)


async def _render_services_page(
    message: Message,
    *,
    uid: int,
    page: int = 0,
    edit: bool = False,
    query: str | None = None,
) -> None:
    # Always pass customer uid explicitly — callback.message.from_user is the bot.
    page_size = PAGE_SIZE
    with get_session() as session:
        links = repo.list_customer_services(session, uid)
    if not links:
        await _ui_show(
            message,
            "✨ هنوز سرویسی ندارید.\nاز «🛒 خرید اشتراک» شروع کنید.",
            reply_markup=customer_menu(),
            edit=edit,
        )
        return
    q = (query or "").strip().lower()
    if q:
        links = [l for l in links if q in (l.service_username or "").lower()]
        if not links:
            rows = [
                [
                    InlineKeyboardButton(
                        text="🔍 جستجوی سریع",
                        callback_data="cust:svcsearch",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت به لیست",
                        callback_data="cust:svclist",
                    )
                ],
            ]
            await _ui_show(
                message,
                f"نتیجه‌ای برای «{query}» پیدا نشد.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                edit=edit,
            )
            return
    total = len(links)
    pages = total_pages(total, page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    chunk = links[start : start + page_size]
    rows: list[list[InlineKeyboardButton]] = []
    with get_session() as session:
        for link in chunk:
            if link.subscription_id:
                cb = f"cust:sid:{int(link.subscription_id)}"
                sub = repo.get_subscription(session, int(link.subscription_id))
            else:
                cb = f"cust:svc:{link.service_username[:40]}"
                sub = repo.get_subscription_by_username(
                    session, link.service_username
                )
            icon = _plan_icon(sub)
            label = f"{icon} {ltr(link.service_username)} {icon}"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=cb,
                    )
                ]
            )
    nav = pagination_row(
        page=page, total=total, page_size=page_size, callback_prefix="cust:svcpages"
    )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text="🔍 جستجوی سریع",
                callback_data="cust:svcsearch",
            )
        ]
    )
    if q:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت به لیست",
                    callback_data="cust:svclist",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="cust:home")])
    title = "🛍 <b>اشتراک های خریداری شده توسط شما</b>"
    if q:
        title += f"\n🔎 جستجو: <b>{query}</b>"
    await _ui_show(
        message,
        f"{title}\n\n"
        "⚠️ برای مشاهده اطلاعات و مدیریت روی نام کاربری کلیک کنید\n\n"
        "🔴 همچنین برای پیدا کردن سریع سرویس خود و مدیریت آن می‌توانید از دکمه "
        "«🔍 جستجو سریع» استفاده کنید\n\n"
        f"📄 صفحه {page + 1} از {pages} | 📊 کل: {total} سرویس",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
    )


@router.callback_query(F.data.startswith("cust:svcpages:"))
async def svc_pages(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    page = int((callback.data or "").rsplit(":", 1)[1])
    uid = callback.from_user.id if callback.from_user else 0
    await _render_services_page(
        callback.message, uid=uid, page=max(0, page), edit=True
    )


@router.callback_query(F.data == "cust:svcsearch")
async def svc_search_start(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    _set_draft(uid, {"step": "svc_search"})
    await _ui_show(
        callback.message,
        "🔎 <b>جستجوی سریع</b>\n"
        "بخشی از نام سرویس را بفرستید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cust:svclist")]
            ]
        ),
        edit=True,
    )
    await callback.answer()


@router.message(F.text.in_({BTN_ORDERS, "📋 سفارش‌ها"}))
async def my_orders(message: Message) -> None:
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    with get_session() as session:
        open_orders = repo.list_customer_orders_for_user(
            session,
            uid,
            statuses=(
                "awaiting_payment",
                "receipt_window",
                "awaiting_review",
                "fulfilling",
            ),
            limit=15,
        )
        history = repo.list_customer_orders_for_user(session, uid, limit=10)
    if not open_orders and not history:
        await message.answer(
            "📋 سفارشی ثبت نشده است.",
            reply_markup=customer_menu(),
        )
        return
    if open_orders:
        await message.answer("📋 <b>سفارش‌های باز</b>")
        for o in open_orders:
            kind_fa = {"wallet": "💰 کیف پول", "create": "🛒 خرید", "renew": "♻️ تمدید"}.get(
                o.kind, o.kind
            )
            rows = []
            if o.status in ("awaiting_payment", "receipt_window", "awaiting_review"):
                rows.append(
                    [
                        InlineKeyboardButton(
                            text="📤 ارسال رسید",
                            callback_data=f"cust:receipt:{o.id}",
                        ),
                        InlineKeyboardButton(
                            text="🚫 لغو",
                            callback_data=f"cust:cancel:{o.id}",
                        ),
                    ]
                )
                if o.status in ("awaiting_payment", "receipt_window"):
                    rows.insert(
                        0,
                        [
                            InlineKeyboardButton(
                                text="💳 مشاهده پرداخت",
                                callback_data=f"cust:orderpay:{o.id}",
                            )
                        ],
                    )
            await message.answer(
                f"#{o.id} — {kind_fa}\n"
                f"وضعیت: <b>{_status_fa(o.status)}</b>\n"
                f"یوزر: <code>{o.service_username}</code>\n"
                f"مبلغ: {o.price_toman:,} ت"
                + (
                    f"\nمهلت: {_deadline_text(o.expires_at)}"
                    if o.expires_at
                    else ""
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
                if rows
                else None,
            )
    doneish = [
        o
        for o in history
        if o.status in ("done", "rejected", "expired", "cancelled")
    ][:5]
    if doneish:
        lines = ["🗂 <b>تاریخچه اخیر</b>"]
        for o in doneish:
            lines.append(
                f"• #{o.id} {_status_fa(o.status)} — {o.price_toman:,} ت "
                f"(<code>{o.service_username}</code>)"
            )
        await message.answer("\n".join(lines), reply_markup=customer_menu())


@router.callback_query(F.data.startswith("cust:orderpay:"))
async def order_pay_resume(callback: CallbackQuery, settings: Settings) -> None:
    oid = int((callback.data or "").rsplit(":", 1)[1])
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        order = repo.get_customer_order(session, oid)
        if not order or order.telegram_id != uid:
            await callback.answer("یافت نشد", show_alert=True)
            return
        if order.status not in ("awaiting_payment", "receipt_window"):
            await callback.answer(_status_fa(order.status), show_alert=True)
            return
    await _send_pay_for_order(callback.message, settings, oid)
    await callback.answer()


@router.callback_query(F.data == "cust:home")
async def cust_home(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    uid = callback.from_user.id if callback.from_user else 0
    _set_flow(uid, "")
    await _show_customer_home(callback.message, edit=True)


async def _show_support_hub(
    message: Message, settings: Settings, *, edit: bool = False
) -> None:
    """Support options only in chat (inline); bottom menu stays the main keyboard."""
    text = (
        f"{_msg('support_hint', support=settings.support_text)}\n\n"
        "از دکمه‌های زیر یکی را انتخاب کنید:"
    )
    await _ui_show(message, text, reply_markup=_support_hub_inline(), edit=edit)


@router.callback_query(F.data.startswith("cust:go:"))
async def cust_go(callback: CallbackQuery, settings: Settings) -> None:
    """Inline hub shortcuts — keep edit-in-place where possible."""
    await safe_answer(callback)
    action = (callback.data or "").rsplit(":", 1)[-1]
    uid = callback.from_user.id if callback.from_user else 0
    _set_flow(uid, "")
    if action == "buy":
        await _buy_start_ui(callback.message, edit=True)
        return
    if action == "mine":
        await _render_services_page(callback.message, uid=uid, page=0, edit=True)
        return
    if action == "orders":
        # Orders currently renders as a fresh message (media/receipts-friendly).
        await my_orders(callback.message)
        return
    if action == "wallet":
        await _wallet_home_ui(callback.message, uid=uid, edit=True)
        return
    if action == "prices":
        await show_prices(callback.message)
        return
    if action == "guide":
        await _show_education_menu(callback.message, edit=True)
        return
    if action == "support":
        _set_flow(uid, "support_hub")
        await _show_support_hub(callback.message, settings, edit=True)
        return
    await _show_customer_home(callback.message, edit=True)


@router.callback_query(F.data == "cust:ticket:leave")
async def ticket_leave(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    _set_flow(uid, "")
    await callback.message.answer("از گفتگو خارج شدید.", reply_markup=customer_menu())
    await callback.answer()


@router.callback_query(F.data == "cust:ticket:stay")
async def ticket_stay(callback: CallbackQuery) -> None:
    await callback.message.answer("گفتگو ادامه دارد؛ پیام بعدی‌تان ارسال می‌شود.")
    await callback.answer()


def _service_actions_kb(username: str) -> InlineKeyboardMarkup:
    from app.telegram.menu_config import build_customer_inline_menu

    return build_customer_inline_menu("service_actions", username=username)


@router.callback_query(F.data == "cust:svclist")
async def svc_list(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    await _render_services_page(callback.message, uid=uid, page=0, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("cust:sid:"))
async def svc_open_by_id(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    try:
        sub_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("داده نامعتبر", show_alert=True)
        return
    username = _owned_username_for_sub_id(uid, sub_id)
    if not username:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    _set_draft(uid, {"step": "service", "username": username, "subscription_id": sub_id})
    sub = _load_service_for_display_by_id(sub_id) or _load_service_for_display(username)
    await _ui_show(
        callback.message,
        _format_service_info(sub, username),
        reply_markup=_service_actions_kb(username),
        edit=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cust:svc:"))
async def svc_open(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    username = (callback.data or "").split(":", 2)[2]
    if not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    _set_draft(uid, {"step": "service", "username": username})
    sub = _load_service_for_display(username)
    await _ui_show(
        callback.message,
        _format_service_info(sub, username),
        reply_markup=_service_actions_kb(username),
        edit=True,
    )
    await callback.answer()


def _owns_service(uid: int, username: str) -> bool:
    name = (username or "").strip()
    if not name:
        return False
    with get_session() as session:
        links = repo.list_customer_services(session, uid)
        return any(l.service_username == name for l in links)


def _owned_username_for_sub_id(uid: int, subscription_id: int) -> str | None:
    with get_session() as session:
        links = repo.list_customer_services(session, uid)
        for link in links:
            if link.subscription_id == subscription_id:
                return link.service_username
        sub = repo.get_subscription(session, subscription_id)
        if sub and _owns_service(uid, sub.username):
            return sub.username
    return None


def _load_service_for_display_by_id(subscription_id: int, *, force: bool = False):
    with get_session() as session:
        sub = repo.get_subscription(session, subscription_id)
        if sub:
            refresh_subscription_userinfo(session, subscription=sub, force=force)
        return sub


def _plan_icon(sub) -> str:
    """Economy vs time-unlimited volume icon for service buttons."""
    if sub is None:
        return ICON_ECONOMY
    name = (getattr(sub, "product_name", None) or "").strip()
    if "نامحدود" in name:
        return ICON_VOLUME
    try:
        with get_session() as session:
            if getattr(sub, "vendor_id", None):
                v = repo.get_vendor(session, sub.vendor_id)
                if v and v.key == "finearts":
                    return ICON_VOLUME
    except Exception:
        pass
    return ICON_ECONOMY


def _format_service_info(sub, username: str) -> str:
    if not sub:
        return (
            f"👤 <b>{ltr(username)}</b>\n"
            "وضعیت در سیستم هنوز کامل نیست.\n"
            "پس از فعال‌سازی، اطلاعات اینجا نمایش داده می‌شود."
        )
    product = sub.product_name or "—"
    expire_line = format_expire_display(getattr(sub, "expiration_date", None))
    icon = _plan_icon(sub)
    remain = getattr(sub, "remaining_traffic_mb", None)
    limit = getattr(sub, "traffic_limit_mb", None)
    pct = ""
    try:
        if remain is not None and limit is not None and float(limit) > 0:
            pct = f" · {int(round(100.0 * float(remain) / float(limit)))}٪"
    except (TypeError, ValueError):
        pct = ""
    return (
        f"📡 وضعیت: <b>{status_fa(sub.status)}</b>\n"
        f"{icon} نام سرویس: <b>{ltr(sub.username)}</b> {icon}\n"
        f"{icon} محصول: {product}\n\n"
        f"✅ حجم باقی‌مانده (مانده از خریدهای قبلی): "
        f"{format_mb(remain)}{pct}\n"
        f"📅 تاریخ اتمام : {expire_line}\n\n"
        "💡 برای قطع دسترسی دیگران از «🔐 تغییر لینک» استفاده کنید."
    )


@router.callback_query(F.data.startswith("cust:svcinfo:"))
async def svc_info(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    username = (callback.data or "").split(":", 2)[2]
    if not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer("در حال بروزرسانی…")
    sub = _load_service_for_display(username, force=True)
    await _ui_show(
        callback.message,
        _format_service_info(sub, username),
        reply_markup=_service_actions_kb(username),
        edit=True,
    )


def _link_only_kb(username: str, url: str) -> InlineKeyboardMarkup:
    u = username[:40]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 کپی لینک",
                    copy_text=CopyTextButton(text=url),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت",
                    callback_data=f"cust:svc:{u}",
                )
            ],
        ]
    )


def _create_success_kb() -> InlineKeyboardMarkup:
    from app.telegram.menu_config import build_customer_inline_menu

    return build_customer_inline_menu("create_success")


def _renew_success_kb(username: str, sub_id: int | None) -> InlineKeyboardMarkup:
    from app.telegram.menu_config import build_customer_inline_menu

    return build_customer_inline_menu(
        "renew_success", username=username, sub_id=sub_id
    )


def _create_success_caption(
    *,
    username: str,
    product: str,
    duration_days: int | None,
    url: str,
) -> str:
    days = str(duration_days) if duration_days is not None else "—"
    return (
        "✅ سرویس با موفقیت ایجاد شد\n\n"
        f"👤 نام کاربری سرویس : <code>{ltr(username).lstrip(chr(0x200E))}</code>\n"
        f"🌿محصول: {product or '—'}\n"
        f"⏳ مدت زمان: {days} روز\n\n"
        "لینک اتصال:\n"
        f"{url}\n"
        "🧑‍🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر و انتخاب سیستم عامل "
        "خود را دریافت کنید"
    )


def _renew_success_text(
    *,
    username: str,
    product: str,
    expire_jalali: str,
) -> str:
    return (
        "✅ تمدید برای سرویس شما با موفقیت صورت گرفت\n"
        f"▫️نام سرویس : <code>{ltr(username).lstrip(chr(0x200E))}</code>\n"
        f"▫️نام محصول : {product or '—'}\n"
        f"▫️تاریخ اتمام : {expire_jalali}"
    )


async def send_create_success_to_customer(
    telegram_id: int,
    *,
    username: str,
    product: str,
    duration_days: int | None,
    url: str,
) -> None:
    """Final create notice: QR photo + caption + iOS/Android/home buttons."""
    bot = customer_bridge.customer_bot
    if not bot:
        return
    link = (url or "").strip() or "—"
    caption = _create_success_caption(
        username=username,
        product=product,
        duration_days=duration_days,
        url=link,
    )
    kb = _create_success_kb()
    if link.startswith("http"):
        try:
            png = subscription_qr_png(link)
            await bot.send_photo(
                telegram_id,
                BufferedInputFile(png, filename=f"{username[:40]}_sub.png"),
                caption=caption,
                reply_markup=kb,
            )
            return
        except Exception:
            log.exception("create success QR send failed for %s", username)
    try:
        await bot.send_message(telegram_id, caption, reply_markup=kb)
    except Exception:
        log.exception("create success text fallback failed for %s", username)


async def send_renew_success_to_customer(
    telegram_id: int,
    *,
    username: str,
    product: str,
    expire_jalali: str,
    sub_id: int | None,
) -> None:
    """Final renew notice with inline back-to-list / back-to-detail."""
    bot = customer_bridge.customer_bot
    if not bot:
        return
    text = _renew_success_text(
        username=username,
        product=product,
        expire_jalali=expire_jalali,
    )
    try:
        await bot.send_message(
            telegram_id,
            text,
            reply_markup=_renew_success_kb(username, sub_id),
        )
    except Exception:
        log.exception("renew success notify failed for %s", username)


@router.callback_query(F.data.in_({"cust:setup:ios", "cust:setup:android"}))
async def setup_os_guide(callback: CallbackQuery) -> None:
    """Open matching connection-guide education category after create success."""
    await safe_answer(callback)
    kind = (callback.data or "").rsplit(":", 1)[-1]
    keywords = (
        ["آیفون", "iphone", "ios", "iOS"]
        if kind == "ios"
        else ["اندروید", "android", "Android"]
    )
    with get_session() as session:
        repo.ensure_default_education(session)
        cat = repo.find_education_category_by_keywords(session, keywords)
        cat_id = cat.id if cat else None
    if cat_id is None:
        await _show_education_menu(callback.message, edit=True)
        return
    await _show_education_menu(callback.message, parent_id=cat_id, edit=True)


async def _send_subscription_link(
    message: Message,
    *,
    username: str,
    url: str,
    caption_prefix: str,
) -> None:
    """One message: QR on top (photo), link in caption; copy + back only."""
    kb = _link_only_kb(username, url)
    caption = f"{caption_prefix}\n\n<code>{url}</code>"
    try:
        png = subscription_qr_png(url)
        await message.answer_photo(
            BufferedInputFile(png, filename=f"{username[:40]}_sub.png"),
            caption=caption,
            reply_markup=kb,
        )
    except Exception:
        log.exception("subscription QR generate/send failed for %s", username)
        await message.answer(caption, reply_markup=kb)


def _persist_subscription_url(username: str, url: str, *, force_userinfo: bool = True) -> None:
    from app.services.subscription_refresh import assign_subscription_url_if_matches

    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        if not sub:
            return
        ok = assign_subscription_url_if_matches(
            session, sub, url, apply_traffic=force_userinfo
        )
        if not ok and force_userinfo:
            # Keep old URL; still try TTL refresh on whatever is stored.
            refresh_subscription_userinfo(session, subscription=sub, force=True)


def _load_service_for_display(username: str, *, force: bool = False):
    """DB row + HTTP userinfo from stored URL (TTL unless force)."""
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        if sub:
            refresh_subscription_userinfo(session, subscription=sub, force=force)
        return sub


@router.callback_query(F.data.startswith("cust:svclink:"))
async def svc_link(callback: CallbackQuery) -> None:
    """Show subscription URL inline on the same service message (no extra chat msg)."""
    uid = callback.from_user.id if callback.from_user else 0
    username = (callback.data or "").split(":", 2)[2]
    if not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        url = (sub.subscription_url or "").strip() if sub else ""
    if not url:
        await callback.answer(
            "لینک در سیستم ذخیره نشده. پس از ساخت/تمدید یا تغییر لینک موجود می‌شود.",
            show_alert=True,
        )
        return
    await callback.answer()
    u = username[:40]
    text = (
        f"🔗 <b>لینک اشتراک</b>\n"
        f"👤 <code>{ltr(username).lstrip(chr(0x200E))}</code>\n\n"
        f"<code>{url}</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 کپی لینک",
                    copy_text=CopyTextButton(text=url),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت به مشخصات",
                    callback_data=f"cust:svc:{u}",
                )
            ],
        ]
    )
    await _ui_show(callback.message, text, reply_markup=kb, edit=True)


@router.callback_query(F.data.startswith("cust:svcchange:ask:"))
async def svc_change_link_ask(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    username = (callback.data or "").split(":")[-1]
    if not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    u = username[:40]
    await _ui_show(
        callback.message,
        "🔐 <b>تغییر لینک</b>\n\n"
        "با تغییر لینک، لینک قبلی باطل می‌شود و دستگاه‌های متصل قطع می‌شوند.\n"
        "ادامه می‌دهید؟",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ بله، تغییر بده",
                        callback_data=f"cust:svcchange:go:{u}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ انصراف",
                        callback_data=f"cust:svc:{u}",
                    )
                ],
            ]
        ),
        edit=True,
    )


@router.callback_query(F.data.startswith("cust:svcchange:go:"))
async def svc_change_link(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    username = (callback.data or "").split(":")[-1]
    if not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer("در حال تغییر لینک…")
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        service_id = sub.vendor_service_id if sub else None
        vendor = repo.resolve_vendor_for_username(session, username)
        vendor_key = vendor.key if vendor else DEFAULT_VENDOR_KEY
    caps = get_vendor_capabilities(vendor_key)
    if caps and not caps.bot_change_link:
        await _ui_show(
            callback.message,
            "تغییر لینک برای این فروشنده پشتیبانی نمی‌شود.",
            reply_markup=_service_actions_kb(username),
            edit=True,
        )
        return
    driver = get_vendor_driver(vendor_key)
    if driver is None and customer_bridge.renew_queue:
        driver = customer_bridge.renew_queue.driver
    url = None
    if driver:
        try:
            url = await driver.change_subscription_link(username, service_id)
        except Exception:
            log.exception("change subscription link failed")
    if url:
        _persist_subscription_url(username, url)
        u = username[:40]
        text = (
            "✅ لینک جدید صادر شد. لینک قبلی دیگر کار نمی‌کند.\n"
            f"🔗 <b>لینک جدید</b>\n"
            f"👤 <code>{ltr(username).lstrip(chr(0x200E))}</code>\n\n"
            f"<code>{url}</code>"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 کپی لینک",
                        copy_text=CopyTextButton(text=url),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت به مشخصات",
                        callback_data=f"cust:svc:{u}",
                    )
                ],
            ]
        )
        await _ui_show(callback.message, text, reply_markup=kb, edit=True)
    else:
        await _ui_show(
            callback.message,
            "لینک جدید هنوز آماده نیست.\n"
            "کمی بعد دوباره «لینک اشتراک» را بزنید یا از پشتیبانی پیگیری کنید.",
            reply_markup=_service_actions_kb(username),
            edit=True,
        )


@router.callback_query(F.data.startswith("cust:xfer:"))
async def svc_transfer(callback: CallbackQuery) -> None:
    """Immediate C2C local ownership transfer (DB only; no NetPlus)."""
    uid = callback.from_user.id if callback.from_user else 0
    parts = (callback.data or "").split(":")
    # cust:xfer:{username} | cust:xfer:ok | cust:xfer:no
    token = parts[2] if len(parts) > 2 else ""

    if token == "no":
        draft = _get_draft(uid)
        uname = draft.get("username") or ""
        _set_draft(uid, {"step": "service", "username": uname} if uname else {})
        await callback.message.answer(
            "انتقال لغو شد.",
            reply_markup=_service_actions_kb(uname) if uname else customer_menu(),
        )
        await callback.answer()
        return

    if token == "ok":
        draft = _get_draft(uid)
        username = str(draft.get("username") or "")
        to_tg = int(draft.get("to_tg") or 0)
        if not username or not to_tg:
            await callback.answer("درخواست منقضی شده؛ دوباره شروع کنید.", show_alert=True)
            return
        if not _owns_service(uid, username):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        try:
            with get_session() as session:
                link = repo.transfer_local_ownership(
                    session,
                    from_telegram_id=uid,
                    to_telegram_id=to_tg,
                    service_username=username,
                )
                uname = link.service_username
        except ValueError as exc:
            code = str(exc)
            if code == "recipient_profile_missing":
                msg = (
                    "گیرنده هنوز ربات را استارت نکرده و پروفایل ندارد.\n"
                    "اول باید /start بزند."
                )
            elif code == "same_owner":
                msg = "نمی‌توانید سرویس را به خودتان منتقل کنید."
            elif code == "not_owner":
                msg = "این سرویس دیگر مال شما نیست."
            else:
                msg = f"انتقال ناموفق: {code}"
            await callback.message.answer(msg, reply_markup=_service_actions_kb(username))
            await callback.answer()
            return
        _set_draft(uid, {})
        await callback.message.answer(
            f"✅ مالکیت <code>{uname}</code> به <code>{to_tg}</code> منتقل شد.\n"
            "این انتقال فقط در ربات ماست (NetPlus تغییر نکرد).",
            reply_markup=customer_menu(),
        )
        if customer_bridge.customer_bot:
            try:
                await customer_bridge.customer_bot.send_message(
                    to_tg,
                    f"🎁 سرویس <code>{uname}</code> به شما منتقل شد.\n"
                    "از منوی «سرویس‌های من» می‌توانید ببینید.",
                    reply_markup=customer_menu(),
                )
            except Exception:
                log.exception("notify transfer recipient failed")
        await callback.answer("منتقل شد")
        return

    username = token
    if not username or not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    _set_draft(uid, {"step": "transfer_wait_tg", "username": username})
    await callback.message.answer(
        f"🔁 انتقال مالکیت <code>{username}</code>\n\n"
        "شناسه عددی تلگرام گیرنده را بفرستید.\n"
        "گیرنده باید قبلاً ربات را استارت کرده باشد.\n"
        "انتقال فوری است و فقط مالکیت محلی را عوض می‌کند.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🚫 انصراف",
                        callback_data="cust:xfer:no",
                    )
                ]
            ]
        ),
    )
    await callback.answer()


def _renew_family_for_username(username: str) -> str:
    """economy|volume for an existing service (FineArts / نامحدود = volume)."""
    with get_session() as session:
        sub = repo.get_subscription_by_username(session, username)
        if not sub:
            return "economy"
        name = (sub.product_name or "").strip()
        if "نامحدود" in name:
            return "volume"
        vendor = (
            repo.get_vendor(session, sub.vendor_id) if sub.vendor_id else None
        )
        if vendor and (vendor.key or "").lower() == "finearts":
            return "volume"
        return "economy"


async def _show_renew_choices(
    message: Message, username: str, *, edit: bool = False
) -> None:
    u = username[:40]
    family = _renew_family_for_username(username)
    with get_session() as session:
        plans = repo.list_price_plans(session, enabled_only=True)
        has_current = repo.find_renew_plan_for_username(session, username) is not None
        has_economy = any(
            int(p.duration_days) > 0
            for p in plans
            if p.duration_days is not None
        )
        has_volume = any(
            int(p.duration_days) == 0
            for p in plans
            if p.duration_days is not None
        )
    if not has_economy and not has_volume and not has_current:
        await _ui_show(
            message,
            "پلنی برای تمدید فعال نیست.",
            reply_markup=customer_menu(),
            edit=edit,
        )
        return
    rows: list[list[InlineKeyboardButton]] = []
    if has_economy:
        if family == "economy":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✨ پلن اقتصادی",
                        callback_data=f"cust:renew:panel:economy:{u}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✨ پلن اقتصادی (غیرفعال)",
                        callback_data=f"cust:renew:locked:{u}",
                    )
                ]
            )
    if has_volume:
        if family == "volume":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="💎 پلن حجمی مدت نامحدود",
                        callback_data=f"cust:renew:panel:volume:{u}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="💎 پلن حجمی (غیرفعال)",
                        callback_data=f"cust:renew:locked:{u}",
                    )
                ]
            )
    if has_current:
        rows.append(
            [
                InlineKeyboardButton(
                    text="♻️ تمدید پلن فعلی",
                    callback_data=f"cust:renew:current:{u}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ بازگشت",
                callback_data=f"cust:svc:{u}",
            )
        ]
    )
    family_fa = "✨ اقتصادی" if family == "economy" else "💎 حجمی"
    await _ui_show(
        message,
        f"♻️ تمدید <b>{username}</b>\n"
        f"نوع سرویس فعلی: <b>{family_fa}</b>\n"
        "فقط همان نوع پلن قابل انتخاب است.\n"
        "برای تغییر نوع پلن از <b>خرید سرویس جدید</b> استفاده کنید.\n"
        "یا پلن فعلی را تمدید کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
    )


async def _show_renew_volumes(
    message: Message, draft: dict, *, edit: bool = False
) -> None:
    raw_days = draft.get("duration_days")
    days = int(raw_days) if raw_days is not None else 31
    panel = str(draft.get("panel") or "economy")
    uid = message.from_user.id if message.from_user else 0
    rows: list[list[InlineKeyboardButton]] = []
    icon = ICON_VOLUME if days == 0 else ICON_ECONOMY
    with get_session() as session:
        plans = [
            p
            for p in _plans_for_panel(
                repo.list_price_plans(session, enabled_only=True), panel
            )
            if int(p.duration_days) == days
        ]
        for plan in plans:
            gb = int(plan.traffic_gb) if plan.traffic_gb else 0
            price = _price_for_uid(session, plan, uid)
            label = (
                f"{icon} {gb} گیگ — {price:,} ت"
                if gb
                else f"{icon} {plan.title}"
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"cust:renew:vol:{plan.id}",
                    )
                ]
            )
    if not rows:
        await _ui_show(
            message,
            "پلنی برای این مدت تعریف نشده.",
            reply_markup=customer_menu(),
            edit=edit,
        )
        return
    await _ui_show(
        message,
        "🔋 حجم تمدید را انتخاب کنید:",
        reply_markup=_back_kb(*rows),
        edit=edit,
    )


@router.callback_query(F.data.startswith("cust:svcrenew:"))
async def svc_renew(callback: CallbackQuery, settings: Settings) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    username = (callback.data or "").split(":", 2)[2]
    if not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    _set_draft(
        uid,
        {
            "step": "renew_choice",
            "username": username,
            "kind": "renew",
            "panel": "economy",
        },
    )
    await _show_renew_choices(callback.message, username, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("cust:renew:current:"))
async def renew_current(callback: CallbackQuery, settings: Settings) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    username = (callback.data or "").split(":", 3)[3]
    if not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    with get_session() as session:
        plan = repo.find_renew_plan_for_username(session, username)
    if not plan:
        await callback.answer("پلنی برای تمدید فعلی نیست", show_alert=True)
        return
    draft = {
        "step": "invoice",
        "panel": "economy",
        "duration_days": plan.duration_days,
        "plan_id": plan.id,
        "username": username,
        "kind": "renew",
        "plan_mode": "current",
    }
    await _show_invoice(callback.message, uid, draft, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("cust:renew:locked:"))
async def renew_locked_family(callback: CallbackQuery) -> None:
    await callback.answer(
        "این نوع پلن با سرویس فعلی یکی نیست.\n"
        "برای تغییر نوع، از خرید سرویس جدید استفاده کنید.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("cust:renew:panel:"))
async def renew_panel(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    parts = (callback.data or "").split(":")
    # cust:renew:panel:{panel}:{username}
    if len(parts) < 5:
        await callback.answer("داده نامعتبر", show_alert=True)
        return
    panel = parts[3]
    username = parts[4]
    if not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    family = _renew_family_for_username(username)
    if panel != family:
        await callback.answer(
            "این نوع پلن با سرویس فعلی یکی نیست.\n"
            "برای تغییر نوع، از خرید سرویس جدید استفاده کنید.",
            show_alert=True,
        )
        return
    mode = next(
        (p.get("duration_mode") for p in PANELS if p["key"] == panel), "timed"
    )
    if mode == "unlimited":
        draft = {
            "step": "renew_volume",
            "panel": panel,
            "duration_days": 0,
            "username": username,
            "kind": "renew",
            "plan_mode": "change",
        }
        _set_draft(uid, draft)
        await _show_renew_volumes(callback.message, draft, edit=True)
    else:
        draft = {
            "step": "renew_choice",
            "panel": panel,
            "username": username,
            "kind": "renew",
            "plan_mode": "change",
        }
        _set_draft(uid, draft)
        with get_session() as session:
            plans = _plans_for_panel(
                repo.list_price_plans(session, enabled_only=True), panel
            )
            days_list = sorted(
                {
                    int(p.duration_days)
                    for p in plans
                    if p.duration_days is not None
                }
            )
        rows: list[list[InlineKeyboardButton]] = []
        u = username[:40]
        for days in days_list:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=_DURATION_LABELS.get(days, f"{days} روز"),
                        callback_data=f"cust:renew:dur:{days}:{u}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت",
                    callback_data=f"cust:svcrenew:{u}",
                )
            ]
        )
        await _ui_show(
            callback.message,
            f"⏱ مدت تمدید را انتخاب کنید:\nپلن: <b>{_panel_title(panel)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            edit=True,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("cust:renew:dur:"))
async def renew_duration(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    parts = (callback.data or "").split(":")
    # cust:renew:dur:{days}:{username}
    if len(parts) < 5:
        await callback.answer("داده نامعتبر", show_alert=True)
        return
    days = int(parts[3])
    username = parts[4]
    if not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    draft = {
        "step": "renew_volume",
        "panel": "economy" if days > 0 else "volume",
        "duration_days": days,
        "username": username,
        "kind": "renew",
        "plan_mode": "change",
    }
    _set_draft(uid, draft)
    await _show_renew_volumes(callback.message, draft, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("cust:renew:vol:"))
async def renew_volume(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    plan_id = int((callback.data or "").rsplit(":", 1)[1])
    draft = _get_draft(uid)
    username = str(draft.get("username") or "")
    if not username or not _owns_service(uid, username):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    with get_session() as session:
        plan = repo.get_price_plan(session, plan_id)
        if not plan or not plan.enabled:
            await callback.answer("پلن نامعتبر", show_alert=True)
            return
        raw_wanted = draft.get("duration_days")
        wanted_days = (
            int(raw_wanted)
            if raw_wanted is not None
            else int(plan.duration_days)
        )
        if int(plan.duration_days) != wanted_days:
            await callback.answer("پلن با مدت انتخاب‌شده هم‌خوان نیست", show_alert=True)
            return
    draft.update(
        {
            "step": "invoice",
            "plan_id": plan_id,
            "kind": "renew",
            "plan_mode": "change",
            "duration_days": wanted_days,
        }
    )
    await _show_invoice(callback.message, uid, draft, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("cust:renew:pay"))
async def renew_pay(callback: CallbackQuery, settings: Settings) -> None:
    data = callback.data or "cust:renew:pay:mix"
    mode = data.rsplit(":", 1)[-1]
    if mode not in ("full", "mix", "card"):
        mode = "mix"
    await _execute_shop_pay(callback, settings, kind="renew", mode=mode)


# ─── Wallet ───────────────────────────────────────────────────────────────────


@router.message(Command("wallet"))
@router.message(F.text.func(lambda t: bool(t) and t in _cust_labels("wallet")))
async def wallet_home(message: Message) -> None:
    _track(message)
    await _wallet_home_ui(message, edit=False)


async def _wallet_home_ui(
    message: Message, *, uid: int | None = None, edit: bool = False
) -> None:
    uid = uid if uid is not None else (message.from_user.id if message.from_user else 0)
    with get_session() as session:
        bal = repo.get_wallet_toman(session, uid)
        mn, mx = repo.get_wallet_limits(session)
        pct = repo.get_referral_percent(session)
        tier = repo.get_customer_rate_tier(session, uid)
    ref_link = ""
    try:
        me = await message.bot.get_me()
        if me.username:
            ref_link = f"https://t.me/{me.username}?start=ref_{uid}"
    except Exception:
        ref_link = ""
    tier_fa = repo.RATE_TIER_LABELS.get(tier, tier)
    lines = [
        "💰 <b>کیف پول</b>\n",
        "🆔 <b>شناسه کاربری تلگرام</b>",
        f"<blockquote><code>{uid}</code></blockquote>",
        "👆 نگه دارید روی شناسه یا دکمه کپی را بزنید.\n",
        f"💳 موجودی قابل استفاده: <b>{bal:,}</b> تومان",
        f"📉 حداقل شارژ: <b>{mn:,}</b> تومان",
        f"📈 حداکثر شارژ: <b>{mx:,}</b> تومان",
        f"🏷 نرخ شما: <b>{tier_fa}</b>",
        "⚠️ امکان برداشت وجه از کیف پول وجود ندارد.\n",
    ]
    if pct > 0 and ref_link:
        lines.append(
            f"🎁 مجموعه‌گیری ({pct}٪):\n<code>{ref_link}</code>\n"
            "با خرید/تمدید زیرمجموعه‌تان، کمیسیون به کیف پول‌تان واریز می‌شود.\n"
        )
    kb_rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📋 کپی شناسه کاربری",
                copy_text=CopyTextButton(text=str(uid)),
            )
        ],
    ]
    if ref_link:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="📋 کپی لینک مجموعه‌گیری",
                    copy_text=CopyTextButton(text=ref_link),
                )
            ]
        )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="➕ افزایش موجودی",
                callback_data="cust:wallet:charge",
            )
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="cust:home")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await _ui_show(message, "\n".join(lines), reply_markup=kb, edit=edit)


@router.callback_query(F.data == "cust:wallet:charge")
async def wallet_charge_start(callback: CallbackQuery, settings: Settings) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        open_w = repo.find_open_wallet_charge(session, uid)
        if open_w:
            oid = open_w.id
            status = open_w.status
        else:
            oid = None
            status = ""
            mn, mx = repo.get_wallet_limits(session)
            prompt = repo.get_bot_message(
                session, "wallet_amount_prompt", min=f"{mn:,}", max=f"{mx:,}"
            )
    if oid:
        await _ui_show(
            callback.message,
            f"یک شارژ باز دارید (سفارش #{oid} — {_status_fa(status)}).\n"
            "می‌توانید همان را ادامه دهید یا لغو کنید و دوباره شارژ بسازید.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💳 ادامه پرداخت",
                            callback_data=f"cust:orderpay:{oid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📤 ارسال رسید",
                            callback_data=f"cust:receipt:{oid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🚫 لغو و شروع دوباره",
                            callback_data=f"cust:wallet:restart:{oid}",
                        )
                    ],
                ]
            ),
            edit=True,
        )
        await callback.answer()
        return
    _set_draft(uid, {"step": "wallet_amount"})
    await _ui_show(
        callback.message, prompt, reply_markup=_back_kb(), edit=True
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cust:wallet:restart:"))
async def wallet_charge_restart(
    callback: CallbackQuery, settings: Settings
) -> None:
    oid = int((callback.data or "").rsplit(":", 1)[1])
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        order = repo.get_customer_order(session, oid)
        if order and order.telegram_id == uid:
            repo.cancel_customer_order(session, order, reason="wallet_restart")
        mn, mx = repo.get_wallet_limits(session)
        prompt = repo.get_bot_message(
            session, "wallet_amount_prompt", min=f"{mn:,}", max=f"{mx:,}"
        )
    _set_draft(uid, {"step": "wallet_amount"})
    await callback.message.answer(prompt, reply_markup=_back_kb())
    await callback.answer("لغو شد")


async def _start_wallet_charge(
    message: Message, uid: int, amount_toman: int, settings: Settings
) -> None:
    with get_session() as session:
        mn, mx = repo.get_wallet_limits(session)
        range_msg = repo.get_bot_message(
            session, "wallet_amount_range", min=f"{mn:,}", max=f"{mx:,}"
        )
    if amount_toman < mn or amount_toman > mx:
        # Keep draft step so the customer can retry without restarting
        _set_draft(uid, {"step": "wallet_amount"})
        await message.answer(range_msg, reply_markup=_back_kb())
        return
    if not _can_take_payment(settings):
        card, _ = _card(settings)
        if not card:
            _set_draft(uid, {"step": "wallet_amount"})
            await message.answer(
                "پرداخت هنوز پیکربندی نشده. به پشتیبانی پیام دهید.",
                reply_markup=_back_kb(),
            )
            return
    with get_session() as session:
        order = repo.create_customer_order(
            session,
            telegram_id=uid,
            kind="wallet",
            plan_id=None,
            service_username="WALLET",
            price_toman=amount_toman,
        )
        oid = order.id
    _set_flow(uid, f"wait_receipt:{oid}")
    await _send_pay_for_order(message, settings, oid)


# ─── Support ──────────────────────────────────────────────────────────────────


def _support_hub_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎟 ارسال پیام به پشتیبانی",
                    callback_data="cust:support:new",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎫 تیکت‌های من",
                    callback_data="cust:support:tickets",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 منوی اصلی",
                    callback_data="cust:home",
                )
            ],
        ]
    )


@router.message(Command("support"))
@router.message(F.text.func(lambda t: bool(t) and t in _cust_labels("support")))
async def support_hint(message: Message, settings: Settings) -> None:
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    _set_flow(uid, "support_hub")
    # Keep main bottom keyboard; show support actions only in this chat message.
    await message.answer(
        f"{_msg('support_hint', support=settings.support_text)}\n\n"
        "از دکمه‌های زیر یکی را انتخاب کنید:",
        reply_markup=_support_hub_inline(),
    )


@router.message(F.text.func(lambda t: bool(t) and t in _support_labels("back")))
async def support_back_to_menu(message: Message) -> None:
    """Legacy support reply-keyboard «back» — keep main menu, clear support flow."""
    uid = message.from_user.id if message.from_user else 0
    flow = _get_flow(uid)
    if flow.startswith("support") or flow.startswith("ticket:"):
        _set_flow(uid, "")
    await message.answer("🏠 منوی اصلی", reply_markup=customer_menu())


@router.message(F.text.func(lambda t: bool(t) and t in _support_labels("support_send")))
async def support_send_start(message: Message, settings: Settings) -> None:
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    _set_flow(uid, "support_subject")
    await message.answer(
        _msg("support_ask_subject", max=SUPPORT_SUBJECT_MAX),
        reply_markup=customer_menu(),
    )


@router.message(F.text.func(lambda t: bool(t) and t in _support_labels("my_tickets")))
async def support_my_tickets(message: Message) -> None:
    """Open tickets in a separate message panel (NetPlus-style «تیکت‌های من»)."""
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    with get_session() as session:
        tickets = repo.list_customer_tickets(session, uid, limit=30)
    if not tickets:
        await message.answer(
            "🎫 تیکتی ندارید.\nاز «ارسال پیام به پشتیبانی» تیکت جدید بسازید.",
            reply_markup=_support_hub_inline(),
        )
        return
    lines = ["🎫 <b>تیکت‌های من</b>\nیک تیکت را انتخاب کنید:"]
    rows: list[list[InlineKeyboardButton]] = []
    status_fa = {
        "open": "باز",
        "awaiting_admin": "منتظر پشتیبانی",
        "awaiting_customer": "منتظر شما",
        "closed": "بسته",
    }
    for t in tickets:
        subj = (t.subject or "بدون موضوع")[:28]
        st = status_fa.get(t.status, t.status)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{t.id} · {st} · {subj}",
                    callback_data=f"cust:ticket:open:{t.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🎟 تیکت جدید",
                callback_data="cust:support:new",
            )
        ]
    )
    # New message = separate «window» from the support hub text.
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "cust:support:new")
async def cust_support_new(callback: CallbackQuery, settings: Settings) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    _set_flow(uid, "support_subject")
    await callback.message.answer(
        _msg("support_ask_subject", max=SUPPORT_SUBJECT_MAX),
        reply_markup=customer_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "cust:support:tickets")
async def cust_support_tickets_inline(callback: CallbackQuery) -> None:
    """Inline entry to the tickets list (chat-only; bottom menu unchanged)."""
    await callback.answer()
    await support_my_tickets(callback.message)


@router.callback_query(F.data.startswith("cust:ticket:open:"))
async def cust_ticket_open(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    ticket_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        ticket = repo.get_support_ticket(session, ticket_id)
        if not ticket or ticket.telegram_id != uid:
            await callback.answer("یافت نشد", show_alert=True)
            return
        msgs = repo.list_support_messages(session, ticket_id)
    status_fa = {
        "open": "باز",
        "awaiting_admin": "منتظر پشتیبانی",
        "awaiting_customer": "منتظر شما",
        "closed": "بسته",
    }
    lines = [
        f"🎫 <b>تیکت #{ticket.id}</b>",
        f"📌 موضوع: {ticket.subject or '—'}",
        f"📡 وضعیت: {status_fa.get(ticket.status, ticket.status)}",
        "",
    ]
    for m in msgs[-12:]:
        who = {"customer": "شما", "admin": "پشتیبانی", "system": "سیستم"}.get(
            m.sender, m.sender
        )
        body = (m.body or "").strip()
        if len(body) > 400:
            body = body[:400] + "…"
        lines.append(f"<b>{who}:</b> {body}")
    kb_rows: list[list[InlineKeyboardButton]] = []
    if ticket.status != "closed":
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="💬 پاسخ به این تیکت",
                    callback_data=f"cust:ticket:resume:{ticket.id}",
                )
            ]
        )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🎫 بازگشت به لیست",
                callback_data="cust:tickets:list",
            )
        ]
    )
    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cust:ticket:resume:"))
async def cust_ticket_resume(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    ticket_id = int((callback.data or "").rsplit(":", 1)[1])
    with get_session() as session:
        ticket = repo.get_support_ticket(session, ticket_id)
        if not ticket or ticket.telegram_id != uid:
            await callback.answer("یافت نشد", show_alert=True)
            return
        if ticket.status == "closed":
            await callback.answer("این تیکت بسته است", show_alert=True)
            return
    _set_flow(uid, f"ticket:{ticket_id}")
    await callback.message.answer(
        f"💬 ادامه گفتگو در تیکت #{ticket_id}\nپیام بعدی شما ارسال می‌شود.",
        reply_markup=customer_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "cust:tickets:list")
async def cust_tickets_list_cb(callback: CallbackQuery) -> None:
    await support_my_tickets(callback.message)
    await callback.answer()


# ─── Cancel / receipts ────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("cust:receipt:"))
async def submit_receipt_prompt(callback: CallbackQuery) -> None:
    order_id = int((callback.data or "").rsplit(":", 1)[1])
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order or order.telegram_id != uid:
            await callback.answer("یافت نشد", show_alert=True)
            return
        if order.status in ("expired", "cancelled", "done", "rejected", "fulfilling"):
            await callback.answer("این سفارش دیگر رسید نمی‌پذیرد", show_alert=True)
            msg = repo.get_bot_message(session, "receipt_rejected_expired")
            await callback.message.answer(msg, reply_markup=customer_menu())
            return
        if order.status not in (
            "awaiting_payment",
            "receipt_window",
            "awaiting_review",
        ):
            await callback.answer(f"وضعیت: {order.status}", show_alert=True)
            return
    _set_flow(uid, f"wait_receipt:{order_id}")
    # No extra prompt message in chat — short alert is enough.
    await callback.answer("عکس یا فایل رسید را همین‌جا بفرستید.", show_alert=True)


@router.callback_query(F.data.startswith("cust:cancel:"))
async def cancel_order(callback: CallbackQuery) -> None:
    order_id = int((callback.data or "").rsplit(":", 1)[1])
    uid = callback.from_user.id if callback.from_user else 0
    with get_session() as session:
        order = repo.get_customer_order(session, order_id)
        if not order or order.telegram_id != uid:
            await callback.answer("یافت نشد", show_alert=True)
            return
        if order.status not in ("awaiting_payment", "receipt_window", "awaiting_review"):
            await callback.answer("قابل لغو نیست", show_alert=True)
            return
        refunded = repo.cancel_customer_order(session, order, reason="customer_cancel")
        repo.set_state(session, f"cust_flow:{uid}", "")
    # Remove pay/card instructions so the customer cannot pay a cancelled order by mistake.
    try:
        await cleanup_customer_order_ui(uid, order_id)
    except Exception:
        log.exception("cleanup after cancel failed order=%s", order_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    msg = "سفارش لغو شد و پیام‌های پرداخت پاک شد."
    if refunded:
        msg += f"\n<code>{refunded:,}</code> تومان به کیف پول برگشت."
    await callback.message.answer(msg, reply_markup=customer_menu())
    await callback.answer("لغو شد")


async def _forward_receipt_media_to_admins(
    message: Message,
    settings: Settings,
    *,
    caption: str,
    kb: InlineKeyboardMarkup | None,
    order_id: int | None = None,
) -> None:
    """Re-upload media via bytes so admin bot can display customer-bot receipts."""
    if not (customer_bridge.admin_bot and settings.admin_ids):
        return
    from aiogram.types import BufferedInputFile

    bot = customer_bridge.admin_bot
    data: bytes | None = None
    filename = "receipt.bin"
    kind = "doc"
    try:
        if message.photo:
            file = await message.bot.get_file(message.photo[-1].file_id)
            buf = await message.bot.download_file(file.file_path)
            data = buf.read() if hasattr(buf, "read") else bytes(buf)
            filename = "receipt.jpg"
            kind = "photo"
        elif message.document:
            file = await message.bot.get_file(message.document.file_id)
            buf = await message.bot.download_file(file.file_path)
            data = buf.read() if hasattr(buf, "read") else bytes(buf)
            filename = message.document.file_name or "receipt.bin"
            kind = "doc"
        elif message.video:
            file = await message.bot.get_file(message.video.file_id)
            buf = await message.bot.download_file(file.file_path)
            data = buf.read() if hasattr(buf, "read") else bytes(buf)
            filename = "receipt.mp4"
            kind = "video"
        elif message.animation:
            file = await message.bot.get_file(message.animation.file_id)
            buf = await message.bot.download_file(file.file_path)
            data = buf.read() if hasattr(buf, "read") else bytes(buf)
            filename = "receipt.gif"
            kind = "animation"
    except Exception:
        log.exception("download receipt for admin reupload failed")
        data = None

    for admin_id in settings.admin_ids:
        sent = None
        try:
            if data:
                inp = BufferedInputFile(data, filename=filename)
                if kind == "photo":
                    sent = await bot.send_photo(
                        admin_id, inp, caption=caption, reply_markup=kb
                    )
                elif kind == "video":
                    sent = await bot.send_video(
                        admin_id, inp, caption=caption, reply_markup=kb
                    )
                elif kind == "animation":
                    sent = await bot.send_animation(
                        admin_id, inp, caption=caption, reply_markup=kb
                    )
                else:
                    sent = await bot.send_document(
                        admin_id, inp, caption=caption, reply_markup=kb
                    )
            else:
                sent = await bot.send_message(admin_id, caption, reply_markup=kb)
        except Exception:
            try:
                sent = await bot.send_message(admin_id, caption, reply_markup=kb)
            except Exception:
                sent = None
        if order_id and sent and getattr(sent, "message_id", None):
            try:
                with get_session() as session:
                    repo.track_admin_order_ui_message(
                        session, order_id, admin_id, int(sent.message_id)
                    )
            except Exception:
                log.debug("track admin order ui failed order=%s", order_id)


@router.message(F.photo | F.document | F.video | F.animation | F.video_note)
async def on_receipt_media(message: Message, settings: Settings) -> None:
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    blocked_msg = ""
    with get_session() as session:
        flow = repo.get_state(session, f"cust_flow:{uid}")
        order = None
        if flow.startswith("wait_receipt:"):
            order = repo.get_customer_order(session, int(flow.split(":", 1)[1]))
        if order is None:
            opens = repo.list_open_customer_orders(session, uid)
            order = next(
                (
                    o
                    for o in opens
                    if o.status
                    in ("awaiting_payment", "receipt_window", "awaiting_review")
                ),
                None,
            )
        oid = None
        uname = "—"
        kind = "unknown"
        amount = "—"
        price = 0
        status = ""
        created = "—"
        kb = None
        if order and order.status in (
            "expired",
            "cancelled",
            "done",
            "rejected",
            "fulfilling",
        ):
            blocked_msg = repo.get_bot_message(session, "receipt_rejected_expired")
            order = None
        resubmit = False
        vendor_direct_receipt = False
        if order:
            vendor_direct_receipt = repo.order_is_vendor_direct(order)
            if order.status == "awaiting_review":
                resubmit = True
            else:
                order.status = "awaiting_review"
                if not vendor_direct_receipt:
                    order.pay_source = "customer_receipt"
                repo.touch_customer_order(order)
            oid = order.id
            uname = order.service_username
            kind = order.kind
            amount = order.amount_rial or "—"
            price = order.price_toman
            status = order.status
            created = (
                order.created_at.strftime("%Y-%m-%d %H:%M")
                if order.created_at
                else "—"
            )
            repo.set_state(session, f"cust_flow:{uid}", "")
            approve_label = (
                "✅ تایید شارژ کیف پول"
                if kind == "wallet"
                else "✅ تایید و صف سرویس"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=approve_label,
                            callback_data=f"corder:ok:{oid}",
                        ),
                        InlineKeyboardButton(
                            text="🚫 رد با دلیل",
                            callback_data=f"corder:no:{oid}",
                        ),
                    ]
                ]
            )
            received = repo.get_bot_message(session, "receipt_received")
            if vendor_direct_receipt:
                received = (
                    "✅ رسید دریافت شد.\n"
                    "در حال بررسی تایید پرداخت… "
                    "اگر تا حدود ۳ دقیقه خودکار تایید نشد، رسید برای پیگیری ارسال می‌شود."
                )
            elif resubmit:
                received = (
                    "✅ رسید جدید دریافت شد و جایگزین/تکمیل بررسی قبلی شد.\n"
                    "نتیجه پس از بررسی اعلام می‌شود."
                )
        else:
            received = ""
            # Rate-limit orphan media spam to admins
            last = repo.get_state(session, f"orphan_media_at:{uid}")
            now_ts = str(int(datetime.utcnow().timestamp()))
            if last and now_ts.isdigit() and last.isdigit():
                if int(now_ts) - int(last) < 60:
                    blocked_msg = (
                        "لطفاً کمی صبر کنید یا از «پشتیبانی» پیام متنی بفرستید."
                    )
            if not blocked_msg:
                repo.set_state(session, f"orphan_media_at:{uid}", now_ts)

    if blocked_msg:
        await message.answer(blocked_msg, reply_markup=customer_menu())
        return

    if oid and vendor_direct_receipt:
        try:
            from app.services.vendor_direct_pay import store_customer_receipt_bytes

            file_bytes = b""
            filename = "receipt.jpg"
            if message.photo:
                file = await message.bot.get_file(message.photo[-1].file_id)
                buf = await message.bot.download_file(file.file_path)
                file_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
            elif message.document:
                file = await message.bot.get_file(message.document.file_id)
                buf = await message.bot.download_file(file.file_path)
                file_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
                filename = message.document.file_name or "receipt.bin"
            if file_bytes:
                store_customer_receipt_bytes(oid, file_bytes, filename=filename)
        except Exception:
            log.exception("store vendor_direct receipt failed order=%s", oid)

    if oid:
        kind_fa = {
            "wallet": "شارژ کیف پول",
            "create": "خرید",
            "renew": "تمدید",
        }.get(kind, kind)
        vd_tag = " · پرداخت مستقیم فروشنده" if vendor_direct_receipt else ""
        caption = (
            f"💳 <b>تراکنش جهت تایید</b>"
            f"{' — ارسال مجدد' if resubmit else ''}{vd_tag}\n"
            f"سفارش #{oid} — {kind_fa}\n"
            f"یوزر: <code>{uname}</code>\n"
            f"مبلغ: {price:,} ت / <code>{amount}</code> ریال\n"
            f"وضعیت: {_status_fa(status)}\n"
            f"ثبت: {created}\n"
            f"از: <code>{uid}</code> "
            f"@{message.from_user.username if message.from_user else ''}\n"
            f"نیاز به واکنش ادمین: تایید یا رد"
        )
    else:
        caption = (
            f"🧾 <b>رسانه بدون سفارش باز</b>\n"
            f"از: <code>{uid}</code> "
            f"@{message.from_user.username if message.from_user else ''}\n"
            f"نیاز به واکنش ادمین"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="↩️ پاسخ به مشتری",
                        callback_data=f"support:reply:{uid}",
                    )
                ]
            ]
        )

    # Forward to admin first (needs media), then keep customer chat clean:
    # only the «receipt received» line — delete the customer's media message.
    await _forward_receipt_media_to_admins(
        message, settings, caption=caption, kb=kb, order_id=oid
    )
    if oid and not (customer_bridge.admin_bot and settings.admin_ids):
        log.warning("admin bot unavailable for pending transaction order=%s", oid)
    elif oid:
        # Short text ping so admins notice even if media caption is collapsed.
        try:
            await customer_bridge.notify_admins_action_required(
                settings.admin_ids,
                (
                    f"🔔 <b>تراکنش جهت تایید</b> — سفارش #{oid}\n"
                    f"رسید دریافت شد؛ لطفاً تایید یا رد کنید."
                ),
                reply_markup=kb,
            )
        except Exception:
            log.exception("admin pending-tx ping failed order=%s", oid)
    try:
        await message.delete()
    except Exception:
        pass
    if oid:
        sent_recv = await message.answer(received, reply_markup=customer_menu())
        _track_order_ui_message(oid, sent_recv.message_id if sent_recv else None)
    else:
        await message.answer(
            "✅ فایل دریافت شد و برای پشتیبانی ارسال شد.",
            reply_markup=customer_menu(),
        )


# ─── Text flows ───────────────────────────────────────────────────────────────


@router.message(F.text)
async def text_flow(message: Message, settings: Settings) -> None:
    _track(message)
    text = (message.text or "").strip()
    menu_btns = {
        BTN_START,
        BTN_BUY,
        BTN_MINE,
        BTN_ORDERS,
        BTN_WALLET,
        BTN_PRICES,
        BTN_GUIDE,
        BTN_SUPPORT,
        "شروع",
        "🛍 سرویس‌های من",
        "🏦 کیف پول + شارژ",
        "💰 تعرفه‌ها",
        "🆘 پشتیبانی",
    }
    if not text or text.startswith("/") or text in menu_btns:
        return
    uid = message.from_user.id if message.from_user else 0
    flow = _get_flow(uid)
    draft = _get_draft(uid)

    if draft.get("step") == "svc_search":
        _set_draft(uid, {"step": "service"})
        await _render_services_page(message, uid=uid, page=0, edit=False, query=text)
        return

    if flow.startswith("wait_receipt:"):
        await message.answer(
            "برای این سفارش لطفاً <b>عکس یا فایل رسید</b> بفرستید، "
            "یا از دکمه لغو/سفارش‌ها استفاده کنید.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📋 سفارش‌ها",
                            callback_data="cust:orders:goto",
                        )
                    ]
                ]
            ),
        )
        return

    if flow.startswith("ticket:"):
        ticket_id = int(flow.split(":", 1)[1])
        with get_session() as session:
            ticket = repo.get_support_ticket(session, ticket_id)
            if not ticket or ticket.telegram_id != uid:
                _set_flow(uid, "")
                await message.answer("گفتگو یافت نشد.", reply_markup=customer_menu())
                return
            if ticket.status == "closed":
                _set_flow(uid, "")
                await message.answer(
                    "این گفتگو بسته شده است. از پشتیبانی پیام جدید بفرستید.",
                    reply_markup=customer_menu(),
                )
                return
            repo.add_support_message(
                session, ticket_id, sender="customer", body=text
            )
            order_ref = ticket.order_id
        uname = message.from_user.username if message.from_user else ""
        caption = (
            f"🔔 <b>درخواست جدید — پاسخ مشتری در تیکت</b>\n"
            f"تیکت #{ticket_id}\n"
            f"سفارش: #{order_ref or '—'}\n"
            f"از: <code>{uid}</code> @{uname}\n"
            f"نیاز به واکنش ادمین\n\n"
            f"{text}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="↩️ پاسخ در تیکت",
                        callback_data=f"ticket:reply:{ticket_id}",
                    )
                ]
            ]
        )
        await customer_bridge.notify_admins_action_required(
            settings.admin_ids, caption, reply_markup=kb
        )
        await message.answer(
            "ارسال شد ✅ می‌توانید ادامه دهید یا از منو خارج شوید.",
            reply_markup=_back_kb(),
        )
        return

    if flow == "support_subject":
        subject = (text or "").strip()
        if not subject:
            await message.answer(
                _msg("support_ask_subject", max=SUPPORT_SUBJECT_MAX),
                reply_markup=customer_menu(),
            )
            return
        if len(subject) > SUPPORT_SUBJECT_MAX:
            await message.answer(
                _msg(
                    "support_subject_too_long",
                    max=SUPPORT_SUBJECT_MAX,
                    n=len(subject),
                ),
                reply_markup=customer_menu(),
            )
            return
        _set_draft(uid, {"support_subject": subject})
        _set_flow(uid, "support_body")
        await message.answer(
            _msg("support_ask_body", subject=subject),
            reply_markup=customer_menu(),
        )
        return

    if flow == "support_body":
        draft = _get_draft(uid)
        subject = str(draft.get("support_subject") or "").strip() or "پشتیبانی"
        body = (text or "").strip()
        if not body:
            await message.answer(
                _msg("support_ask_body", subject=subject),
                reply_markup=customer_menu(),
            )
            return
        with get_session() as session:
            ticket = repo.create_support_ticket(
                session,
                telegram_id=uid,
                kind="support",
                subject=subject[:SUPPORT_SUBJECT_MAX],
                status="awaiting_admin",
            )
            repo.add_support_message(
                session, ticket.id, sender="customer", body=body
            )
            ticket_id = ticket.id
        _set_draft(uid, {})
        _set_flow(uid, f"ticket:{ticket_id}")
        uname = message.from_user.username if message.from_user else ""
        caption = (
            f"🔔 <b>درخواست جدید — تیکت پشتیبانی</b>\n"
            f"تیکت #{ticket_id}\n"
            f"📌 موضوع: {subject}\n"
            f"از: <code>{uid}</code> @{uname}\n"
            f"نام: {(message.from_user.full_name if message.from_user else '')}\n"
            f"نیاز به واکنش ادمین\n\n"
            f"{body}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="↩️ پاسخ در تیکت",
                        callback_data=f"ticket:reply:{ticket_id}",
                    )
                ]
            ]
        )
        await customer_bridge.notify_admins_action_required(
            settings.admin_ids, caption, reply_markup=kb
        )
        await message.answer(
            f"ارسال شد ✅ تیکت #{ticket_id}\n"
            "منتظر پاسخ پشتیبانی باشید.\n"
            "می‌توانید همین‌جا ادامه پیام بفرستید یا از «تیکت‌های من» ببینید.",
            reply_markup=_support_hub_inline(),
        )
        return

    # Legacy single-step support flow → redirect to subject prompt
    if flow == "support":
        _set_flow(uid, "support_subject")
        await message.answer(
            _msg("support_ask_subject", max=SUPPORT_SUBJECT_MAX),
            reply_markup=customer_menu(),
        )
        return

    if draft.get("step") == "wallet_amount":
        amount = _parse_amount_toman(text)
        if amount is None:
            await message.answer(_msg("wallet_amount_invalid"), reply_markup=_back_kb())
            return
        await _start_wallet_charge(message, uid, amount, settings)
        return

    if draft.get("step") == "transfer_wait_tg":
        username = str(draft.get("username") or "")
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < 5:
            await message.answer(
                "شناسه تلگرام معتبر نیست. فقط عدد بفرستید یا انصراف بزنید.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🚫 انصراف",
                                callback_data="cust:xfer:no",
                            )
                        ]
                    ]
                ),
            )
            return
        to_tg = int(digits)
        if to_tg == uid:
            await message.answer("نمی‌توانید به خودتان منتقل کنید.")
            return
        with get_session() as session:
            profile = repo.get_customer_profile(session, to_tg)
        if not profile:
            await message.answer(
                f"پروفایل <code>{to_tg}</code> یافت نشد.\n"
                "گیرنده باید اول ربات را استارت کند.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🚫 انصراف",
                                callback_data="cust:xfer:no",
                            )
                        ]
                    ]
                ),
            )
            return
        if not username or not _owns_service(uid, username):
            _set_draft(uid, {})
            await message.answer("این سرویس مال شما نیست.", reply_markup=customer_menu())
            return
        draft.update({"step": "transfer_confirm", "to_tg": to_tg})
        _set_draft(uid, draft)
        await message.answer(
            f"تایید انتقال <code>{username}</code> → <code>{to_tg}</code>؟\n"
            "بعد از تایید، فوری انجام می‌شود.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ تایید انتقال",
                            callback_data="cust:xfer:ok",
                        ),
                        InlineKeyboardButton(
                            text="🚫 انصراف",
                            callback_data="cust:xfer:no",
                        ),
                    ]
                ]
            ),
        )
        return

    if draft.get("step") == "username":
        username = _valid_username(text)
        if not username:
            await message.answer(
                _msg("username_invalid", help=USERNAME_HELP),
                reply_markup=_back_kb(),
            )
            return
        with get_session() as session:
            ok, _reason = repo.is_username_available_for_create(session, username, uid)
        if not ok:
            await message.answer(
                "❌ این نام کاربری در دسترس نیست. نام دیگری انتخاب کنید.",
                reply_markup=_back_kb(),
            )
            return
        draft["username"] = username
        await _show_invoice(message, uid, draft)
        return

    await message.answer(
        "از منوی پایین یک گزینه انتخاب کنید، یا /start را بزنید.",
        reply_markup=customer_menu(),
    )


@router.callback_query(F.data == "cust:orders:goto")
async def orders_goto(callback: CallbackQuery) -> None:
    await my_orders(callback.message)
    await callback.answer()
