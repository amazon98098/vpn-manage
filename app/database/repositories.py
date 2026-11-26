"""Persistence helpers used by the listener and admin bot."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.utils.text import local_product_name_from_plan, local_product_name_from_vendor_text

from .models import (
    AppState,
    BotMessage,
    CustomerOrder,
    CustomerProfile,
    CustomerServiceLink,
    EducationCategory,
    EducationLesson,
    IncomingMessage,
    PricePlan,
    RenewJob,
    Subscription,
    SupportMessage,
    SupportTicket,
    USABLE_SUBSCRIPTION_STATUSES,
    Vendor,
)

# Sell catalog: NetPlus monthly + FineArts time-unlimited volume (50 / 100 GB)
# Tuple: key, title, duration_days (0 = unlimited), traffic_gb, price_toman, vendor_cost_toman
DEFAULT_PRICE_PLANS = (
    ("1m_25gb", "۲۵ گیگ یکماهه", 31, 25.0, 150000, 0),
    ("1m_55gb", "۵۵ گیگ یکماهه", 31, 55.0, 180000, 0),
    ("1m_100gb", "۱۰۰ گیگ یکماهه", 31, 100.0, 250000, 0),
    ("fa_50gb", "۵۰ گیگ نامحدود زمانی", 0, 50.0, 460000, 360000),
    ("fa_100gb", "۱۰۰ گیگ نامحدود زمانی", 0, 100.0, 820000, 696000),
)

ACTIVE_RENEW_STATUSES = (
    "queued",
    "driving",
    "awaiting_package",
    "awaiting_payment",
    "awaiting_auto_approve",
    "receipt_window",
    "awaiting_receipt_confirm",
)

JOB_STATUS_FA = {
    "queued": "در صف",
    "driving": "در حال اجرای فروشنده",
    "awaiting_package": "منتظر انتخاب بسته",
    "awaiting_payment": "منتظر پرداخت",
    "awaiting_auto_approve": "منتظر تایید خودکار",
    "receipt_window": "پنجره رسید",
    "awaiting_receipt_confirm": "منتظر تایید رسید",
    "completed": "موفق",
    "failed": "ناموفق",
    "cancelled": "لغو شده",
}

KIND_FA = {"renew": "تمدید", "create": "خرید جدید"}


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


def _as_datetime(value: Any) -> datetime | None:
    """Parse expire instant (prefer full datetime; date-only → midnight UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and value > 0:
        return datetime.utcfromtimestamp(int(value))
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            if "T" in raw or " " in raw:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
            d = date.fromisoformat(raw[:10])
            return datetime(d.year, d.month, d.day)
        except ValueError:
            return None
    return None



# Default provider key until another adapter is registered (Phase 9).
DEFAULT_VENDOR_KEY = "netplus"


def ensure_vendor(session: Session, key: str, name: str, username: str) -> Vendor:
    vendor = session.scalar(select(Vendor).where(Vendor.key == key))
    if vendor:
        vendor.telegram_username = username.lstrip("@")
        vendor.name = name
        # FineArts: use Clash headers on subscription URL (not HTML page totals / not bot walk).
        if key == "finearts" and (
            not vendor.info_refresh_strategy
            or vendor.info_refresh_strategy
            in ("automatic", "provider_bot")
        ):
            vendor.info_refresh_strategy = "subscription_url"
        return vendor
    strategy = "subscription_url" if key == "finearts" else "automatic"
    vendor = Vendor(
        key=key,
        name=name,
        telegram_username=username.lstrip("@"),
        info_refresh_strategy=strategy,
    )
    session.add(vendor)
    session.flush()
    return vendor


def get_vendor(session: Session, vendor_id: int) -> Vendor | None:
    return session.get(Vendor, vendor_id)


def get_vendor_by_key(session: Session, key: str) -> Vendor | None:
    return session.scalar(select(Vendor).where(Vendor.key == key))


VENDOR_INFO_REFRESH_STRATEGIES = frozenset(
    {"automatic", "subscription_url", "provider_bot"}
)


def list_vendors(session: Session) -> list[Vendor]:
    return list(session.scalars(select(Vendor).order_by(Vendor.id.asc())))


def set_vendor_info_refresh_strategy(
    session: Session, vendor_key: str, strategy: str
) -> Vendor | None:
    """Set how inventory traffic/expiry is refreshed for one vendor."""
    key = (vendor_key or "").strip().lower()
    val = (strategy or "").strip().lower()
    if val not in VENDOR_INFO_REFRESH_STRATEGIES:
        raise ValueError(f"invalid_strategy:{val}")
    vendor = get_vendor_by_key(session, key)
    if not vendor:
        return None
    vendor.info_refresh_strategy = val
    session.flush()
    return vendor


def resolve_vendor_for_username(session: Session, username: str) -> Vendor | None:
    """Vendor owning this inventory username, else default NetPlus row if present."""
    sub = get_subscription_by_username(session, username)
    if sub and sub.vendor_id:
        return session.get(Vendor, sub.vendor_id)
    return get_vendor_by_key(session, DEFAULT_VENDOR_KEY)


def resolve_vendor_id_for_job(
    session: Session,
    *,
    username: str,
    subscription_id: int | None = None,
    vendor_id: int | None = None,
) -> int | None:
    """Pick vendor_id for a renew/create job.

    Existing subscription vendor always wins (never let a FineArts plan
    override a NetPlus service, or the reverse).
    """
    if subscription_id is not None:
        sub = session.get(Subscription, subscription_id)
        if sub and sub.vendor_id:
            return sub.vendor_id
    by_name = get_subscription_by_username(session, username)
    if by_name and by_name.vendor_id:
        return by_name.vendor_id
    if vendor_id is not None:
        return vendor_id
    vendor = resolve_vendor_for_username(session, username)
    return vendor.id if vendor else None


def set_vendor_chat_id(session: Session, vendor: Vendor, chat_id: int) -> None:
    vendor.telegram_chat_id = chat_id


def message_exists(session: Session, vendor_id: int, telegram_message_id: int) -> bool:
    return (
        session.scalar(
            select(IncomingMessage.id).where(
                IncomingMessage.vendor_id == vendor_id,
                IncomingMessage.telegram_message_id == telegram_message_id,
            )
        )
        is not None
    )


def save_incoming(
    session: Session,
    *,
    vendor: Vendor,
    telegram_message_id: int,
    text: str,
    kind: str,
    parsed: dict[str, Any] | None,
    received_at: datetime,
) -> IncomingMessage | None:
    if message_exists(session, vendor.id, telegram_message_id):
        return None
    row = IncomingMessage(
        vendor_id=vendor.id,
        telegram_message_id=telegram_message_id,
        message_text=text,
        kind=kind,
        parsed_json=json.dumps(parsed, ensure_ascii=False) if parsed else None,
        processing_status="parsed" if kind != "unknown" else "ignored",
        received_at=received_at,
    )
    session.add(row)
    session.flush()
    return row


def apply_status_snapshot(
    session: Session, vendor_id: int, data: dict[str, Any]
) -> Subscription | None:
    """Update subscription fields from a vendor status refresh dict."""
    username = (data.get("username") or "").strip()
    if not username:
        return None
    sub = session.scalar(
        select(Subscription).where(
            Subscription.vendor_id == vendor_id,
            Subscription.username == username,
        )
    )
    if not sub:
        sub = Subscription(
            vendor_id=vendor_id,
            username=username,
            created_at=datetime.utcnow(),
        )
        session.add(sub)
    if data.get("vendor_service_id"):
        sub.vendor_service_id = str(data["vendor_service_id"])
    local_product = None
    if data.get("product_name"):
        local_product = local_product_name_from_vendor_text(str(data["product_name"]))
    if local_product:
        sub.product_name = local_product
    kind = data.get("kind")
    if data.get("status"):
        sub.status = str(data["status"])
    elif kind == "status_expired":
        sub.status = "expired"
    elif kind == "status_volume_exhausted":
        sub.status = "volume_exhausted"
    elif kind == "status_disabled":
        sub.status = "disabled"
    elif kind == "status_active":
        sub.status = "active"
    for src, dst in (
        ("traffic_limit_mb", "traffic_limit_mb"),
        ("used_traffic_mb", "used_traffic_mb"),
        ("remaining_traffic_mb", "remaining_traffic_mb"),
    ):
        if data.get(src) is not None:
            try:
                setattr(sub, dst, float(data[src]))
            except (TypeError, ValueError):
                pass
    exp = _as_datetime(data.get("expiration_date"))
    if exp is not None:
        sub.expiration_date = exp
    if data.get("subscription_url"):
        sub.subscription_url = str(data["subscription_url"])
    if data.get("last_refresh_method"):
        sub.last_refresh_method = str(data["last_refresh_method"])[:32]
    session.flush()
    return sub


def upsert_subscription_from_alert(
    session: Session,
    vendor: Vendor,
    parsed: dict[str, Any],
    received_at: datetime,
) -> Subscription | None:
    username = parsed.get("username")
    if not username:
        return None
    sub = session.scalar(
        select(Subscription).where(
            Subscription.vendor_id == vendor.id,
            Subscription.username == username,
        )
    )
    if not sub:
        sub = Subscription(
            vendor_id=vendor.id,
            username=username,
            created_at=datetime.utcnow(),
        )
        session.add(sub)

    if parsed.get("vendor_service_id"):
        sub.vendor_service_id = parsed["vendor_service_id"]
    if parsed.get("remaining_traffic_mb") is not None:
        sub.remaining_traffic_mb = parsed["remaining_traffic_mb"]
    if parsed.get("used_traffic_mb") is not None:
        sub.used_traffic_mb = parsed["used_traffic_mb"]
    if parsed.get("traffic_limit_mb") is not None:
        sub.traffic_limit_mb = parsed["traffic_limit_mb"]
    exp = _as_datetime(parsed.get("expiration_date"))
    if exp is not None:
        sub.expiration_date = exp
    if parsed.get("subscription_url"):
        sub.subscription_url = str(parsed["subscription_url"])
    local_product = None
    if parsed.get("product_name"):
        local_product = local_product_name_from_vendor_text(str(parsed["product_name"]))
    if local_product:
        sub.product_name = local_product

    kind = parsed.get("kind")
    if kind == "alert_deleted":
        # Phase 6: hard-remove is handled by remove_subscription_after_provider_delete.
        # Keep a soft flag only if the row is still present mid-transaction.
        sub.status = "removed_by_vendor"
        sub.vendor_alert_at = None
    elif kind == "status_expired":
        sub.status = "expired"
    elif kind == "status_volume_exhausted":
        sub.status = "volume_exhausted"
    elif kind == "status_disabled":
        sub.status = "disabled"
    elif kind in {"alert_days", "alert_mb"}:
        # Real vendor bot alert — this alone drives the vendor-warnings page.
        sub.status = "warning"
        sub.vendor_alert_at = received_at or datetime.utcnow()
    elif kind in {"renew_ok", "create_ok", "status_active"}:
        sub.status = "active"
        if kind in {"renew_ok", "create_ok"}:
            sub.vendor_alert_at = None
            sub.last_renewed_at = received_at or datetime.utcnow()

    session.flush()
    return sub


def mark_notified(session: Session, message: IncomingMessage) -> None:
    message.notified = True
    message.processing_status = "notified"


_ALMOST_OUT_HIDDEN_KEY = "almost_out_hidden_ids"
_VENDOR_WARNING_HIDDEN_KEY = "vendor_warning_hidden_ids"


def _hidden_id_set(session: Session, key: str) -> set[int]:
    raw = (get_state(session, key) or "").strip()
    if not raw:
        return set()
    try:
        data = json.loads(raw)
        return {int(x) for x in data}
    except (json.JSONDecodeError, TypeError, ValueError):
        return set()


def _hide_id(session: Session, key: str, subscription_id: int) -> None:
    ids = _hidden_id_set(session, key)
    ids.add(int(subscription_id))
    set_state(session, key, json.dumps(sorted(ids)))


def get_almost_out_hidden_ids(session: Session) -> set[int]:
    return _hidden_id_set(session, _ALMOST_OUT_HIDDEN_KEY)


def hide_almost_out_subscription(session: Session, subscription_id: int) -> None:
    _hide_id(session, _ALMOST_OUT_HIDDEN_KEY, subscription_id)


def clear_almost_out_hidden(session: Session) -> None:
    set_state(session, _ALMOST_OUT_HIDDEN_KEY, "[]")


def get_vendor_warning_hidden_ids(session: Session) -> set[int]:
    return _hidden_id_set(session, _VENDOR_WARNING_HIDDEN_KEY)


def hide_vendor_warning_subscription(session: Session, subscription_id: int) -> None:
    _hide_id(session, _VENDOR_WARNING_HIDDEN_KEY, subscription_id)


def clear_vendor_warning_hidden(session: Session) -> None:
    set_state(session, _VENDOR_WARNING_HIDDEN_KEY, "[]")


def _as_naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


# After renew/create, keep service off warning/almost-out lists for this window.
RECENT_RENEW_GRACE_DAYS = 3


def mark_subscription_renewed(
    session: Session,
    sub: Subscription | None,
    *,
    when: datetime | None = None,
) -> None:
    """Stamp last_renewed_at and clear vendor-warning marker (all renew sources)."""
    if sub is None:
        return
    ts = _as_naive_utc(when) or datetime.utcnow()
    prev = _as_naive_utc(getattr(sub, "last_renewed_at", None))
    if prev is None or ts >= prev:
        sub.last_renewed_at = ts
    sub.vendor_alert_at = None
    if (sub.status or "") == "warning":
        sub.status = "active"


def subscription_renewed_recently(
    sub: Subscription, *, days: int = RECENT_RENEW_GRACE_DAYS
) -> bool:
    """True when last_renewed_at is within the grace window."""
    ts = _as_naive_utc(getattr(sub, "last_renewed_at", None))
    if ts is None:
        return False
    return ts >= datetime.utcnow() - timedelta(days=max(0, int(days)))


def sync_last_renewed_at_from_jobs(session: Session) -> int:
    """Fast backfill/update of last_renewed_at from completed renew/create jobs.

    Covers admin-bot, panel, and customer-bot queue completions in one pass.
    """
    jobs = list(
        session.scalars(
            select(RenewJob).where(
                RenewJob.status == "completed",
                RenewJob.kind.in_(("renew", "create")),
            )
        )
    )
    latest: dict[str, datetime] = {}
    for job in jobs:
        uname = (job.username or "").strip()
        if not uname:
            continue
        done = _as_naive_utc(job.completed_at) or _as_naive_utc(job.updated_at)
        if done is None:
            continue
        prev = latest.get(uname)
        if prev is None or done > prev:
            latest[uname] = done
    updated = 0
    for uname, ts in latest.items():
        sub = get_subscription_by_username(session, uname)
        if sub is None:
            continue
        prev = _as_naive_utc(getattr(sub, "last_renewed_at", None))
        if prev is None or ts > prev:
            sub.last_renewed_at = ts
            updated += 1
    if updated:
        session.flush()
    return updated


def vendor_alert_superseded_by_renew(
    session: Session, sub: Subscription
) -> bool:
    """True if renew/create succeeded after the stored vendor_alert_at."""
    alert_at = _as_naive_utc(getattr(sub, "vendor_alert_at", None))
    if alert_at is None or not (sub.username or "").strip():
        return False

    # Fast path: stamped renew from admin bot / panel / customer queue.
    renewed = _as_naive_utc(getattr(sub, "last_renewed_at", None))
    if renewed is not None and renewed > alert_at:
        return True
    if subscription_renewed_recently(sub):
        return True

    uname = sub.username.strip()

    # Completed renew/create job after the alert.
    job = session.scalar(
        select(RenewJob)
        .where(
            RenewJob.username == uname,
            RenewJob.status == "completed",
            RenewJob.kind.in_(("renew", "create")),
        )
        .order_by(RenewJob.completed_at.desc(), RenewJob.id.desc())
        .limit(1)
    )
    if job is not None:
        done = _as_naive_utc(job.completed_at) or _as_naive_utc(job.updated_at)
        if done is not None and done > alert_at:
            mark_subscription_renewed(session, sub, when=done)
            return True

    # Vendor success message after the alert (covers renews outside our queue).
    msgs = list(
        session.scalars(
            select(IncomingMessage)
            .where(
                IncomingMessage.kind.in_(("renew_ok", "create_ok")),
                IncomingMessage.parsed_json.contains(uname),
            )
            .order_by(IncomingMessage.received_at.desc())
            .limit(20)
        )
    )
    for msg in msgs:
        try:
            data = json.loads(msg.parsed_json or "") if msg.parsed_json else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        if str((data or {}).get("username") or "").strip() != uname:
            continue
        received = _as_naive_utc(msg.received_at)
        if received is not None and received > alert_at:
            mark_subscription_renewed(session, sub, when=received)
            return True
    return False


def clear_vendor_alerts_superseded_by_renew(session: Session) -> int:
    """Remove vendor-warning marker when a later renew/create already succeeded."""
    sync_last_renewed_at_from_jobs(session)
    rows = list(
        session.scalars(
            select(Subscription).where(Subscription.vendor_alert_at.is_not(None))
        )
    )
    cleared = 0
    for sub in rows:
        if not vendor_alert_superseded_by_renew(session, sub):
            continue
        sub.vendor_alert_at = None
        if (sub.status or "") == "warning":
            sub.status = "active"
        cleared += 1
    if cleared:
        session.flush()
    return cleared


def clear_recently_renewed_from_alert_lists(session: Session) -> dict[str, int]:
    """Fast pass: sync renew stamps + drop superseded vendor-warnings.

    Almost-out exclusion is applied live in list_local_almost_out via last_renewed_at.
    """
    stamped = sync_last_renewed_at_from_jobs(session)
    warn_cleared = clear_vendor_alerts_superseded_by_renew(session)
    grace = datetime.utcnow() - timedelta(days=RECENT_RENEW_GRACE_DAYS)
    recent_n = int(
        session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.last_renewed_at.is_not(None),
                Subscription.last_renewed_at >= grace,
            )
        )
        or 0
    )
    return {
        "stamped": int(stamped),
        "vendor_cleared": int(warn_cleared),
        "recent_renewed": recent_n,
        "grace_days": int(RECENT_RENEW_GRACE_DAYS),
    }


def list_warning_subscriptions(
    session: Session, *, offset: int = 0, limit: int = 30, include_hidden: bool = False
) -> tuple[list[Subscription], int]:
    """Vendor-warnings page: only rows that received a real vendor alert_days/alert_mb.

    Local almost-out / userinfo checks must not put rows here (they use vendor_alert_at).
    Rows renewed/created after the alert are cleared automatically.
    Returns (page_rows, total_count).
    """
    clear_vendor_alerts_superseded_by_renew(session)
    far = datetime(2099, 1, 1)
    all_rows = list(
        session.scalars(
            select(Subscription)
            .where(
                Subscription.vendor_alert_at.is_not(None),
                Subscription.status.notin_(("removed_by_vendor", "deleted")),
            )
            .order_by(
                Subscription.vendor_alert_at.desc(),
                func.coalesce(Subscription.expiration_date, far).asc(),
                func.coalesce(Subscription.remaining_traffic_mb, 1e12).asc(),
                Subscription.id.desc(),
            )
        )
    )
    hidden = set() if include_hidden else get_vendor_warning_hidden_ids(session)
    rows = [s for s in all_rows if int(s.id) not in hidden]
    total = len(rows)
    start = max(0, offset)
    end = start + max(1, limit)
    return rows[start:end], total


def hide_all_vendor_warnings_visible(session: Session) -> int:
    rows, _total = list_warning_subscriptions(
        session, offset=0, limit=10000, include_hidden=False
    )
    hidden = get_vendor_warning_hidden_ids(session)
    before = len(hidden)
    for sub in rows:
        hidden.add(int(sub.id))
    set_state(session, _VENDOR_WARNING_HIDDEN_KEY, json.dumps(sorted(hidden)))
    return len(hidden) - before


def clear_subscription_from_alert_lists_after_renew(
    session: Session,
    *,
    username: str | None = None,
    subscription: Subscription | None = None,
    plan_id: int | None = None,
) -> Subscription | None:
    """After successful renew/create: leave vendor-warnings + almost-out lists.

    - Force status active (drops vendor-warning list).
    - If still under almost-out thresholds, apply one optimistic plan bump so the
      row leaves the local almost-out list until the next real low-traffic/days hit.
    """
    from datetime import timedelta

    from app.utils.text import expire_within_days, is_expire_passed

    sub = subscription
    if sub is None and username:
        sub = get_subscription_by_username(session, username)
    if sub is None:
        return None

    mark_subscription_renewed(session, sub, when=datetime.utcnow())
    sub.status = "active"

    days_th = get_alert_days_threshold(session)
    traffic_th = get_alert_traffic_mb_threshold(session)
    still_days = is_expire_passed(sub.expiration_date) or expire_within_days(
        sub.expiration_date, days_th
    )
    still_traf = sub.remaining_traffic_mb is not None and (
        sub.remaining_traffic_mb <= 0 or sub.remaining_traffic_mb <= traffic_th
    )

    plan = get_price_plan(session, plan_id) if plan_id else None
    if plan is None and sub.product_name:
        # best-effort: no plan id — still clear status; userinfo should have run
        pass

    if plan and still_traf and plan.traffic_gb is not None:
        add_mb = float(plan.traffic_gb) * 1024.0
        rem = float(sub.remaining_traffic_mb or 0.0)
        lim = float(sub.traffic_limit_mb or 0.0)
        sub.remaining_traffic_mb = rem + add_mb
        sub.traffic_limit_mb = max(lim, rem) + add_mb
        if sub.used_traffic_mb is not None and sub.traffic_limit_mb is not None:
            # keep used coherent if present
            pass

    if plan and still_days and plan.duration_days:
        base = sub.expiration_date or datetime.utcnow()
        if base.tzinfo is not None:
            base = base.replace(tzinfo=None)
        if base < datetime.utcnow():
            base = datetime.utcnow()
        sub.expiration_date = base + timedelta(days=int(plan.duration_days))

    # If still matching almost-out with no plan to bump, drop from hidden? No —
    # force-clear by ensuring remaining above threshold when we renewed without plan.
    if still_traf and (plan is None or plan.traffic_gb is None):
        # Leave list by setting remaining just above threshold when unknown plan size
        floor = float(traffic_th) + 1.0
        if sub.remaining_traffic_mb is None or sub.remaining_traffic_mb <= traffic_th:
            sub.remaining_traffic_mb = max(float(sub.remaining_traffic_mb or 0.0), floor)

    session.flush()
    return sub


def list_local_almost_out(
    session: Session,
    *,
    offset: int = 0,
    limit: int = 40,
    include_hidden: bool = False,
) -> tuple[list[tuple[Subscription, frozenset[str]]], int]:
    """Local almost-out from expiration_date/traffic vs configured thresholds.

    Independent of the vendor-warnings page: a row may appear on both, one, or neither.
    Includes usable statuses (even if also vendor-warned).
    Recently renewed services (last_renewed_at within grace days) are excluded.
    Reasons subset of {'days','traffic','expired'}.
    Returns (page_items, total_matching).
    """
    from app.utils.text import expire_within_days, is_expire_passed

    days_th = get_alert_days_threshold(session)
    traffic_th = get_alert_traffic_mb_threshold(session)
    cutoff = datetime.utcnow() + timedelta(days=int(days_th))
    far = datetime(2099, 1, 1)
    grace_cut = datetime.utcnow() - timedelta(days=RECENT_RENEW_GRACE_DAYS)
    rows = list(
        session.scalars(
            select(Subscription)
            .where(
                Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)),
                or_(
                    Subscription.last_renewed_at.is_(None),
                    Subscription.last_renewed_at < grace_cut,
                ),
                or_(
                    and_(
                        Subscription.expiration_date.is_not(None),
                        Subscription.expiration_date <= cutoff,
                    ),
                    and_(
                        Subscription.remaining_traffic_mb.is_not(None),
                        Subscription.remaining_traffic_mb <= traffic_th,
                    ),
                ),
            )
            .order_by(
                func.coalesce(Subscription.expiration_date, far).asc(),
                func.coalesce(Subscription.remaining_traffic_mb, 1e12).asc(),
                Subscription.id.desc(),
            )
        )
    )
    hidden = set() if include_hidden else get_almost_out_hidden_ids(session)
    out: list[tuple[Subscription, frozenset[str]]] = []
    for sub in rows:
        if int(sub.id) in hidden:
            continue
        if subscription_renewed_recently(sub):
            continue
        reasons: set[str] = set()
        if is_expire_passed(sub.expiration_date):
            reasons.add("expired")
        elif expire_within_days(sub.expiration_date, days_th):
            reasons.add("days")
        if sub.remaining_traffic_mb is not None and sub.remaining_traffic_mb <= 0:
            reasons.add("traffic")
        elif (
            sub.remaining_traffic_mb is not None
            and sub.remaining_traffic_mb <= traffic_th
        ):
            reasons.add("traffic")
        if reasons:
            out.append((sub, frozenset(reasons)))
    total = len(out)
    start = max(0, offset)
    end = start + max(1, limit)
    return out[start:end], total


def hide_all_almost_out_visible(session: Session) -> int:
    """Hide every currently visible almost-out row (admin dismiss only)."""
    items, _total = list_local_almost_out(
        session, offset=0, limit=10000, include_hidden=False
    )
    hidden = get_almost_out_hidden_ids(session)
    before = len(hidden)
    for sub, _reasons in items:
        hidden.add(int(sub.id))
    set_state(session, _ALMOST_OUT_HIDDEN_KEY, json.dumps(sorted(hidden)))
    return len(hidden) - before


def list_owned_usable_subscriptions(
    session: Session, *, limit: int | None = None
) -> list[tuple[CustomerServiceLink, Subscription]]:
    """Local ownership links joined to usable inventory rows."""
    q = (
        select(CustomerServiceLink, Subscription)
        .join(
            Subscription,
            CustomerServiceLink.subscription_id == Subscription.id,
        )
        .where(Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)))
        .order_by(Subscription.id.asc())
    )
    if limit is not None:
        q = q.limit(limit)
    return list(session.execute(q).all())


def remove_subscription_after_provider_delete(
    session: Session, username: str
) -> dict[str, Any] | None:
    """Unlink owner, null renew FKs, delete inventory row. Returns owner info if any."""
    uname = (username or "").strip()
    if not uname:
        return None
    sub = session.scalar(
        select(Subscription)
        .where(Subscription.username == uname)
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    link = get_link_for_username(session, uname)
    if link is None and sub is not None:
        link = get_link_for_subscription(session, sub.id)
    owner_tg = link.telegram_id if link else None
    product = sub.product_name if sub else None

    # Delete all links for this username / subscription id.
    if sub is not None:
        for row in list(
            session.scalars(
                select(CustomerServiceLink).where(
                    CustomerServiceLink.subscription_id == sub.id
                )
            )
        ):
            session.delete(row)
        session.execute(
            update(RenewJob)
            .where(RenewJob.subscription_id == sub.id)
            .values(subscription_id=None)
        )
    for row in list(
        session.scalars(
            select(CustomerServiceLink).where(
                CustomerServiceLink.service_username == uname
            )
        )
    ):
        session.delete(row)
    session.flush()

    if sub is not None:
        session.delete(sub)
        session.flush()

    if owner_tg is None:
        return None
    return {
        "telegram_id": int(owner_tg),
        "username": uname,
        "product": product or "—",
    }


def search_subscriptions(session: Session, query: str, limit: int = 20) -> list[Subscription]:
    """Fuzzy discovery only — do not use first hit for ops (exact helpers below)."""
    q = f"%{query.strip()}%"
    return list(
        session.scalars(
            select(Subscription)
            .where(Subscription.username.ilike(q))
            .order_by(
                Subscription.created_at.desc(),
                Subscription.id.desc(),
            )
            .limit(limit)
        )
    )


def list_inventory_subscriptions(
    session: Session,
    *,
    query: str | None = None,
    usable_only: bool = True,
    offset: int = 0,
    limit: int = 10,
) -> tuple[list[Subscription], int]:
    """Paginated inventory for admin services list. Newest created_at first."""
    q = select(Subscription)
    count_q = select(func.count()).select_from(Subscription)
    if usable_only:
        usable = tuple(USABLE_SUBSCRIPTION_STATUSES)
        q = q.where(Subscription.status.in_(usable))
        count_q = count_q.where(Subscription.status.in_(usable))
    needle = (query or "").strip()
    if needle:
        like = f"%{needle}%"
        q = q.where(Subscription.username.ilike(like))
        count_q = count_q.where(Subscription.username.ilike(like))
    total = int(session.scalar(count_q) or 0)
    rows = list(
        session.scalars(
            q.order_by(
                Subscription.created_at.desc(),
                Subscription.id.desc(),
            )
            .offset(max(0, offset))
            .limit(max(1, limit))
        )
    )
    return rows, total


def count_by_status(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Subscription.status, Subscription.id).select_from(Subscription)
    ).all()
    out: dict[str, int] = {}
    for status, _ in rows:
        out[status] = out.get(status, 0) + 1
    return out


def track_admin_order_ui_message(
    session: Session,
    order_id: int,
    chat_id: int,
    message_id: int,
    *,
    delete_on_done: bool = True,
) -> None:
    """Remember admin-bot review messages so approve/reject can clear them.

    delete_on_done=True → delete the message after confirm/reject (receipt/ping).
    delete_on_done=False → only strip buttons (shared requests list pages).
    """
    key = f"admin_order_ui:{int(order_id)}"
    raw = (get_state(session, key) or "").strip()
    rows: list[list[int]] = []
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for item in data:
                    if (
                        isinstance(item, (list, tuple))
                        and len(item) >= 2
                    ):
                        flag = int(item[2]) if len(item) >= 3 else 1
                        rows.append([int(item[0]), int(item[1]), flag])
        except (json.JSONDecodeError, TypeError, ValueError):
            rows = []
    pair = [int(chat_id), int(message_id), 1 if delete_on_done else 0]
    # Replace same chat/mid entry so the latest flag wins.
    rows = [r for r in rows if not (r[0] == pair[0] and r[1] == pair[1])]
    rows.append(pair)
    set_state(session, key, json.dumps(rows[-40:]))


def pop_admin_order_ui_messages(
    session: Session, order_id: int
) -> list[tuple[int, int, bool]]:
    """Pop and clear tracked admin review messages for an order.

    Returns (chat_id, message_id, delete_on_done).
    """
    key = f"admin_order_ui:{int(order_id)}"
    raw = (get_state(session, key) or "").strip()
    set_state(session, key, "")
    if not raw:
        return []
    out: list[tuple[int, int, bool]] = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    flag = True if len(item) < 3 else bool(int(item[2]))
                    out.append((int(item[0]), int(item[1]), flag))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return out


def track_renew_pay_admin_message(
    session: Session, job_id: int, chat_id: int, message_id: int
) -> None:
    """Remember admin payment (card/amount) messages for a renew/create job."""
    key = f"renew_pay_ui:{int(job_id)}"
    raw = (get_state(session, key) or "").strip()
    rows: list[list[int]] = []
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        rows.append([int(item[0]), int(item[1])])
        except (json.JSONDecodeError, TypeError, ValueError):
            rows = []
    pair = [int(chat_id), int(message_id)]
    rows = [r for r in rows if not (r[0] == pair[0] and r[1] == pair[1])]
    rows.append(pair)
    set_state(session, key, json.dumps(rows[-40:]))


def get_renew_pay_admin_messages(
    session: Session, job_id: int
) -> list[tuple[int, int]]:
    key = f"renew_pay_ui:{int(job_id)}"
    raw = (get_state(session, key) or "").strip()
    if not raw:
        return []
    out: list[tuple[int, int]] = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    out.append((int(item[0]), int(item[1])))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return out


def set_renew_pay_admin_messages(
    session: Session, job_id: int, rows: list[tuple[int, int]]
) -> None:
    key = f"renew_pay_ui:{int(job_id)}"
    payload = [[int(a), int(b)] for a, b in rows][-40:]
    set_state(session, key, json.dumps(payload) if payload else "")


def pop_renew_pay_admin_messages(
    session: Session, job_id: int
) -> list[tuple[int, int]]:
    key = f"renew_pay_ui:{int(job_id)}"
    raw = (get_state(session, key) or "").strip()
    set_state(session, key, "")
    if not raw:
        return []
    out: list[tuple[int, int]] = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    out.append((int(item[0]), int(item[1])))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return out


def get_state(session: Session, key: str, default: str = "") -> str:
    row = session.get(AppState, key)
    return row.value if row else default


def set_state(session: Session, key: str, value: str) -> None:
    row = session.get(AppState, key)
    if row:
        row.value = value
    else:
        session.add(AppState(key=key, value=value))


def ensure_default_price_plans(session: Session) -> None:
    """Seed NetPlus monthly + FineArts volume plans; disable unknown keys."""
    removed = _removed_price_plan_keys(session)
    wanted_keys = {row[0] for row in DEFAULT_PRICE_PLANS} - removed
    for i, (key, title, days, gb, price, cost) in enumerate(DEFAULT_PRICE_PLANS):
        if key in removed:
            continue
        plan = session.scalar(select(PricePlan).where(PricePlan.key == key))
        if not plan:
            # Migrate old 30GB key if present
            if key == "1m_25gb":
                old = session.scalar(select(PricePlan).where(PricePlan.key == "1m_30gb"))
                if old:
                    old.key = "1m_25gb"
                    old.title = title
                    old.traffic_gb = gb
                    old.duration_days = days
                    old.price_toman = price
                    old.vendor_cost_toman = cost or None
                    old.sort_order = i
                    old.enabled = True
                    continue
            session.add(
                PricePlan(
                    key=key,
                    title=title,
                    duration_days=days,
                    traffic_gb=gb,
                    price_toman=price,
                    vendor_cost_toman=cost or None,
                    enabled=True,
                    sort_order=i,
                )
            )
        else:
            plan.title = title
            plan.traffic_gb = gb
            plan.duration_days = days
            # Keep admin-edited sell price; only seed cost + sort/enabled.
            if plan.price_toman <= 0:
                plan.price_toman = price
            if cost and not plan.vendor_cost_toman:
                plan.vendor_cost_toman = cost
            plan.sort_order = i
            plan.enabled = True
            # Always refresh FineArts list prices from catalog (new sell rates).
            if key.startswith("fa_"):
                plan.price_toman = price
                plan.vendor_cost_toman = cost or None
    # Disable anything not in the sell catalog (2m/3m/legacy)
    for plan in session.scalars(select(PricePlan)).all():
        if plan.key not in wanted_keys:
            plan.enabled = False
    session.flush()


def vendor_key_for_price_plan(plan: PricePlan | None) -> str:
    if plan is None:
        return DEFAULT_VENDOR_KEY
    key = (plan.key or "").strip().lower()
    if key.startswith("fa_"):
        return "finearts"
    return DEFAULT_VENDOR_KEY


def vendor_key_for_subscription(
    session: Session, sub: Subscription | None, *, username: str | None = None
) -> str:
    """Authoritative vendor for an existing service (never infer from plan GB alone)."""
    if sub is None and username:
        sub = get_subscription_by_username(session, username)
    if sub is not None and sub.vendor_id:
        vendor = get_vendor(session, sub.vendor_id)
        if vendor and (vendor.key or "").strip():
            return (vendor.key or "").strip().lower()
    if username or (sub and sub.username):
        resolved = resolve_vendor_for_username(
            session, username or (sub.username if sub else "")
        )
        if resolved and (resolved.key or "").strip():
            return (resolved.key or "").strip().lower()
    return DEFAULT_VENDOR_KEY


def plan_matches_service_vendor(
    session: Session,
    plan: PricePlan | None,
    *,
    username: str | None = None,
    subscription: Subscription | None = None,
) -> bool:
    """True when sell-plan vendor equals the subscription's real vendor."""
    if plan is None:
        return False
    service_key = vendor_key_for_subscription(
        session, subscription, username=username
    )
    return vendor_key_for_price_plan(plan) == service_key


def finearts_package_callback_for_plan(plan: PricePlan | None) -> str | None:
    """Map sell plan → FineArts driver package token (50/100 GB volume)."""
    if plan is None:
        return None
    key = (plan.key or "").strip().lower()
    gb = int(round(float(plan.traffic_gb or 0)))
    if key == "fa_50gb" or (key.startswith("fa_") and gb == 50):
        return "fa_vol_50"
    if key == "fa_100gb" or (key.startswith("fa_") and gb == 100):
        return "fa_vol_100"
    return None


def list_price_plans(session: Session, *, enabled_only: bool = False) -> list[PricePlan]:
    q = select(PricePlan).order_by(PricePlan.sort_order.asc(), PricePlan.id.asc())
    if enabled_only:
        q = q.where(PricePlan.enabled.is_(True))
    return list(session.scalars(q))


def get_price_plan(session: Session, plan_id: int) -> PricePlan | None:
    return session.get(PricePlan, plan_id)


def set_price_toman(session: Session, plan_id: int, price_toman: int) -> PricePlan | None:
    plan = session.get(PricePlan, plan_id)
    if not plan:
        return None
    plan.price_toman = price_toman
    plan.updated_at = datetime.utcnow()
    session.flush()
    return plan


def _removed_price_plan_keys(session: Session) -> set[str]:
    raw = (get_state(session, "price_plan_removed_keys") or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _save_removed_price_plan_keys(session: Session, keys: set[str]) -> None:
    set_state(session, "price_plan_removed_keys", ",".join(sorted(keys)))


def rename_price_plan(
    session: Session,
    plan_id: int,
    *,
    title: str,
    also_rename_product_names: bool = False,
) -> tuple[PricePlan | None, int]:
    """Rename plan title. Optionally retitle matching subscription.product_name rows."""
    plan = session.get(PricePlan, plan_id)
    if not plan:
        return None, 0
    old = (plan.title or "").strip()
    new = (title or "").strip()
    if not new:
        return plan, 0
    plan.title = new[:128]
    plan.updated_at = datetime.utcnow()
    touched = 0
    if also_rename_product_names and old and old != new:
        for sub in session.scalars(
            select(Subscription).where(Subscription.product_name == old)
        ):
            sub.product_name = new[:64]
            touched += 1
    session.flush()
    return plan, touched


def reorder_price_plan(session: Session, plan_id: int, *, direction: str) -> bool:
    """Swap sort_order with neighbor (up = earlier in list)."""
    plans = list_price_plans(session, enabled_only=False)
    idx = next((i for i, p in enumerate(plans) if p.id == plan_id), None)
    if idx is None:
        return False
    swap_with = idx - 1 if direction == "up" else idx + 1
    if swap_with < 0 or swap_with >= len(plans):
        return False
    a, b = plans[idx], plans[swap_with]
    a.sort_order, b.sort_order = b.sort_order, a.sort_order
    # Normalize if ties
    if a.sort_order == b.sort_order:
        a.sort_order, b.sort_order = idx, swap_with
    a.updated_at = datetime.utcnow()
    b.updated_at = datetime.utcnow()
    session.flush()
    return True


def delete_price_plan(session: Session, plan_id: int) -> bool:
    """Remove a sell plan and remember its key so ensure_default won't revive it."""
    plan = session.get(PricePlan, plan_id)
    if not plan:
        return False
    removed = _removed_price_plan_keys(session)
    removed.add(plan.key)
    _save_removed_price_plan_keys(session, removed)
    session.delete(plan)
    session.flush()
    return True


def unlink_subscription_owner(session: Session, subscription_id: int) -> bool:
    """Drop local ownership links for one inventory row (service stays)."""
    sub = session.get(Subscription, subscription_id)
    if not sub:
        return False
    removed = False
    link = get_link_for_subscription(session, subscription_id)
    if link:
        session.delete(link)
        removed = True
    # Also clear username-only links that match
    for row in list(
        session.scalars(
            select(CustomerServiceLink).where(
                CustomerServiceLink.service_username == sub.username
            )
        )
    ):
        session.delete(row)
        removed = True
    session.flush()
    return removed


def delete_customer_profile_keep_services(
    session: Session, telegram_id: int
) -> dict[str, int]:
    """Delete customer profile + ownership links. Inventory rows are kept unowned."""
    from app.database.models import WalletLedger

    links = list_customer_services(session, telegram_id)
    unlinked = 0
    for link in links:
        session.delete(link)
        unlinked += 1
    ledgers = list(
        session.scalars(
            select(WalletLedger).where(WalletLedger.telegram_id == telegram_id)
        )
    )
    for row in ledgers:
        session.delete(row)
    profile = get_customer_profile(session, telegram_id)
    if profile:
        session.delete(profile)
    session.flush()
    return {"unlinked": unlinked, "ledger_rows": len(ledgers), "deleted_profile": 1 if profile else 0}


def clear_customer_orders(
    session: Session, *, statuses: tuple[str, ...] | None = None
) -> int:
    """Delete customer shop orders (does not touch inventory). Returns deleted count."""
    q = select(CustomerOrder)
    if statuses:
        q = q.where(CustomerOrder.status.in_(statuses))
    rows = list(session.scalars(q))
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


def delete_customer_orders_by_ids(session: Session, order_ids: list[int]) -> int:
    n = 0
    for oid in order_ids:
        row = session.get(CustomerOrder, int(oid))
        if row:
            session.delete(row)
            n += 1
    session.flush()
    return n


def upsert_customer_profile(
    session: Session,
    telegram_id: int,
    *,
    username: str | None = None,
    full_name: str | None = None,
) -> CustomerProfile:
    row = session.scalar(
        select(CustomerProfile).where(CustomerProfile.telegram_id == telegram_id)
    )
    if not row:
        now = datetime.utcnow()
        row = CustomerProfile(
            telegram_id=telegram_id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    if username is not None:
        row.username = username
    if full_name is not None:
        row.full_name = full_name
    if not getattr(row, "created_at", None):
        row.created_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    session.flush()
    return row


def link_customer_service(session: Session, telegram_id: int, service_username: str) -> CustomerServiceLink:
    uname = service_username.strip()
    row = session.scalar(
        select(CustomerServiceLink).where(
            CustomerServiceLink.telegram_id == telegram_id,
            CustomerServiceLink.service_username == uname,
        )
    )
    if row:
        return row
    row = CustomerServiceLink(telegram_id=telegram_id, service_username=uname)
    session.add(row)
    session.flush()
    return row


def unlink_customer_service(
    session: Session, telegram_id: int, service_username: str
) -> bool:
    uname = service_username.strip()
    row = session.scalar(
        select(CustomerServiceLink).where(
            CustomerServiceLink.telegram_id == telegram_id,
            CustomerServiceLink.service_username == uname,
        )
    )
    if not row:
        return False
    session.delete(row)
    session.flush()
    return True


def list_customer_services(session: Session, telegram_id: int) -> list[CustomerServiceLink]:
    """Customer-owned links, newest inventory first (subscription.created_at)."""
    return list(
        session.scalars(
            select(CustomerServiceLink)
            .outerjoin(
                Subscription,
                CustomerServiceLink.subscription_id == Subscription.id,
            )
            .where(CustomerServiceLink.telegram_id == telegram_id)
            .order_by(
                func.coalesce(
                    Subscription.created_at, CustomerServiceLink.created_at
                ).desc(),
                CustomerServiceLink.id.desc(),
            )
        )
    )


def get_customer_profile(
    session: Session, telegram_id: int
) -> CustomerProfile | None:
    return session.scalar(
        select(CustomerProfile).where(CustomerProfile.telegram_id == telegram_id)
    )


def find_usable_subscription_by_username(
    session: Session, username: str, *, exclude_subscription_id: int | None = None
) -> Subscription | None:
    """Return any currently usable inventory row with this username (any vendor)."""
    name = (username or "").strip()
    if not name:
        return None
    q = select(Subscription).where(
        Subscription.username == name,
        Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)),
    )
    if exclude_subscription_id is not None:
        q = q.where(Subscription.id != exclude_subscription_id)
    return session.scalar(q.limit(1))


def is_usable_username_available(
    session: Session, username: str, *, exclude_subscription_id: int | None = None
) -> bool:
    return (
        find_usable_subscription_by_username(
            session, username, exclude_subscription_id=exclude_subscription_id
        )
        is None
    )


def get_link_for_subscription(
    session: Session, subscription_id: int
) -> CustomerServiceLink | None:
    return session.scalar(
        select(CustomerServiceLink).where(
            CustomerServiceLink.subscription_id == subscription_id
        )
    )


def get_link_for_username(
    session: Session, service_username: str
) -> CustomerServiceLink | None:
    uname = (service_username or "").strip()
    if not uname:
        return None
    return session.scalar(
        select(CustomerServiceLink)
        .where(CustomerServiceLink.service_username == uname)
        .order_by(CustomerServiceLink.id.desc())
        .limit(1)
    )


def transfer_local_ownership(
    session: Session,
    *,
    from_telegram_id: int,
    to_telegram_id: int,
    service_username: str | None = None,
    subscription: Subscription | None = None,
    subscription_id: int | None = None,
) -> CustomerServiceLink:
    """Immediate C2C local ownership transfer (no NetPlus). Caller must commit."""
    if int(from_telegram_id) == int(to_telegram_id):
        raise ValueError("same_owner")
    sub = subscription
    if sub is None and subscription_id is not None:
        sub = session.get(Subscription, subscription_id)
    uname = (service_username or (sub.username if sub else "") or "").strip()
    if not uname and sub is None:
        raise ValueError("service_username_required")

    owned: CustomerServiceLink | None = None
    if sub is not None:
        owned = get_link_for_subscription(session, sub.id)
    if owned is None and uname:
        owned = get_link_for_username(session, uname)
    if owned is None or int(owned.telegram_id) != int(from_telegram_id):
        raise ValueError("not_owner")

    return set_local_owner(
        session,
        to_telegram_id=to_telegram_id,
        initiated_by_telegram_id=from_telegram_id,
        source="customer_transfer",
        subscription=sub,
        subscription_id=owned.subscription_id or (sub.id if sub else None),
        service_username=owned.service_username or uname,
        note="c2c_immediate",
    )


def set_local_owner(
    session: Session,
    *,
    to_telegram_id: int,
    initiated_by_telegram_id: int,
    source: str,
    subscription: Subscription | None = None,
    subscription_id: int | None = None,
    service_username: str | None = None,
    note: str | None = None,
) -> CustomerServiceLink:
    """Atomically assign local ownership (no audit table).

    Does not touch provider fields, orders, or wallet. Revalidates inside the
    caller's transaction; caller must commit.
    """
    _ = initiated_by_telegram_id, source, note  # reserved for future logging
    sub = subscription
    if sub is None and subscription_id is not None:
        sub = session.get(Subscription, subscription_id)
    uname = (service_username or (sub.username if sub else "") or "").strip()
    if not uname:
        raise ValueError("service_username_required")
    if sub is None:
        sub = session.scalar(
            select(Subscription)
            .where(
                Subscription.username == uname,
                Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)),
            )
            .order_by(Subscription.id.desc())
            .limit(1)
        )
    profile = get_customer_profile(session, to_telegram_id)
    if not profile:
        raise ValueError("recipient_profile_missing")

    # Remove any existing ownership of this subscription / username.
    existing: list[CustomerServiceLink] = []
    if sub is not None:
        existing.extend(
            session.scalars(
                select(CustomerServiceLink).where(
                    CustomerServiceLink.subscription_id == sub.id
                )
            )
        )
    existing.extend(
        session.scalars(
            select(CustomerServiceLink).where(
                CustomerServiceLink.service_username == uname
            )
        )
    )
    seen_ids: set[int] = set()
    for link in existing:
        if link.id in seen_ids:
            continue
        seen_ids.add(link.id)
        session.delete(link)
    session.flush()

    row = CustomerServiceLink(
        telegram_id=to_telegram_id,
        service_username=uname,
        subscription_id=sub.id if sub else None,
        vendor_id=sub.vendor_id if sub else None,
    )
    session.add(row)
    session.flush()
    return row


def list_unlinked_subscriptions(
    session: Session, *, limit: int | None = None, offset: int = 0
) -> list[Subscription]:
    """Subscriptions with no customer_service_links row (by subscription_id or username)."""
    rows, _total = list_unlinked_subscriptions_page(
        session, offset=offset, limit=limit if limit is not None else 10_000
    )
    return rows


def list_unlinked_subscriptions_page(
    session: Session, *, offset: int = 0, limit: int = 10
) -> tuple[list[Subscription], int]:
    """Paginated unlinked usable subscriptions. Returns (page_rows, total)."""
    linked_ids = {
        i
        for i in session.scalars(
            select(CustomerServiceLink.subscription_id).where(
                CustomerServiceLink.subscription_id.is_not(None)
            )
        )
        if i is not None
    }
    linked_names = set(
        session.scalars(select(CustomerServiceLink.service_username))
    )
    q = (
        select(Subscription)
        .where(Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)))
        .order_by(Subscription.id.asc())
    )
    all_unlinked: list[Subscription] = []
    for sub in session.scalars(q):
        if sub.id in linked_ids or sub.username in linked_names:
            continue
        all_unlinked.append(sub)
    total = len(all_unlinked)
    start = max(0, offset)
    end = start + max(1, limit)
    return all_unlinked[start:end], total


def count_unlinked_subscriptions(session: Session) -> int:
    _rows, total = list_unlinked_subscriptions_page(session, offset=0, limit=1)
    # recount properly — page helper already has full total
    return total


def assign_subscriptions_to_owner(
    session: Session,
    *,
    subscription_ids: list[int],
    to_telegram_id: int,
    initiated_by_telegram_id: int,
    note: str | None = None,
) -> list[CustomerServiceLink]:
    """Assign a specific set of subscriptions to one customer (reassign OK)."""
    links: list[CustomerServiceLink] = []
    for sid in subscription_ids:
        sub = session.get(Subscription, int(sid))
        if sub is None:
            continue
        links.append(
            set_local_owner(
                session,
                to_telegram_id=to_telegram_id,
                initiated_by_telegram_id=initiated_by_telegram_id,
                source="admin_page",
                subscription=sub,
                note=note or "page_assign",
            )
        )
    return links


def bulk_assign_unlinked(
    session: Session,
    *,
    to_telegram_id: int,
    initiated_by_telegram_id: int,
    note: str | None = None,
) -> list[CustomerServiceLink]:
    """Assign every currently unlinked usable subscription to one customer."""
    links: list[CustomerServiceLink] = []
    for sub in list_unlinked_subscriptions(session):
        links.append(
            set_local_owner(
                session,
                to_telegram_id=to_telegram_id,
                initiated_by_telegram_id=initiated_by_telegram_id,
                source="admin_bulk",
                subscription=sub,
                note=note or "bulk_unlinked",
            )
        )
    return links


def get_wallet_toman(session: Session, telegram_id: int) -> int:
    from app.database.models import CustomerProfile

    row = session.scalar(
        select(CustomerProfile).where(CustomerProfile.telegram_id == telegram_id)
    )
    return int(row.wallet_toman) if row else 0


def credit_wallet(
    session: Session,
    telegram_id: int,
    amount_toman: int,
    *,
    kind: str = "charge",
    order_id: int | None = None,
    note: str | None = None,
    allow_overdraft: bool = False,
) -> int:
    """Credit (or debit if negative) wallet; returns new balance.

    Debits that would go below zero raise ValueError unless allow_overdraft.
    """
    from app.database.models import CustomerProfile, WalletLedger

    if amount_toman == 0:
        return get_wallet_toman(session, telegram_id)
    row = session.scalar(
        select(CustomerProfile).where(CustomerProfile.telegram_id == telegram_id)
    )
    if not row:
        row = CustomerProfile(telegram_id=telegram_id, wallet_toman=0)
        session.add(row)
        session.flush()
    new_bal = int(row.wallet_toman or 0) + int(amount_toman)
    if new_bal < 0 and not allow_overdraft:
        raise ValueError("insufficient_wallet")
    row.wallet_toman = new_bal
    row.updated_at = datetime.utcnow()
    session.add(
        WalletLedger(
            telegram_id=telegram_id,
            amount_toman=int(amount_toman),
            balance_after=row.wallet_toman,
            kind=kind,
            order_id=order_id,
            note=note,
        )
    )
    session.flush()
    return row.wallet_toman


def debit_wallet(
    session: Session,
    telegram_id: int,
    amount_toman: int,
    *,
    kind: str = "spend",
    order_id: int | None = None,
    note: str | None = None,
) -> int:
    """Debit wallet; raises ValueError if balance is insufficient."""
    amount = abs(int(amount_toman))
    return credit_wallet(
        session,
        telegram_id,
        -amount,
        kind=kind,
        order_id=order_id,
        note=note,
        allow_overdraft=False,
    )


def wallet_charge_already_posted(session: Session, order_id: int) -> bool:
    """True if a positive charge ledger row already exists for this order."""
    from app.database.models import WalletLedger

    row = session.scalar(
        select(WalletLedger)
        .where(
            WalletLedger.order_id == order_id,
            WalletLedger.kind == "charge",
            WalletLedger.amount_toman > 0,
        )
        .limit(1)
    )
    return row is not None


def credit_wallet_once_for_order(
    session: Session,
    telegram_id: int,
    amount_toman: int,
    *,
    order_id: int,
    note: str | None = None,
) -> tuple[int, bool]:
    """Credit wallet for an order at most once. Returns (balance, credited_now)."""
    if amount_toman <= 0:
        return get_wallet_toman(session, telegram_id), False
    if wallet_charge_already_posted(session, order_id):
        return get_wallet_toman(session, telegram_id), False
    bal = credit_wallet(
        session,
        telegram_id,
        amount_toman,
        kind="charge",
        order_id=order_id,
        note=note or "order_charge",
    )
    return bal, True


def refund_wallet_applied(session: Session, order: CustomerOrder) -> int:
    """Refund wallet_applied_toman once; returns amount refunded."""
    from app.database.models import WalletLedger

    applied = int(getattr(order, "wallet_applied_toman", 0) or 0)
    if applied <= 0:
        return 0
    already = session.scalar(
        select(WalletLedger)
        .where(
            WalletLedger.order_id == order.id,
            WalletLedger.kind == "adjust",
            WalletLedger.amount_toman > 0,
            WalletLedger.note.is_not(None),
        )
        .limit(1)
    )
    if already and already.note and "refund" in (already.note or ""):
        order.wallet_applied_toman = 0
        return 0
    credit_wallet(
        session,
        order.telegram_id,
        applied,
        kind="adjust",
        order_id=order.id,
        note="wallet_refund",
    )
    order.wallet_applied_toman = 0
    touch_customer_order(order)
    return applied


def list_wallet_ledger(
    session: Session, telegram_id: int, *, limit: int = 10
) -> list:
    from app.database.models import WalletLedger

    return list(
        session.scalars(
            select(WalletLedger)
            .where(WalletLedger.telegram_id == telegram_id)
            .order_by(WalletLedger.id.desc())
            .limit(limit)
        )
    )


def get_payment_card(session: Session, env_number: str = "", env_holder: str = "") -> tuple[str, str]:
    """Card for customer transfers: DB override wins, else .env."""
    number = get_state(session, "reseller_card_number") or env_number
    holder = get_state(session, "reseller_card_holder") or env_holder
    return number.replace(" ", "").strip(), (holder or "").strip()


def set_payment_card(session: Session, number: str, holder: str) -> None:
    set_state(session, "reseller_card_number", number.replace(" ", "").strip())
    set_state(session, "reseller_card_holder", (holder or "").strip())


DEFAULT_INVOICE_DESCRIPTION = "پرداخت سفارش"
DEFAULT_PRICING_TEXT = (
    "💰 <b>تعرفه سرویس‌ها</b>\n\n"
    "قیمت‌های فعلی فروشگاه در ادامه آمده است."
)
DEFAULT_WALLET_MIN = 10000
DEFAULT_WALLET_MAX = 600000

# Order payment window + reminder schedule (seconds), admin-overridable via AppState
DEFAULT_ORDER_TTL_SECONDS = 86400  # 24h
DEFAULT_REMINDER_DELAYS_SECONDS = (300, 3600, 21600)  # 5m, 1h, 6h
DEFAULT_USERINFO_TTL_SECONDS = 120  # HTTP subscription-userinfo cache
DEFAULT_ALERT_DAYS_THRESHOLD = 3
DEFAULT_ALERT_TRAFFIC_MB_THRESHOLD = 1024.0  # 1 GB
DEFAULT_LIFECYCLE_ALERT_COOLDOWN_SECONDS = 86400  # 24h
DEFAULT_LIFECYCLE_SCAN_INTERVAL_SECONDS = 300  # 5m
DEFAULT_ADMIN_VENDOR_ALERT_MAX_REPEATS = 3
DEFAULT_ADMIN_VENDOR_ALERT_WINDOW_HOURS = 72
DEFAULT_ADMIN_VENDOR_DELETED_SUPPRESS_HOURS = 168  # 7d
# When True: paid create/renew wait for admin approve (no auto fulfill).
DEFAULT_ORDERS_MANUAL_APPROVAL = False
# Legacy: vendor renew/create settle (kept for old state; UI removed).
DEFAULT_VENDOR_SETTLE_MODE = "card"
# Customer pays NetPlus card directly (wallet top-up walk) for create/renew.
DEFAULT_DIRECT_VENDOR_CARD_PAY = False

DEFAULT_BOT_MESSAGES: dict[str, str] = {
    "welcome": (
        "سلام 👋 خوش آمدید.\n\n"
        "برای تهیه اشتراک از «خرید اشتراک» استفاده کنید.\n"
        "می‌توانید ابتدا کیف پول را شارژ کنید و بعد سرویس بخرید."
    ),
    "wallet_amount_prompt": (
        "💸 مبلغ را به <b>تومان</b> وارد کنید:\n"
        "✅ حداقل مبلغ <code>{min}</code> حداکثر مبلغ <code>{max}</code> تومان می‌باشد"
    ),
    "wallet_amount_invalid": "فقط مبلغ به تومان بفرستید (عدد).",
    "wallet_amount_range": (
        "مبلغ باید بین <code>{min}</code> و <code>{max}</code> تومان باشد.\n"
        "لطفاً دوباره وارد کنید."
    ),
    "username_invalid": "❌ نام کاربری نامعتبر است.\n{help}",
    "receipt_submit_prompt": (
        "📷 لطفاً <b>عکس یا فایل رسید پرداخت</b> را همین‌جا ارسال کنید.\n"
        "سفارش #{order}"
    ),
    "receipt_received": (
        "✅ رسید شما دریافت شد و در صف بررسی قرار گرفت.\n"
        "پس از بررسی، نتیجه به شما اطلاع داده می‌شود."
    ),
    "receipt_rejected_expired": (
        "⏰ مهلت این سفارش به پایان رسیده و دیگر امکان ارسال رسید وجود ندارد.\n"
        "لطفاً سفارش جدید ایجاد کنید."
    ),
    "order_expired": (
        "⏰ سفارش #{order} منقضی شد.\n"
        "برای ادامه، لطفاً سفارش جدیدی ایجاد کنید."
    ),
    "payment_reminder": (
        "⏳ هنوز پرداخت سفارش #{order} تایید نشده است.\n\n"
        "اگر واریز کرده‌اید، دکمه «ارسال رسید پرداخت» را بزنید و عکس رسید را بفرستید.\n"
        "مبلغ: <code>{amount}</code> ریال\n"
        "مهلت تا: {deadline}"
    ),
    "c2c_footer": (
        "📌 پس از واریز، حتماً با دکمه «ارسال رسید پرداخت» تصویر رسید را ارسال کنید.\n"
        "حتی اگر قبل از باز کردن ربات واریز کرده باشید، تا قبل از انقضای سفارش می‌توانید رسید را بفرستید."
    ),
    "guide_empty": "📚 هنوز محتوایی در بخش آموزش ثبت نشده است.",
    "support_hint": (
        "🆘 {support}\n\n"
        "از دکمه‌ها یکی را انتخاب کنید:\n"
        "• ارسال پیام به پشتیبانی\n"
        "• تیکت‌های من"
    ),
    "support_ask_subject": (
        "📌 موضوع تیکت را بفرستید (حداکثر {max} کاراکتر).\n"
        "فقط موضوع کوتاه — متن اصلی پیام را بعداً می‌فرستید."
    ),
    "support_subject_too_long": (
        "❌ موضوع طولانی است. حداکثر {max} کاراکتر مجاز است.\n"
        "الان: {n} کاراکتر."
    ),
    "support_ask_body": (
        "✏️ متن پیام پشتیبانی را بفرستید.\n"
        "موضوع: <b>{subject}</b>"
    ),
    "support_ticket_closed_timeout": (
        "⌛ تیکت #{id} به‌خاطر نبود پاسخ شما تا ۲۴ ساعت بسته شد.\n"
        "موضوع: {subject}\n"
        "در صورت نیاز از پشتیبانی تیکت جدید باز کنید."
    ),
    "reject_customer_notice": (
        "❌ سفارش #{order} تایید نشد.\n\n"
        "دلیل:\n{reason}\n\n"
        "می‌توانید همین‌جا پاسخ دهید تا گفتگو ادامه پیدا کند."
    ),
    "alert_almost_out_days": (
        "با سلام خدمت شما کاربر گرامی 👋\n"
        "📌 از مهلت زمانی استفاده از سرویس <code>{username}</code> فقط "
        "<b>{days}</b> روز باقی مانده است. لطفاً در صورت تمایل برای تمدید "
        "سرویستون از طریق بخش «🛍 سرویس های من» اقدام بفرمایین. با تشکر از همراهی شما"
    ),
    "alert_almost_out_traffic": (
        "با سلام خدمت شما کاربر گرامی 👋\n"
        "🚨 از حجم سرویس <code>{username}</code> تنها <b>{traffic}</b> باقی مانده است. "
        "لطفاً در صورت تمایل برای تمدید سرویستون از طریق بخش «🛍 سرویس های من» "
        "اقدام بفرمایین"
    ),
    "alert_expired_days": (
        "با سلام خدمت شما کاربر گرامی 👋\n"
        "🔚 مهلت زمانی استفاده از سرویس <code>{username}</code> به پایان رسیده است. "
        "لطفاً در صورت تمایل برای تمدید سرویستون از طریق بخش «🛍 سرویس های من» "
        "اقدام بفرمایین. با تشکر از همراهی شما"
    ),
    "alert_volume_exhausted": (
        "با سلام خدمت شما کاربر گرامی 👋\n"
        "🚫 حجم سرویس <code>{username}</code> به پایان رسیده است. "
        "لطفاً در صورت تمایل برای تمدید سرویستون از طریق بخش «🛍 سرویس های من» "
        "اقدام بفرمایین"
    ),
    "alert_service_deleted": (
        "📌 کاربر گرامی بدلیل عدم تمدید، سرویس {username} از لیست سرویس های شما حذف گردید\n\n"
        "🌟 جهت تهیه سرویس جدید از گزینه خرید سرویس اقدام فرمایید"
    ),
    "admin.help": (
        "ℹ️ <b>راهنمای سریع</b>\n"
        "📋 منوی پایین = دسترسی سریع\n"
        "⚠️ هشدار فروشنده = وضعیت warning از NetPlus/قدیمی\n"
        "🪫 نزدیک اتمام = آستانه محلی روی دادهٔ خودمان\n"
        "✨ سرویس‌ها → جزئیات و تمدید\n"
        "💳 پرداخت: کپی مبلغ و کارت، واریز، منتظر تایید"
    ),
    "admin.welcome": (
        "سلام 👋 پلن مدیریت آماده است.\n"
        "از منوی پایین استفاده کنید.\n"
        "تنظیمات قابل‌پیکربندی زیر «⚙️ تنظیمات» هستند.\n"
        "درخواست‌های مشتری زیر «📥 درخواست‌ها»."
    ),
    "admin.status_header": "📊 <b>وضعیت اشتراک‌ها</b>",
    "admin.search_prompt": (
        "🔍 <b>جستجوی سریع</b>\nبخشی از نام کاربری را بفرستید:"
    ),
    "admin.create_prompt": "🛒 نام کاربری سرویس جدید را بفرستید:",
    "admin.settings_prompt": (
        "⚙️ <b>تنظیمات</b>\nیک بخش را انتخاب کنید:"
    ),
    "admin.requests_prompt": (
        "📥 <b>درخواست‌های مشتری</b>\nبر اساس وضعیت فیلتر کنید:"
    ),
    "admin.sync_prompt": (
        "🔄 <b>همگام‌سازی افزایشی</b>\n"
        "کدام فروشنده را با لیست زنده مقایسه کنیم؟"
    ),
    "admin.ownership_prompt": (
        "👤 <b>مالکیت سرویس</b>\n"
        "بدون مالک (قابل اختصاص): <b>{unlinked}</b>\n"
        "اختصاص فقط در دیتابیس محلی است؛ به NetPlus منتقل نمی‌شود."
    ),
}


def get_pricing_text(session: Session) -> str:
    raw = (get_state(session, "customer_pricing_text") or "").strip()
    # Scrub legacy admin-jargon copy that leaked to customers
    if "پنل ادمین" in raw or "قابل ویرایش" in raw:
        raw = DEFAULT_PRICING_TEXT
        set_state(session, "customer_pricing_text", raw)
    return raw or DEFAULT_PRICING_TEXT


def set_pricing_text(session: Session, text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        cleaned = DEFAULT_PRICING_TEXT
    set_state(session, "customer_pricing_text", cleaned)
    return cleaned


def get_wallet_limits(session: Session) -> tuple[int, int]:
    try:
        mn = int(get_state(session, "wallet_min_toman") or DEFAULT_WALLET_MIN)
    except ValueError:
        mn = DEFAULT_WALLET_MIN
    try:
        mx = int(get_state(session, "wallet_max_toman") or DEFAULT_WALLET_MAX)
    except ValueError:
        mx = DEFAULT_WALLET_MAX
    if mn < 1:
        mn = DEFAULT_WALLET_MIN
    if mx < mn:
        mx = max(mn, DEFAULT_WALLET_MAX)
    return mn, mx


def set_wallet_limits(session: Session, minimum: int, maximum: int) -> tuple[int, int]:
    mn = max(1, int(minimum))
    mx = max(mn, int(maximum))
    set_state(session, "wallet_min_toman", str(mn))
    set_state(session, "wallet_max_toman", str(mx))
    return mn, mx


def find_plan_by_traffic_gb(
    session: Session,
    traffic_gb: float,
    *,
    duration_days: int = 31,
    vendor_key: str | None = None,
) -> PricePlan | None:
    plans = list_price_plans(session, enabled_only=True)
    want_vendor = (vendor_key or "").strip().lower() or None
    for p in plans:
        if want_vendor and vendor_key_for_price_plan(p) != want_vendor:
            continue
        if p.duration_days == duration_days and p.traffic_gb is not None:
            if abs(float(p.traffic_gb) - float(traffic_gb)) < 0.1:
                return p
    return None


def find_renew_plan_for_username(
    session: Session, username: str
) -> PricePlan | None:
    """Match enabled sell plan for renew-current on the SAME vendor only.

    Prefer product_name (e.g. ۲۵ گیگ) over cumulative traffic_limit_mb, which can
    stack across renewals and falsely look like a larger plan.

    Critical: never match NetPlus 100GB monthly to FineArts fa_100gb (or reverse).
    """
    sub = get_subscription_by_username(session, username)
    service_vendor = vendor_key_for_subscription(session, sub, username=username)
    plans = [
        p
        for p in list_price_plans(session, enabled_only=True)
        if vendor_key_for_price_plan(p) == service_vendor
    ]
    if not plans:
        return None

    # FineArts volume → duration 0 first; NetPlus economy → monthly first.
    if service_vendor == "finearts":
        day_order = (0, 31, 30, 61, 62, 91, 92)
    else:
        day_order = (31, 30, 61, 62, 91, 92, 0)

    # 1) Product title / key hints (most accurate for "current plan")
    if sub and (sub.product_name or "").strip():
        pname = (sub.product_name or "").replace(" ", "").replace("‌", "")
        digits = ""
        for ch in pname:
            if ch.isdigit() or ch in "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩":
                digits += ch
            elif digits:
                break
        if digits:
            from app.utils.text import to_number

            gb_hint = to_number(digits)
            if gb_hint is not None:
                for days in day_order:
                    hit = find_plan_by_traffic_gb(
                        session,
                        float(gb_hint),
                        duration_days=days,
                        vendor_key=service_vendor,
                    )
                    if hit:
                        return hit
        for p in plans:
            title = (p.title or "").replace(" ", "").replace("‌", "")
            if title and (title in pname or pname in title):
                return p

    # 2) Only if product unknown: closest to traffic_limit (may be cumulative)
    if sub and sub.traffic_limit_mb:
        gb = float(sub.traffic_limit_mb) / 1024.0
        for days in day_order:
            hit = find_plan_by_traffic_gb(
                session, gb, duration_days=days, vendor_key=service_vendor
            )
            if hit:
                return hit
        best = None
        best_diff = 1e9
        for p in plans:
            if p.traffic_gb is None:
                continue
            diff = abs(float(p.traffic_gb) - gb)
            if diff < best_diff:
                best = p
                best_diff = diff
        if best:
            return best
    return next((p for p in plans if int(p.duration_days or 0) > 0), plans[0])


def list_enabled_duration_days(session: Session) -> list[int]:
    """Distinct duration_days from enabled sell plans, ascending."""
    plans = list_price_plans(session, enabled_only=True)
    return sorted({int(p.duration_days) for p in plans})


def renew_extend_callback_for_days(days: int | None) -> str:
    """NetPlus renew duration callback for change-plan path."""
    d = 31 if days is None else int(days)
    if d <= 0:
        # Time-unlimited FineArts volume — no NetPlus month callback.
        return ""
    if d >= 90:
        return "productextendmonths_91"
    if d >= 60:
        return "productextendmonths_61"
    return "productextendmonths_31"


def create_month_callback_for_days(days: int | None) -> str:
    """NetPlus create duration callback from sell-plan days."""
    d = 31 if days is None else int(days)
    if d <= 0:
        return ""
    if d >= 90:
        return "productmonth_91"
    if d >= 60:
        return "productmonth_61"
    return "productmonth_31"


def create_job_args_from_plan(
    session: Session, plan: PricePlan | None
) -> tuple[str | None, str | None, int | None]:
    """
    Map a local sell plan → (duration_callback, package_callback, vendor_id).
    Used by admin create so we never re-query vendor package menus.
    """
    if plan is None:
        return None, None, None
    vkey = vendor_key_for_price_plan(plan)
    vendor = get_vendor_by_key(session, vkey) if vkey else None
    vendor_id = vendor.id if vendor else None
    if vkey == "finearts":
        return None, finearts_package_callback_for_plan(plan), vendor_id
    days = int(plan.duration_days) if plan.duration_days is not None else 31
    return create_month_callback_for_days(days) or None, None, vendor_id


def renew_job_args_from_order(
    session: Session, order: CustomerOrder
) -> tuple[str, str | None]:
    """
    Return (plan_mode, duration_callback) for a paid renew order.
    Legacy renew orders without plan_mode default to current (quick renew).
    """
    mode = (order.plan_mode or "current").strip().lower()
    if mode != "change":
        return "current", None
    days: int | None = 31
    if order.plan_id:
        plan = get_price_plan(session, order.plan_id)
        if plan:
            # Keep 0 as unlimited — never coerce with `or 31`.
            days = int(plan.duration_days) if plan.duration_days is not None else 31
            if vendor_key_for_price_plan(plan) == "finearts" or days <= 0:
                return "current", None
    cb = renew_extend_callback_for_days(days)
    return "change", (cb or None)


def is_username_available_for_create(
    session: Session, username: str, telegram_id: int
) -> tuple[bool, str]:
    """Return (ok, reason_code). reason: ok|taken_sub|taken_other|open_order."""
    name = (username or "").strip()
    if not name:
        return False, "invalid"
    # Global usable inventory occupancy (any vendor); removed/deleted do not block.
    if not is_usable_username_available(session, name):
        return False, "taken_sub"
    link = session.scalar(
        select(CustomerServiceLink).where(
            CustomerServiceLink.service_username == name
        )
    )
    if link and link.telegram_id != telegram_id:
        return False, "taken_other"
    open_order = session.scalar(
        select(CustomerOrder)
        .where(
            CustomerOrder.service_username == name,
            CustomerOrder.kind == "create",
            CustomerOrder.status.in_(
                (
                    "awaiting_payment",
                    "receipt_window",
                    "awaiting_review",
                    "approved",
                    "fulfilling",
                )
            ),
            CustomerOrder.telegram_id != telegram_id,
        )
        .limit(1)
    )
    if open_order:
        return False, "open_order"
    return True, "ok"


def list_customer_orders_for_user(
    session: Session,
    telegram_id: int,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int = 20,
) -> list[CustomerOrder]:
    q = (
        select(CustomerOrder)
        .where(CustomerOrder.telegram_id == telegram_id)
        .order_by(CustomerOrder.id.desc())
        .limit(limit)
    )
    if statuses:
        q = q.where(CustomerOrder.status.in_(statuses))
    return list(session.scalars(q))


def find_open_wallet_charge(
    session: Session, telegram_id: int
) -> CustomerOrder | None:
    return session.scalar(
        select(CustomerOrder)
        .where(
            CustomerOrder.telegram_id == telegram_id,
            CustomerOrder.kind == "wallet",
            CustomerOrder.status.in_(
                ("awaiting_payment", "receipt_window", "awaiting_review")
            ),
        )
        .order_by(CustomerOrder.id.desc())
        .limit(1)
    )


def cancel_customer_order(
    session: Session, order: CustomerOrder, *, reason: str = "cancelled"
) -> int:
    """Cancel order, refund wallet applied, unlink create service. Returns refunded toman."""
    if order.status not in (
        "awaiting_payment",
        "receipt_window",
        "awaiting_review",
    ):
        return 0
    refunded = refund_wallet_applied(session, order)
    order.status = "cancelled"
    order.note = (order.note or "") + f"|{reason}"
    touch_customer_order(order)
    if order.kind == "create" and order.service_username:
        unlink_customer_service(session, order.telegram_id, order.service_username)
    return refunded


def order_payable_toman(order: CustomerOrder) -> int:
    applied = int(getattr(order, "wallet_applied_toman", 0) or 0)
    return max(0, int(order.price_toman or 0) - applied)


def set_order_payable_amount(
    order: CustomerOrder, session: Session | None = None
) -> None:
    """Set unique amount_rial from remaining payable toman (collision-safe)."""
    payable = order_payable_toman(order)
    if payable <= 0:
        order.amount_rial = "0"
        return
    base = payable * 10
    used: set[str] = set()
    if session is not None:
        rows = session.scalars(
            select(CustomerOrder.amount_rial).where(
                CustomerOrder.status.in_(
                    ("awaiting_payment", "receipt_window", "awaiting_review")
                ),
                CustomerOrder.amount_rial.is_not(None),
                CustomerOrder.id != order.id,
            )
        )
        used = {str(a) for a in rows if a}
    # Prefer stable-ish suffix from id, then scan for a free slot
    preferred = 100 + (int(order.id or 0) % 900)
    for offset in range(900):
        suffix = 100 + ((preferred - 100 + offset) % 900)
        candidate = str(base + suffix)
        if candidate not in used:
            order.amount_rial = candidate
            return
    # Extremely busy: append order id bits beyond 3 digits
    order.amount_rial = str(base + 100 + (int(order.id or 0) % 100000))


def get_invoice_description(session: Session) -> str:
    raw = (get_state(session, "payment_invoice_description") or "").strip()
    return raw or DEFAULT_INVOICE_DESCRIPTION


def set_invoice_description(session: Session, text: str) -> str:
    cleaned = " ".join((text or "").split()).strip()[:200]
    if not cleaned:
        cleaned = DEFAULT_INVOICE_DESCRIPTION
    set_state(session, "payment_invoice_description", cleaned)
    return cleaned


def format_invoice_description(
    session: Session, *, order_id: int, kind: str = "", username: str = ""
) -> str:
    """Build gateway invoice description from admin template.

    Placeholders (optional): {order} {kind} {user}
    kind becomes خرید / تمدید.
    """
    template = get_invoice_description(session)
    kind_fa = "خرید" if kind == "create" else ("تمدید" if kind == "renew" else kind)
    text = (
        template.replace("{order}", str(order_id))
        .replace("{kind}", kind_fa)
        .replace("{user}", username or "")
    )
    text = " ".join(text.split()).strip()
    if "{order}" not in template:
        text = f"{text} #{order_id}".strip()
    return text[:200]


def create_customer_order(
    session: Session,
    *,
    telegram_id: int,
    kind: str,
    plan_id: int | None,
    service_username: str,
    price_toman: int,
    plan_mode: str | None = None,
) -> CustomerOrder:
    now = datetime.utcnow()
    ttl = get_order_ttl_seconds(session)
    order = CustomerOrder(
        telegram_id=telegram_id,
        kind=kind,
        plan_id=plan_id,
        plan_mode=plan_mode,
        service_username=service_username.strip(),
        price_toman=price_toman,
        status="awaiting_payment",
        instructions_at=now,
        expires_at=now + timedelta(seconds=ttl),
        reminder_count=0,
    )
    order.wallet_applied_toman = 0
    session.add(order)
    session.flush()
    set_order_payable_amount(order, session)
    session.flush()
    return order


def find_customer_order_by_renew_job(
    session: Session, renew_job_id: int
) -> CustomerOrder | None:
    return session.scalar(
        select(CustomerOrder)
        .where(CustomerOrder.renew_job_id == renew_job_id)
        .order_by(CustomerOrder.id.desc())
        .limit(1)
    )


def find_customer_order_for_job(
    session: Session,
    *,
    renew_job_id: int,
    username: str | None = None,
) -> CustomerOrder | None:
    """Resolve customer order for a vendor job.

    Prefer exact renew_job_id link; if admin cancelled/retried and the order still
    points at an old job, fall back to an open fulfilling order for the username.
    """
    order = find_customer_order_by_renew_job(session, renew_job_id)
    if order:
        return order
    name = (username or "").strip()
    if not name:
        return None
    order = session.scalar(
        select(CustomerOrder)
        .where(
            CustomerOrder.service_username == name,
            CustomerOrder.kind.in_(("renew", "create")),
            CustomerOrder.status.in_(("fulfilling", "approved", "awaiting_review")),
        )
        .order_by(CustomerOrder.id.desc())
        .limit(1)
    )
    if order:
        # Relink so later events hit the new job id.
        order.renew_job_id = renew_job_id
        touch_customer_order(order)
    return order


def relink_customer_order_renew_job(
    session: Session, *, old_job_id: int, new_job_id: int
) -> CustomerOrder | None:
    order = find_customer_order_by_renew_job(session, old_job_id)
    if not order:
        return None
    order.renew_job_id = new_job_id
    if order.status in ("awaiting_review", "approved"):
        order.status = "fulfilling"
    touch_customer_order(order)
    return order


def find_customer_order_by_amount(
    session: Session, amount_rial: str | int
) -> CustomerOrder | None:
    amount = str(amount_rial).replace(",", "").replace(" ", "").strip()
    if not amount:
        return None
    return session.scalar(
        select(CustomerOrder)
        .where(
            CustomerOrder.amount_rial == amount,
            CustomerOrder.status.in_(
                ("awaiting_payment", "receipt_window", "awaiting_review")
            ),
        )
        .order_by(CustomerOrder.id.desc())
        .limit(1)
    )


def list_customer_orders_needing_receipt_nudge(
    session: Session, older_than_seconds: float
) -> list[CustomerOrder]:
    """Legacy one-shot helper — prefer list_customer_orders_due_reminder."""
    delays = get_reminder_delays_seconds(session)
    delay = delays[0] if delays else older_than_seconds
    return [
        o
        for o in list_customer_orders_due_reminder(session)
        if int(getattr(o, "reminder_count", 0) or 0) == 0
        and o.instructions_at
        and (datetime.utcnow() - o.instructions_at).total_seconds() >= delay
    ]


DEFAULT_CUSTOMER_BOT_DISABLED_MESSAGE = (
    "🛠 ربات موقتاً در حال به‌روزرسانی است.\n"
    "لطفاً کمی بعد دوباره تلاش کنید."
)


def is_customer_bot_enabled(session: Session) -> bool:
    raw = (get_state(session, "customer_bot_enabled") or "1").strip().lower()
    return raw not in {"0", "false", "off", "no", "disabled"}


def get_customer_bot_disabled_message(session: Session) -> str:
    raw = (get_state(session, "customer_bot_disabled_message") or "").strip()
    return raw or DEFAULT_CUSTOMER_BOT_DISABLED_MESSAGE


def set_customer_bot_maintenance(
    session: Session, *, enabled: bool, message: str | None = None
) -> None:
    set_state(session, "customer_bot_enabled", "1" if enabled else "0")
    if message is not None:
        text = (message or "").strip() or DEFAULT_CUSTOMER_BOT_DISABLED_MESSAGE
        set_state(session, "customer_bot_disabled_message", text)


def get_order_ttl_seconds(session: Session) -> int:
    raw = (get_state(session, "order_ttl_seconds") or "").strip()
    try:
        val = int(raw) if raw else DEFAULT_ORDER_TTL_SECONDS
    except ValueError:
        val = DEFAULT_ORDER_TTL_SECONDS
    return max(60, val)


def set_order_ttl_seconds(session: Session, seconds: int) -> int:
    val = max(60, int(seconds))
    set_state(session, "order_ttl_seconds", str(val))
    return val


def get_userinfo_ttl_seconds(session: Session) -> int:
    """TTL for HTTP subscription-userinfo refresh from stored URL (not NetPlus bot)."""
    raw = (get_state(session, "userinfo_ttl_seconds") or "").strip()
    try:
        val = int(raw) if raw else DEFAULT_USERINFO_TTL_SECONDS
    except ValueError:
        val = DEFAULT_USERINFO_TTL_SECONDS
    return max(0, val)


def set_userinfo_ttl_seconds(session: Session, seconds: int) -> int:
    val = max(0, int(seconds))
    set_state(session, "userinfo_ttl_seconds", str(val))
    return val


def get_alert_days_threshold(session: Session) -> int:
    raw = (get_state(session, "alert_days_threshold") or "").strip()
    try:
        val = int(raw) if raw else DEFAULT_ALERT_DAYS_THRESHOLD
    except ValueError:
        val = DEFAULT_ALERT_DAYS_THRESHOLD
    return max(0, val)


def set_alert_days_threshold(session: Session, days: int) -> int:
    val = max(0, int(days))
    set_state(session, "alert_days_threshold", str(val))
    return val


def get_alert_traffic_mb_threshold(session: Session) -> float:
    raw = (get_state(session, "alert_traffic_mb_threshold") or "").strip()
    try:
        val = float(raw) if raw else DEFAULT_ALERT_TRAFFIC_MB_THRESHOLD
    except ValueError:
        val = DEFAULT_ALERT_TRAFFIC_MB_THRESHOLD
    return max(0.0, val)


def set_alert_traffic_mb_threshold(session: Session, mb: float) -> float:
    val = max(0.0, float(mb))
    set_state(session, "alert_traffic_mb_threshold", str(val))
    return val


def get_lifecycle_alert_cooldown_seconds(session: Session) -> int:
    raw = (get_state(session, "lifecycle_alert_cooldown_seconds") or "").strip()
    try:
        val = int(raw) if raw else DEFAULT_LIFECYCLE_ALERT_COOLDOWN_SECONDS
    except ValueError:
        val = DEFAULT_LIFECYCLE_ALERT_COOLDOWN_SECONDS
    return max(60, val)


def set_lifecycle_alert_cooldown_seconds(session: Session, seconds: int) -> int:
    val = max(60, int(seconds))
    set_state(session, "lifecycle_alert_cooldown_seconds", str(val))
    return val


def get_lifecycle_scan_interval_seconds(session: Session) -> int:
    raw = (get_state(session, "lifecycle_scan_interval_seconds") or "").strip()
    try:
        val = int(raw) if raw else DEFAULT_LIFECYCLE_SCAN_INTERVAL_SECONDS
    except ValueError:
        val = DEFAULT_LIFECYCLE_SCAN_INTERVAL_SECONDS
    return max(60, val)


def set_lifecycle_scan_interval_seconds(session: Session, seconds: int) -> int:
    val = max(60, int(seconds))
    set_state(session, "lifecycle_scan_interval_seconds", str(val))
    return val


def get_admin_vendor_alert_max_repeats(session: Session) -> int:
    raw = (get_state(session, "admin_vendor_alert_max_repeats") or "").strip()
    try:
        val = int(raw) if raw else DEFAULT_ADMIN_VENDOR_ALERT_MAX_REPEATS
    except ValueError:
        val = DEFAULT_ADMIN_VENDOR_ALERT_MAX_REPEATS
    return max(1, val)


def set_admin_vendor_alert_max_repeats(session: Session, n: int) -> int:
    val = max(1, int(n))
    set_state(session, "admin_vendor_alert_max_repeats", str(val))
    return val


def get_admin_vendor_alert_window_hours(session: Session) -> int:
    raw = (get_state(session, "admin_vendor_alert_window_hours") or "").strip()
    try:
        val = int(raw) if raw else DEFAULT_ADMIN_VENDOR_ALERT_WINDOW_HOURS
    except ValueError:
        val = DEFAULT_ADMIN_VENDOR_ALERT_WINDOW_HOURS
    return max(1, val)


def set_admin_vendor_alert_window_hours(session: Session, hours: int) -> int:
    val = max(1, int(hours))
    set_state(session, "admin_vendor_alert_window_hours", str(val))
    return val


def get_admin_vendor_deleted_suppress_hours(session: Session) -> int:
    raw = (get_state(session, "admin_vendor_deleted_suppress_hours") or "").strip()
    try:
        val = int(raw) if raw else DEFAULT_ADMIN_VENDOR_DELETED_SUPPRESS_HOURS
    except ValueError:
        val = DEFAULT_ADMIN_VENDOR_DELETED_SUPPRESS_HOURS
    return max(1, val)


def set_admin_vendor_deleted_suppress_hours(session: Session, hours: int) -> int:
    val = max(1, int(hours))
    set_state(session, "admin_vendor_deleted_suppress_hours", str(val))
    return val


def is_orders_manual_approval(session: Session) -> bool:
    raw = (get_state(session, "orders_manual_approval") or "").strip().lower()
    if not raw:
        return DEFAULT_ORDERS_MANUAL_APPROVAL
    return raw in {"1", "true", "yes", "on", "manual"}


def set_orders_manual_approval(session: Session, enabled: bool) -> bool:
    val = bool(enabled)
    set_state(session, "orders_manual_approval", "1" if val else "0")
    return val


def get_vendor_settle_mode(session: Session) -> str:
    raw = (get_state(session, "vendor_settle_mode") or "").strip().lower()
    if raw in {"wallet", "card"}:
        return raw
    return DEFAULT_VENDOR_SETTLE_MODE


def set_vendor_settle_mode(session: Session, mode: str) -> str:
    val = (mode or "").strip().lower()
    if val not in {"wallet", "card"}:
        val = DEFAULT_VENDOR_SETTLE_MODE
    set_state(session, "vendor_settle_mode", val)
    return val


def is_direct_vendor_card_pay_global(session: Session) -> bool:
    raw = (get_state(session, "direct_vendor_card_pay") or "").strip().lower()
    if not raw:
        return DEFAULT_DIRECT_VENDOR_CARD_PAY
    return raw in {"1", "true", "yes", "on"}


def set_direct_vendor_card_pay_global(session: Session, enabled: bool) -> bool:
    val = bool(enabled)
    set_state(session, "direct_vendor_card_pay", "1" if val else "0")
    return val


def get_direct_vendor_card_usernames(session: Session) -> list[str]:
    raw = (get_state(session, "direct_vendor_card_usernames") or "").strip()
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.replace(";", ",").replace("\n", ",").split(","):
        u = part.strip().lstrip("@")
        if not u:
            continue
        key = u.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def set_direct_vendor_card_usernames(session: Session, raw: str | list[str]) -> list[str]:
    if isinstance(raw, list):
        text = ",".join(str(x) for x in raw)
    else:
        text = str(raw or "")
    # Normalize via getter path
    set_state(session, "direct_vendor_card_usernames", text)
    return get_direct_vendor_card_usernames(session)


def is_direct_vendor_card_for_username(session: Session, username: str | None) -> bool:
    """Global on → all create/renew card pays; else only listed usernames."""
    uname = (username or "").strip().lstrip("@")
    if is_direct_vendor_card_pay_global(session):
        return True
    if not uname:
        return False
    want = uname.lower()
    return any(u.lower() == want for u in get_direct_vendor_card_usernames(session))


def order_is_vendor_direct(order) -> bool:
    if not order:
        return False
    if (order.pay_source or "").strip() == "vendor_direct":
        return True
    note = order.note or ""
    return "|vendor_direct" in note or note.startswith("vendor_direct")


def toggle_direct_vendor_card_username(session: Session, username: str) -> bool:
    """Add/remove one service username from direct-vendor list. Returns True if now listed."""
    uname = (username or "").strip().lstrip("@")
    if not uname:
        return False
    cur = get_direct_vendor_card_usernames(session)
    lower = {u.lower(): u for u in cur}
    key = uname.lower()
    if key in lower:
        cur = [u for u in cur if u.lower() != key]
        set_direct_vendor_card_usernames(session, cur)
        return False
    cur.append(uname)
    set_direct_vendor_card_usernames(session, cur)
    return True


RATE_TIERS = ("default", "friends", "colleagues")
RATE_TIER_LABELS = {
    "default": "عادی",
    "friends": "دوستان",
    "colleagues": "همکاران",
}


def normalize_rate_tier(tier: str | None) -> str:
    t = (tier or "default").strip().lower()
    if t in {"friend", "friends"}:
        return "friends"
    if t in {"colleague", "colleagues", "coworker", "coworkers"}:
        return "colleagues"
    if t in RATE_TIERS:
        return t
    return "default"


def get_customer_rate_tier(session: Session, telegram_id: int) -> str:
    row = get_customer_profile(session, telegram_id)
    if not row:
        return "default"
    return normalize_rate_tier(getattr(row, "rate_tier", None))


def set_customer_rate_tier(
    session: Session, telegram_id: int, tier: str
) -> CustomerProfile:
    row = upsert_customer_profile(session, telegram_id)
    row.rate_tier = normalize_rate_tier(tier)
    row.updated_at = datetime.utcnow()
    session.flush()
    return row


def list_customers_by_rate_tier(
    session: Session, tier: str
) -> list[CustomerProfile]:
    want = normalize_rate_tier(tier)
    if want == "default":
        return list(
            session.scalars(
                select(CustomerProfile)
                .where(
                    or_(
                        CustomerProfile.rate_tier.is_(None),
                        CustomerProfile.rate_tier == "",
                        CustomerProfile.rate_tier == "default",
                    )
                )
                .order_by(CustomerProfile.id.desc())
            )
        )
    return list(
        session.scalars(
            select(CustomerProfile)
            .where(CustomerProfile.rate_tier == want)
            .order_by(CustomerProfile.id.desc())
        )
    )


def sell_price_toman(plan: PricePlan | None, tier: str | None = "default") -> int:
    if plan is None:
        return 0
    t = normalize_rate_tier(tier)
    if t == "friends":
        special = getattr(plan, "price_friends_toman", None)
        if special is not None and int(special) > 0:
            return int(special)
    elif t == "colleagues":
        special = getattr(plan, "price_colleagues_toman", None)
        if special is not None and int(special) > 0:
            return int(special)
    return int(plan.price_toman or 0)


def sell_price_for_customer(
    session: Session, plan: PricePlan | None, telegram_id: int
) -> int:
    return sell_price_toman(plan, get_customer_rate_tier(session, telegram_id))


def set_plan_tier_price(
    session: Session,
    plan_id: int,
    *,
    tier: str,
    price_toman: int | None,
) -> PricePlan | None:
    plan = session.get(PricePlan, plan_id)
    if not plan:
        return None
    t = normalize_rate_tier(tier)
    val = None if price_toman is None else max(0, int(price_toman))
    if t == "friends":
        plan.price_friends_toman = val
    elif t == "colleagues":
        plan.price_colleagues_toman = val
    else:
        plan.price_toman = int(val or 0)
    plan.updated_at = datetime.utcnow()
    session.flush()
    return plan


def get_referral_percent(session: Session) -> int:
    raw = (get_state(session, "referral_commission_percent") or "").strip()
    try:
        pct = int(raw)
    except ValueError:
        pct = 0
    return max(0, min(100, pct))


def set_referral_percent(session: Session, percent: int) -> int:
    pct = max(0, min(100, int(percent)))
    set_state(session, "referral_commission_percent", str(pct))
    return pct


def set_referred_by_if_empty(
    session: Session, telegram_id: int, referrer_telegram_id: int
) -> bool:
    """Bind referrer once (cannot overwrite). Returns True if newly set."""
    if int(telegram_id) == int(referrer_telegram_id):
        return False
    row = upsert_customer_profile(session, telegram_id)
    if getattr(row, "referred_by_telegram_id", None):
        return False
    # Ensure referrer profile exists
    upsert_customer_profile(session, referrer_telegram_id)
    row.referred_by_telegram_id = int(referrer_telegram_id)
    row.updated_at = datetime.utcnow()
    session.flush()
    return True


def apply_referral_credit_for_order(session: Session, order: CustomerOrder) -> dict:
    """Credit referrer wallet with configured % of paid create/renew. Idempotent per order."""
    if not order or order.kind not in ("create", "renew"):
        return {"ok": False, "reason": "kind"}
    note = order.note or ""
    if "|referral_paid" in note:
        return {"ok": False, "reason": "already"}
    pct = get_referral_percent(session)
    if pct <= 0:
        return {"ok": False, "reason": "pct_zero"}
    buyer = get_customer_profile(session, order.telegram_id)
    ref_id = getattr(buyer, "referred_by_telegram_id", None) if buyer else None
    if not ref_id:
        return {"ok": False, "reason": "no_referrer"}
    if int(ref_id) == int(order.telegram_id):
        return {"ok": False, "reason": "self"}
    base = max(0, int(order.price_toman or 0) - int(order.wallet_applied_toman or 0))
    if base <= 0:
        base = max(0, int(order.price_toman or 0))
    amount = int(base * pct / 100)
    if amount <= 0:
        return {"ok": False, "reason": "amount_zero"}
    new_bal = credit_wallet(
        session,
        int(ref_id),
        amount,
        kind="referral",
        order_id=order.id,
        note=f"referral:{order.telegram_id}:{pct}%",
    )
    order.note = (note + "|referral_paid").strip("|")
    touch_customer_order(order)
    return {
        "ok": True,
        "referrer_id": int(ref_id),
        "amount": amount,
        "balance": new_bal,
        "percent": pct,
        "buyer_id": int(order.telegram_id),
    }


def _invite_store(session: Session) -> dict[str, Any]:
    raw = (get_state(session, "rate_tier_invites") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_invite_store(session: Session, data: dict[str, Any]) -> None:
    set_state(session, "rate_tier_invites", json.dumps(data, ensure_ascii=False))


def create_rate_tier_invite(
    session: Session, tier: str, *, created_by: int | None = None
) -> str:
    import secrets

    t = normalize_rate_tier(tier)
    if t == "default":
        t = "friends"
    token = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    store = _invite_store(session)
    store[token] = {
        "tier": t,
        "created_at": datetime.utcnow().isoformat(),
        "created_by": created_by,
        "used": False,
    }
    _save_invite_store(session, store)
    return token


def consume_rate_tier_invite(
    session: Session, token: str, telegram_id: int
) -> str | None:
    """One-time invite → set rate_tier. Returns applied tier or None."""
    tok = (token or "").strip()
    if not tok:
        return None
    store = _invite_store(session)
    row = store.get(tok)
    if not isinstance(row, dict) or row.get("used"):
        return None
    tier = normalize_rate_tier(str(row.get("tier") or "friends"))
    set_customer_rate_tier(session, telegram_id, tier)
    row["used"] = True
    row["used_by"] = int(telegram_id)
    row["used_at"] = datetime.utcnow().isoformat()
    store[tok] = row
    _save_invite_store(session, store)
    return tier


def search_customer_profiles_page(
    session: Session,
    *,
    query: str | None = None,
    page: int = 0,
    page_size: int = 10,
) -> tuple[list[CustomerProfile], int]:
    filters = []
    raw = (query or "").strip().lstrip("@")
    if raw:
        like = f"%{raw}%"
        clauses = [
            CustomerProfile.username.ilike(like),
            CustomerProfile.full_name.ilike(like),
        ]
        if raw.isdigit():
            clauses.append(CustomerProfile.telegram_id == int(raw))
        filters.append(or_(*clauses))
    count_q = select(func.count()).select_from(CustomerProfile)
    list_q = select(CustomerProfile)
    for f in filters:
        count_q = count_q.where(f)
        list_q = list_q.where(f)
    total = session.scalar(count_q) or 0
    rows = list(
        session.scalars(
            list_q.order_by(CustomerProfile.id.desc())
            .offset(max(0, page) * page_size)
            .limit(page_size)
        )
    )
    return rows, int(total)


def list_usable_usernames_with_owners_page(
    session: Session,
    *,
    query: str | None = None,
    page: int = 0,
    page_size: int = 10,
) -> tuple[list[dict[str, Any]], int]:
    """Usable inventory usernames + owner display name for admin pickers."""
    q = (
        select(Subscription, CustomerServiceLink, CustomerProfile)
        .outerjoin(
            CustomerServiceLink,
            or_(
                CustomerServiceLink.subscription_id == Subscription.id,
                CustomerServiceLink.service_username == Subscription.username,
            ),
        )
        .outerjoin(
            CustomerProfile,
            CustomerProfile.telegram_id == CustomerServiceLink.telegram_id,
        )
        .where(Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)))
    )
    raw = (query or "").strip().lstrip("@")
    if raw:
        like = f"%{raw}%"
        clauses = [
            Subscription.username.ilike(like),
            CustomerProfile.username.ilike(like),
            CustomerProfile.full_name.ilike(like),
        ]
        if raw.isdigit():
            clauses.append(CustomerServiceLink.telegram_id == int(raw))
        q = q.where(or_(*clauses))
    # Deduplicate by username (keep newest sub id)
    rows = list(
        session.execute(q.order_by(Subscription.id.desc()).limit(2000)).all()
    )
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for sub, link, profile in rows:
        uname = (sub.username or "").strip()
        if not uname or uname.lower() in seen:
            continue
        seen.add(uname.lower())
        owner_name = None
        owner_tg = None
        if profile is not None:
            owner_tg = profile.telegram_id
            owner_name = (
                (f"@{profile.username}" if profile.username else None)
                or profile.full_name
                or str(profile.telegram_id)
            )
        elif link is not None:
            owner_tg = link.telegram_id
            owner_name = str(link.telegram_id)
        items.append(
            {
                "username": uname,
                "subscription_id": sub.id,
                "owner_telegram_id": owner_tg,
                "owner_label": owner_name or "—",
            }
        )
    total = len(items)
    start = max(0, page) * page_size
    return items[start : start + page_size], total


def match_pending_vendor_direct_by_amount(
    session: Session, amount_rial: str | int | None
):
    """Find open create/renew order waiting on direct vendor card pay."""
    if amount_rial is None:
        return None
    amount = str(amount_rial).replace(",", "").replace(" ", "").strip()
    if not amount:
        return None
    rows = session.scalars(
        select(CustomerOrder)
        .where(
            CustomerOrder.kind.in_(("create", "renew")),
            CustomerOrder.status.in_(
                ("awaiting_payment", "receipt_window", "awaiting_review")
            ),
            CustomerOrder.amount_rial == amount,
        )
        .order_by(CustomerOrder.id.desc())
    ).all()
    for order in rows:
        if order_is_vendor_direct(order):
            return order
    # Also match toman*10 against payable when amount_rial was set from NetPlus
    try:
        rial = int(amount)
    except ValueError:
        return None
    if rial % 10 != 0:
        return None
    toman = rial // 10
    rows = session.scalars(
        select(CustomerOrder)
        .where(
            CustomerOrder.kind.in_(("create", "renew")),
            CustomerOrder.status.in_(
                ("awaiting_payment", "receipt_window", "awaiting_review")
            ),
        )
        .order_by(CustomerOrder.id.desc())
        .limit(40)
    ).all()
    for order in rows:
        if not order_is_vendor_direct(order):
            continue
        payable = order_payable_toman(order)
        if payable == toman:
            return order
    return None


def _incoming_username_match(parsed_json: str | None, username: str) -> bool:
    uname = (username or "").strip()
    if not uname:
        return False
    try:
        data = json.loads(parsed_json or "") if parsed_json else {}
    except (json.JSONDecodeError, TypeError):
        return False
    return str((data or {}).get("username") or "").strip() == uname


def count_notified_vendor_ending_alerts(
    session: Session,
    username: str,
    *,
    within_hours: int,
    exclude_message_id: int | None = None,
) -> int:
    """How many ending alerts (days/mb) already notified for this username."""
    uname = (username or "").strip()
    if not uname:
        return 0
    since = datetime.utcnow() - timedelta(hours=max(1, int(within_hours)))
    rows = list(
        session.scalars(
            select(IncomingMessage)
            .where(
                IncomingMessage.kind.in_(("alert_days", "alert_mb")),
                IncomingMessage.notified.is_(True),
                IncomingMessage.received_at >= since,
                IncomingMessage.parsed_json.contains(uname),
            )
            .order_by(IncomingMessage.received_at.desc())
            .limit(200)
        )
    )
    n = 0
    for msg in rows:
        if exclude_message_id is not None and int(msg.id) == int(exclude_message_id):
            continue
        if _incoming_username_match(msg.parsed_json, uname):
            n += 1
    return n


def vendor_username_deleted_recently(
    session: Session, username: str, *, within_hours: int
) -> bool:
    """True if vendor sent alert_deleted for this username within the window."""
    uname = (username or "").strip()
    if not uname:
        return False
    since = datetime.utcnow() - timedelta(hours=max(1, int(within_hours)))
    rows = list(
        session.scalars(
            select(IncomingMessage)
            .where(
                IncomingMessage.kind == "alert_deleted",
                IncomingMessage.received_at >= since,
                IncomingMessage.parsed_json.contains(uname),
            )
            .order_by(IncomingMessage.received_at.desc())
            .limit(40)
        )
    )
    for msg in rows:
        if _incoming_username_match(msg.parsed_json, uname):
            return True
    return False


def should_notify_admin_vendor_ending_alert(
    session: Session,
    *,
    username: str,
    message_id: int | None = None,
) -> bool:
    """Throttle ending-service admin alerts; hush if vendor deleted recently."""
    uname = (username or "").strip()
    if not uname:
        return True
    suppress_h = get_admin_vendor_deleted_suppress_hours(session)
    if vendor_username_deleted_recently(session, uname, within_hours=suppress_h):
        return False
    max_n = get_admin_vendor_alert_max_repeats(session)
    window_h = get_admin_vendor_alert_window_hours(session)
    already = count_notified_vendor_ending_alerts(
        session,
        uname,
        within_hours=window_h,
        exclude_message_id=message_id,
    )
    return already < max_n


def get_reminder_delays_seconds(session: Session) -> list[int]:
    raw = (get_state(session, "order_reminder_delays_seconds") or "").strip()
    if not raw:
        return list(DEFAULT_REMINDER_DELAYS_SECONDS)
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            sec = int(part)
        except ValueError:
            continue
        if sec > 0:
            out.append(sec)
    return out or list(DEFAULT_REMINDER_DELAYS_SECONDS)


def set_reminder_delays_seconds(session: Session, delays: list[int]) -> list[int]:
    cleaned = [max(1, int(d)) for d in delays if int(d) > 0]
    if not cleaned:
        cleaned = list(DEFAULT_REMINDER_DELAYS_SECONDS)
    set_state(
        session,
        "order_reminder_delays_seconds",
        ",".join(str(d) for d in cleaned),
    )
    return cleaned


def list_customer_orders_due_reminder(session: Session) -> list[CustomerOrder]:
    """Orders that need the next payment reminder (idempotent by reminder_count)."""
    delays = get_reminder_delays_seconds(session)
    if not delays:
        return []
    now = datetime.utcnow()
    rows = list(
        session.scalars(
            select(CustomerOrder).where(
                CustomerOrder.status.in_(("awaiting_payment", "receipt_window")),
                CustomerOrder.instructions_at.is_not(None),
            )
        )
    )
    due: list[CustomerOrder] = []
    for o in rows:
        if o.expires_at and o.expires_at <= now:
            continue
        count = int(getattr(o, "reminder_count", 0) or 0)
        if count >= len(delays):
            continue
        # Migrate legacy one-shot flag into count
        if count == 0 and o.receipt_reminded_at is not None:
            o.reminder_count = 1
            o.last_reminder_at = o.receipt_reminded_at
            touch_customer_order(o)
            count = 1
            if count >= len(delays):
                continue
        age = (now - o.instructions_at).total_seconds() if o.instructions_at else 0
        if age >= delays[count]:
            due.append(o)
    return due


def list_customer_orders_to_expire(session: Session) -> list[CustomerOrder]:
    now = datetime.utcnow()
    return list(
        session.scalars(
            select(CustomerOrder).where(
                CustomerOrder.status.in_(("awaiting_payment", "receipt_window")),
                CustomerOrder.expires_at.is_not(None),
                CustomerOrder.expires_at <= now,
            )
        )
    )


def list_customer_orders_by_statuses(
    session: Session,
    statuses: tuple[str, ...] | list[str],
    *,
    limit: int = 30,
    include_hidden: bool = False,
) -> list[CustomerOrder]:
    q = select(CustomerOrder).where(CustomerOrder.status.in_(tuple(statuses)))
    if not include_hidden:
        q = q.where(
            or_(
                CustomerOrder.admin_hidden.is_(False),
                CustomerOrder.admin_hidden.is_(None),
            )
        )
    return list(
        session.scalars(q.order_by(CustomerOrder.id.desc()).limit(limit))
    )


def hide_admin_orders(
    session: Session, *, statuses: tuple[str, ...] | None = None
) -> int:
    """Hide orders from admin request lists (optionally only given statuses)."""
    q = select(CustomerOrder).where(
        or_(CustomerOrder.admin_hidden.is_(False), CustomerOrder.admin_hidden.is_(None))
    )
    if statuses:
        q = q.where(CustomerOrder.status.in_(tuple(statuses)))
    rows = list(session.scalars(q))
    for o in rows:
        o.admin_hidden = True
        touch_customer_order(o)
    session.flush()
    return len(rows)


def hide_admin_order(session: Session, order_id: int) -> bool:
    order = session.get(CustomerOrder, order_id)
    if not order:
        return False
    order.admin_hidden = True
    touch_customer_order(order)
    session.flush()
    return True


def list_customer_orders_pending_variz(session: Session) -> list[CustomerOrder]:
    return list(
        session.scalars(
            select(CustomerOrder)
            .where(
                CustomerOrder.status.in_(("awaiting_payment", "receipt_window")),
                CustomerOrder.variz_payment_id.is_not(None),
            )
            .order_by(CustomerOrder.id.asc())
        )
    )


def list_customer_orders_pending_gateway(session: Session) -> list[CustomerOrder]:
    return list(
        session.scalars(
            select(CustomerOrder)
            .where(
                CustomerOrder.status.in_(("awaiting_payment", "receipt_window")),
                CustomerOrder.gateway_invoice_id.is_not(None),
            )
            .order_by(CustomerOrder.id.asc())
        )
    )


def get_customer_order(session: Session, order_id: int) -> CustomerOrder | None:
    return session.get(CustomerOrder, order_id)


def list_open_customer_orders(session: Session, telegram_id: int) -> list[CustomerOrder]:
    return list(
        session.scalars(
            select(CustomerOrder)
            .where(
                CustomerOrder.telegram_id == telegram_id,
                CustomerOrder.status.in_(
                    (
                        "awaiting_payment",
                        "receipt_window",
                        "awaiting_review",
                        "approved",
                        "fulfilling",
                    )
                ),
            )
            .order_by(CustomerOrder.id.desc())
        )
    )


def touch_customer_order(order: CustomerOrder) -> None:
    order.updated_at = datetime.utcnow()


def get_subscription(session: Session, subscription_id: int) -> Subscription | None:
    """Exact inventory row by primary key."""
    return session.get(Subscription, subscription_id)


def get_subscription_by_username(session: Session, username: str) -> Subscription | None:
    """Exact username match (not ILIKE). Newest row if historical duplicates exist."""
    name = (username or "").strip()
    if not name:
        return None
    return session.scalar(
        select(Subscription)
        .where(Subscription.username == name)
        .order_by(Subscription.id.desc())
    )


def set_subscription_product_from_plan(
    session: Session,
    *,
    username: str | None = None,
    subscription: Subscription | None = None,
    plan: PricePlan | None = None,
    plan_id: int | None = None,
) -> Subscription | None:
    """Stamp our catalog product_name from a sell plan (never vendor priced text)."""
    sub = subscription
    if sub is None and username:
        sub = get_subscription_by_username(session, username)
    if sub is None:
        return None
    if plan is None and plan_id is not None:
        plan = get_price_plan(session, plan_id)
    # Never overwrite a NetPlus product with FineArts (or reverse) from a bad plan match.
    if plan is not None and not plan_matches_service_vendor(
        session, plan, username=username or sub.username, subscription=sub
    ):
        return sub
    label = local_product_name_from_plan(plan)
    if label:
        sub.product_name = label
        session.flush()
    return sub


def match_vendor_package_callback(
    packages: list[dict[str, str]], target_gb: float | int | None
) -> str | None:
    """Pick NetPlus package button callback by traffic GB in button text."""
    if target_gb is None or not packages:
        return None
    from app.utils.text import to_number

    n = int(round(float(target_gb)))
    needles = (
        f"{n}گیگ",
        f"{n}gb",
        f"{n}گیگابایت",
    )
    for pkg in packages:
        raw = (pkg.get("text") or "").replace(" ", "").replace("‌", "")
        # Normalize Persian/Arabic digits so ۲۵ گیگ matches 25.
        norm = raw.translate(
            str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
        ).lower()
        if any(needle in norm for needle in needles):
            cb = (pkg.get("callback_data") or "").strip()
            if cb:
                return cb
    # Fallback: first button whose leading number equals target GB
    for pkg in packages:
        raw = (pkg.get("text") or "").replace(" ", "").replace("‌", "")
        digits = ""
        for ch in raw.translate(
            str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
        ):
            if ch.isdigit():
                digits += ch
            elif digits:
                break
        if digits and to_number(digits) == float(n):
            cb = (pkg.get("callback_data") or "").strip()
            if cb:
                return cb
    return None


def create_renew_job(
    session: Session,
    *,
    username: str,
    vendor_service_id: str | None,
    subscription_id: int | None,
    plan_mode: str,
    requested_by: int | None,
    duration_callback: str | None = None,
    kind: str = "renew",
    package_callback: str | None = None,
    vendor_id: int | None = None,
    plan_id: int | None = None,
) -> RenewJob:
    resolved_vendor_id = resolve_vendor_id_for_job(
        session,
        username=username,
        subscription_id=subscription_id,
        vendor_id=vendor_id,
    )
    job = RenewJob(
        kind=kind,
        username=username,
        vendor_id=resolved_vendor_id,
        vendor_service_id=vendor_service_id,
        subscription_id=subscription_id,
        plan_mode=plan_mode,
        plan_id=int(plan_id) if plan_id else None,
        duration_callback=duration_callback,
        package_callback=package_callback,
        status="queued",
        requested_by=requested_by,
    )
    session.add(job)
    session.flush()
    # Close older open jobs for same username so admin isn't shown a stale card.
    cancel_open_renew_jobs_for_username(
        session,
        username,
        exclude_job_id=job.id,
        reason="superseded_by_new_job",
    )
    return job


def retry_renew_job(session: Session, old: RenewJob, requested_by: int | None) -> RenewJob:
    new = create_renew_job(
        session,
        username=old.username,
        vendor_service_id=old.vendor_service_id,
        subscription_id=old.subscription_id,
        plan_mode=old.plan_mode,
        requested_by=requested_by,
        duration_callback=old.duration_callback,
        kind=old.kind or "renew",
        package_callback=old.package_callback,
        vendor_id=old.vendor_id,
        plan_id=old.plan_id,
    )
    # Keep customer order attached across cancel/retry so success notifies the buyer.
    relink_customer_order_renew_job(
        session, old_job_id=old.id, new_job_id=new.id
    )
    return new


def list_recent_terminal_jobs(session: Session, limit: int = 8) -> list[RenewJob]:
    return list(
        session.scalars(
            select(RenewJob)
            .where(RenewJob.status.in_(("completed", "failed", "cancelled")))
            .order_by(RenewJob.id.desc())
            .limit(limit)
        )
    )


def get_renew_job(session: Session, job_id: int) -> RenewJob | None:
    return session.get(RenewJob, job_id)


def list_active_renew_jobs(session: Session, limit: int = 20) -> list[RenewJob]:
    return list(
        session.scalars(
            select(RenewJob)
            .where(RenewJob.status.in_(ACTIVE_RENEW_STATUSES))
            .order_by(RenewJob.id.asc())
            .limit(limit)
        )
    )


def panel_badge_counts(session: Session) -> dict[str, int]:
    """Sidebar / notification counts for items needing attention."""
    orders_n = session.scalar(
        select(func.count())
        .select_from(CustomerOrder)
        .where(
            CustomerOrder.status.in_(
                ("awaiting_review", "receipt_window", "awaiting_payment")
            )
        )
    )
    queue_n = session.scalar(
        select(func.count())
        .select_from(RenewJob)
        .where(RenewJob.status.in_(tuple(ACTIVE_RENEW_STATUSES)))
    )
    tickets_n = session.scalar(
        select(func.count())
        .select_from(SupportTicket)
        .where(SupportTicket.status.in_(("awaiting_admin", "open")))
    )
    _items, almost_n = list_local_almost_out(session, limit=500)
    _warn_rows, warn_n = list_warning_subscriptions(
        session, offset=0, limit=1, include_hidden=False
    )
    # total is returned as second value; limit=1 still computes full total
    return {
        "orders": int(orders_n or 0),
        "queue": int(queue_n or 0),
        "tickets": int(tickets_n or 0),
        "almost_out": int(almost_n or 0),
        "warnings": int(warn_n or 0),
    }


def next_queued_renew_job(session: Session) -> RenewJob | None:
    return session.scalar(
        select(RenewJob)
        .where(RenewJob.status == "queued")
        .order_by(RenewJob.id.asc())
        .limit(1)
    )


def get_driving_or_payment_job(session: Session) -> RenewJob | None:
    return session.scalar(
        select(RenewJob)
        .where(
            RenewJob.status.in_(
                (
                    "driving",
                    "awaiting_package",
                    "awaiting_payment",
                    "awaiting_auto_approve",
                    "receipt_window",
                    "awaiting_receipt_confirm",
                )
            )
        )
        .order_by(RenewJob.id.asc())
        .limit(1)
    )


def touch_renew_job(job: RenewJob) -> None:
    job.updated_at = datetime.utcnow()


def fail_renew_job(job: RenewJob, error: str) -> None:
    job.status = "failed"
    job.error = error[:2000]
    job.completed_at = datetime.utcnow()
    touch_renew_job(job)


def complete_renew_job(job: RenewJob) -> None:
    job.status = "completed"
    job.completed_at = datetime.utcnow()
    touch_renew_job(job)


def cancel_renew_job(job: RenewJob, reason: str = "cancelled") -> None:
    job.status = "cancelled"
    job.error = reason[:2000]
    job.completed_at = datetime.utcnow()
    touch_renew_job(job)


def clear_renew_queue(session: Session) -> dict[str, int]:
    """Cancel every active renew job, then delete all renew_jobs rows (empties queue UI)."""
    active = list_active_renew_jobs(session, limit=10_000)
    cancelled = 0
    for job in active:
        cancel_renew_job(job, "cleared_by_admin")
        cancelled += 1
    session.flush()
    session.execute(
        update(CustomerOrder)
        .where(CustomerOrder.renew_job_id.is_not(None))
        .values(renew_job_id=None)
    )
    total = int(
        session.scalar(select(func.count()).select_from(RenewJob)) or 0
    )
    session.execute(delete(RenewJob))
    session.flush()
    return {"cancelled_active": cancelled, "deleted_rows": total}


def renew_payment_message_already_used(
    session: Session, message_id: int | None, *, exclude_job_id: int | None = None
) -> RenewJob | None:
    """Return an earlier job that already stored this NetPlus payment message id."""
    if not message_id:
        return None
    q = select(RenewJob).where(RenewJob.payment_message_id == int(message_id))
    if exclude_job_id is not None:
        q = q.where(RenewJob.id != int(exclude_job_id))
    return session.scalar(q.order_by(RenewJob.id.desc()).limit(1))


def cancel_open_renew_jobs_for_username(
    session: Session, username: str, *, exclude_job_id: int | None = None, reason: str
) -> int:
    """Cancel leftover payment waits for the same username so a new renew cannot reuse them."""
    if not username:
        return 0
    rows = list(
        session.scalars(
            select(RenewJob).where(
                RenewJob.username == username,
                RenewJob.status.in_(
                    (
                        "awaiting_payment",
                        "awaiting_auto_approve",
                        "receipt_window",
                        "awaiting_receipt_confirm",
                        "queued",
                        "awaiting_package",
                    )
                ),
            )
        )
    )
    n = 0
    for job in rows:
        if exclude_job_id is not None and job.id == exclude_job_id:
            continue
        cancel_renew_job(job, reason)
        n += 1
    return n


def match_pending_renew_by_amount(session: Session, amount_rial: str | int | None) -> RenewJob | None:
    if amount_rial is None:
        return None
    amount = str(amount_rial).replace(",", "").strip()
    if not amount:
        return None
    return session.scalar(
        select(RenewJob)
        .where(
            RenewJob.amount_rial == amount,
            RenewJob.status.in_(
                (
                    "awaiting_payment",
                    "awaiting_auto_approve",
                    "receipt_window",
                    "awaiting_receipt_confirm",
                )
            ),
        )
        .order_by(RenewJob.id.desc())
        .limit(1)
    )


_PENDING_RENEW_STATUSES = (
    "awaiting_payment",
    "awaiting_auto_approve",
    "receipt_window",
    "awaiting_receipt_confirm",
    "driving",
)


def match_pending_renew_by_username(session: Session, username: str | None) -> RenewJob | None:
    """Match open renew/create job by vendor username.

    FineArts create often returns ``requested_digits`` (e.g. roodaki_mom_9021682)
    while the queue job still holds the requested base name (roodaki_mom).
    """
    want = (username or "").strip().lstrip("\u200e").lstrip("\u200f")
    if not want:
        return None
    job = session.scalar(
        select(RenewJob)
        .where(
            RenewJob.username == want,
            RenewJob.status.in_(_PENDING_RENEW_STATUSES),
        )
        .order_by(RenewJob.id.desc())
        .limit(1)
    )
    if job:
        return job

    # Prefix match for FineArts-style create suffix (_NNNNNNN).
    candidates = list(
        session.scalars(
            select(RenewJob)
            .where(
                RenewJob.status.in_(_PENDING_RENEW_STATUSES),
                RenewJob.kind == "create",
            )
            .order_by(RenewJob.id.desc())
            .limit(40)
        )
    )
    best: RenewJob | None = None
    best_len = -1
    for cand in candidates:
        base = (cand.username or "").strip().lstrip("\u200e").lstrip("\u200f")
        if not base or len(base) < 3:
            continue
        if want == base:
            return cand
        if want.startswith(base + "_"):
            suffix = want[len(base) + 1 :]
            if suffix.isdigit() and len(base) > best_len:
                best = cand
                best_len = len(base)
    if best is not None:
        return best

    # Late renew_ok after drive mistook operator-wallet settle for empty_message.
    recent_fail = session.scalar(
        select(RenewJob)
        .where(
            RenewJob.username == want,
            RenewJob.status == "failed",
            RenewJob.kind.in_(("renew", "create")),
        )
        .order_by(RenewJob.id.desc())
        .limit(1)
    )
    if not recent_fail or not recent_fail.completed_at:
        return None
    err = (recent_fail.error or "").lower()
    if err not in ("empty_message", "empty_before_confirm") and "unhandled" not in err:
        return None
    age = datetime.utcnow() - recent_fail.completed_at
    if age.total_seconds() > 15 * 60:
        return None
    return recent_fail


# ─── Bot messages / Education / Support tickets ───────────────────────────────


def ensure_default_bot_messages(session: Session) -> None:
    # Always refresh lifecycle alert templates so copy stays in sync with product UX.
    force_keys = {
        "alert_almost_out_days",
        "alert_almost_out_traffic",
        "alert_expired_days",
        "alert_volume_exhausted",
        "alert_service_deleted",
    }
    for key, text in DEFAULT_BOT_MESSAGES.items():
        row = session.get(BotMessage, key)
        if not row:
            session.add(BotMessage(key=key, text=text))
        elif key in force_keys:
            row.text = text
    session.flush()


def get_bot_message(session: Session, key: str, **fmt: Any) -> str:
    row = session.get(BotMessage, key)
    text = (row.text if row and row.text else DEFAULT_BOT_MESSAGES.get(key, key)) or key
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, ValueError, IndexError):
            return text
    return text


def set_bot_message(session: Session, key: str, text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        cleaned = DEFAULT_BOT_MESSAGES.get(key, "")
    row = session.get(BotMessage, key)
    if row:
        row.text = cleaned
        row.updated_at = datetime.utcnow()
    else:
        session.add(BotMessage(key=key, text=cleaned))
    session.flush()
    return cleaned


def list_bot_messages(session: Session) -> list[BotMessage]:
    ensure_default_bot_messages(session)
    return list(session.scalars(select(BotMessage).order_by(BotMessage.key.asc())))


EDU_ANDROID_INSTALL_TITLE = "📲 نصب سرویس"
EDU_ANDROID_INSTALL_BODY = (
    "برای دانلود آخرین نسخه V2rayNG اندروید وارد لینک زیر شوید👇👇\n"
    "https://t.me/fxonevpn/4\n\n"
    "برای راه‌اندازی اشتراک روی <b>اندروید</b> با برنامه "
    "<b>V2rayNG</b>، مراحل زیر را به ترتیب انجام دهید:\n\n"
    "1️⃣ لینک اشتراک را کپی کنید.\n"
    "2️⃣ وارد برنامه <b>V2rayNG</b> شوید.\n"
    "3️⃣ گزینه <b>➕</b> را انتخاب کنید.\n"
    "4️⃣ گزینه <b>دوم</b> را انتخاب کنید.\n"
    "5️⃣ یک‌بار برنامه را کامل ببندید و مجدد باز کنید.\n"
    "6️⃣ از منوی سه‌نقطهٔ بالای صفحه، گزینهٔ آخر را بزنید و "
    "<b>بروزرسانی</b> را انجام دهید.\n"
    "7️⃣ بعد از بروزرسانی، سرورها مشاهده خواهند شد.\n"
    "8️⃣ اگر سرورها بالا نیامدند، اینترنت را عوض کنید و دوباره "
    "بروزرسانی کنید.\n\n"
    "✨ پس از دیدن سرورها، یکی را انتخاب کنید و اتصال را روشن کنید."
)

EDU_ANDROID_UPDATE_TITLE = "🔄 به‌روزرسانی سرویس"
EDU_ANDROID_UPDATE_BODY = (
    "روش بروزرسانی اشتراک :\n"
    "1- فیلترشکن را خاموش کنید\n"
    "2- سپس در برنامه از طریق منوی سه نقطه بالای صفحه و گزینه آخر را انتخاب کنید\n"
    "3- بعد از بروزرسانی اخرین تغییرات را خواهید دید\n"
    "4- سرور مورد نظر را انتخاب کرده و فیلترشکن را روشن نمایید"
)

# Backward-compatible aliases (older callers / seeds).
EDU_ANDROID_V2RAYNG_TITLE = EDU_ANDROID_INSTALL_TITLE
EDU_ANDROID_V2RAYNG_BODY = EDU_ANDROID_INSTALL_BODY

EDU_IOS_V2BOX_TITLE = "اتصال در برنامه V2Box"
EDU_IOS_V2BOX_BODY = (
    "برای راه‌اندازی اشتراک روی <b>آیفون</b> با برنامه "
    "<b>V2Box</b>، مراحل زیر را به ترتیب انجام دهید:\n\n"
    "1️⃣ لینک اشتراک را کپی کنید.\n"
    "2️⃣ وارد برنامه <b>V2Box</b> شوید.\n"
    "3️⃣ از قسمت پایین صفحه، منوی <b>Configs</b> را انتخاب کنید.\n"
    "4️⃣ گزینه <b>➕</b> را انتخاب کنید.\n"
    "5️⃣ گزینه <b>اول</b> را انتخاب کنید.\n"
    "6️⃣ سرورها قابل مشاهده هستند.\n"
    "7️⃣ سپس سرور موردنظر را انتخاب کنید و فیلترشکن را روشن کنید.\n\n"
    "✨ اگر لیست خالی بود، یک‌بار از برنامه خارج شوید و دوباره وارد شوید."
)


def ensure_default_education(session: Session) -> None:
    def _ensure_root(title: str, sort_order: int) -> EducationCategory:
        row = session.scalar(
            select(EducationCategory).where(
                EducationCategory.title == title,
                EducationCategory.parent_id.is_(None),
            )
        )
        if row:
            row.enabled = True
            return row
        row = EducationCategory(title=title, sort_order=sort_order, enabled=True)
        session.add(row)
        session.flush()
        return row

    def _upsert_guide_lesson(
        category_id: int,
        *,
        title: str,
        body: str,
        match_tokens: tuple[str, ...],
    ) -> None:
        lessons = list_education_lessons(session, category_id, enabled_only=False)
        target = None
        for les in lessons:
            t = (les.title or "").lower()
            if any(tok.lower() in t for tok in match_tokens):
                target = les
                break
        if target is None and len(lessons) == 1:
            target = lessons[0]
        if target is not None:
            target.title = title
            target.body = body
            target.enabled = True
            target.sort_order = 0
            return
        session.add(
            EducationLesson(
                category_id=category_id,
                title=title,
                body=body,
                sort_order=0,
                enabled=True,
            )
        )

    start = _ensure_root("شروع کار", 0)
    if not list_education_lessons(session, start.id, enabled_only=False):
        session.add(
            EducationLesson(
                category_id=start.id,
                title="چگونه اشتراک بخرم؟",
                body=(
                    "۱) از منو «خرید اشتراک» را بزنید.\n"
                    "۲) پلن، مدت و حجم را انتخاب کنید.\n"
                    "۳) نام کاربری بسازید و پرداخت را انجام دهید.\n"
                    "۴) پس از واریز، رسید را با دکمه «ارسال رسید پرداخت» بفرستید."
                ),
                sort_order=0,
                enabled=True,
            )
        )

    ios = _ensure_root("آیفون / iOS", 10)
    _upsert_guide_lesson(
        ios.id,
        title=EDU_IOS_V2BOX_TITLE,
        body=EDU_IOS_V2BOX_BODY,
        match_tokens=("v2box", "آیفون", "iphone", "ios", "اتصال"),
    )

    android = _ensure_root("اندروید", 20)
    # Drop legacy single-lesson / attachment so install+update appear as two choices.
    for les in list_education_lessons(session, android.id, enabled_only=False):
        title = (les.title or "").lower()
        if any(
            tok in title
            for tok in ("v2ray", "اتصال", "نصب", "به‌روز", "بروز", "update", "install")
        ):
            les.media_type = None
            les.media_file_id = None
            les.media_caption = None
    _upsert_guide_lesson(
        android.id,
        title=EDU_ANDROID_INSTALL_TITLE,
        body=EDU_ANDROID_INSTALL_BODY,
        match_tokens=("نصب سرویس", "نصب", "v2ray", "اتصال"),
    )
    # Force sort + second lesson for update guide.
    install = None
    update = None
    for les in list_education_lessons(session, android.id, enabled_only=False):
        t = (les.title or "").lower()
        if "به‌روز" in t or "بروز" in t or "update" in t:
            update = les
        elif "نصب" in t or "v2ray" in t or "اتصال" in t:
            install = les
    if install is not None:
        install.title = EDU_ANDROID_INSTALL_TITLE
        install.body = EDU_ANDROID_INSTALL_BODY
        install.sort_order = 0
        install.enabled = True
        install.media_type = None
        install.media_file_id = None
        install.media_caption = None
    if update is None:
        session.add(
            EducationLesson(
                category_id=android.id,
                title=EDU_ANDROID_UPDATE_TITLE,
                body=EDU_ANDROID_UPDATE_BODY,
                sort_order=1,
                enabled=True,
            )
        )
    else:
        update.title = EDU_ANDROID_UPDATE_TITLE
        update.body = EDU_ANDROID_UPDATE_BODY
        update.sort_order = 1
        update.enabled = True
        update.media_type = None
        update.media_file_id = None
        update.media_caption = None
    session.flush()


def find_education_category_by_keywords(
    session: Session, keywords: list[str]
) -> EducationCategory | None:
    """Find an enabled root/education category whose title matches any keyword."""
    cats = list_all_education_categories(session)
    lowered = [k.lower() for k in keywords if k]
    for cat in cats:
        if not cat.enabled:
            continue
        title = (cat.title or "").lower()
        if any(k.lower() in title for k in lowered):
            return cat
    return None


def list_education_categories(
    session: Session, *, parent_id: int | None = None, enabled_only: bool = True
) -> list[EducationCategory]:
    q = select(EducationCategory).order_by(
        EducationCategory.sort_order.asc(), EducationCategory.id.asc()
    )
    if parent_id is None:
        q = q.where(EducationCategory.parent_id.is_(None))
    else:
        q = q.where(EducationCategory.parent_id == parent_id)
    if enabled_only:
        q = q.where(EducationCategory.enabled.is_(True))
    return list(session.scalars(q))


def list_all_education_categories(session: Session) -> list[EducationCategory]:
    return list(
        session.scalars(
            select(EducationCategory).order_by(
                EducationCategory.sort_order.asc(), EducationCategory.id.asc()
            )
        )
    )


def get_education_category(session: Session, category_id: int) -> EducationCategory | None:
    return session.get(EducationCategory, category_id)


def create_education_category(
    session: Session,
    title: str,
    *,
    parent_id: int | None = None,
    sort_order: int = 0,
) -> EducationCategory:
    row = EducationCategory(
        title=title.strip()[:128],
        parent_id=parent_id,
        sort_order=sort_order,
        enabled=True,
    )
    session.add(row)
    session.flush()
    return row


def update_education_category(
    session: Session,
    category_id: int,
    *,
    title: str | None = None,
    enabled: bool | None = None,
    sort_order: int | None = None,
) -> EducationCategory | None:
    row = session.get(EducationCategory, category_id)
    if not row:
        return None
    if title is not None:
        row.title = title.strip()[:128]
    if enabled is not None:
        row.enabled = enabled
    if sort_order is not None:
        row.sort_order = sort_order
    session.flush()
    return row


def delete_education_category(session: Session, category_id: int) -> bool:
    row = session.get(EducationCategory, category_id)
    if not row:
        return False
    lessons = list(
        session.scalars(
            select(EducationLesson).where(EducationLesson.category_id == category_id)
        )
    )
    for lesson in lessons:
        session.delete(lesson)
    children = list(
        session.scalars(
            select(EducationCategory).where(EducationCategory.parent_id == category_id)
        )
    )
    for child in children:
        delete_education_category(session, child.id)
    session.delete(row)
    session.flush()
    return True


def list_education_lessons(
    session: Session, category_id: int, *, enabled_only: bool = True
) -> list[EducationLesson]:
    q = (
        select(EducationLesson)
        .where(EducationLesson.category_id == category_id)
        .order_by(EducationLesson.sort_order.asc(), EducationLesson.id.asc())
    )
    if enabled_only:
        q = q.where(EducationLesson.enabled.is_(True))
    return list(session.scalars(q))


def get_education_lesson(session: Session, lesson_id: int) -> EducationLesson | None:
    return session.get(EducationLesson, lesson_id)


def create_education_lesson(
    session: Session,
    category_id: int,
    title: str,
    body: str,
    *,
    sort_order: int = 0,
) -> EducationLesson:
    row = EducationLesson(
        category_id=category_id,
        title=title.strip()[:128],
        body=body or "",
        sort_order=sort_order,
        enabled=True,
    )
    session.add(row)
    session.flush()
    return row


def update_education_lesson(
    session: Session,
    lesson_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    enabled: bool | None = None,
    sort_order: int | None = None,
    media_type: str | None = None,
    media_file_id: str | None = None,
    media_caption: str | None = None,
    clear_media: bool = False,
) -> EducationLesson | None:
    row = session.get(EducationLesson, lesson_id)
    if not row:
        return None
    if title is not None:
        row.title = title.strip()[:128]
    if body is not None:
        row.body = body
    if enabled is not None:
        row.enabled = enabled
    if sort_order is not None:
        row.sort_order = sort_order
    if clear_media:
        row.media_type = None
        row.media_file_id = None
        row.media_caption = None
    else:
        if media_type is not None:
            row.media_type = (media_type or "").strip()[:16] or None
        if media_file_id is not None:
            row.media_file_id = (media_file_id or "").strip()[:256] or None
        if media_caption is not None:
            row.media_caption = media_caption
    row.updated_at = datetime.utcnow()
    session.flush()
    return row


def delete_education_lesson(session: Session, lesson_id: int) -> bool:
    row = session.get(EducationLesson, lesson_id)
    if not row:
        return False
    session.delete(row)
    session.flush()
    return True


def create_support_ticket(
    session: Session,
    *,
    telegram_id: int,
    order_id: int | None = None,
    kind: str = "support",
    subject: str | None = None,
    status: str = "open",
) -> SupportTicket:
    ticket = SupportTicket(
        telegram_id=telegram_id,
        order_id=order_id,
        kind=kind,
        subject=(subject or "")[:256] or None,
        status=status,
    )
    session.add(ticket)
    session.flush()
    return ticket


def add_support_message(
    session: Session,
    ticket_id: int,
    *,
    sender: str,
    body: str,
    admin_id: int | None = None,
) -> SupportMessage:
    msg = SupportMessage(
        ticket_id=ticket_id,
        sender=sender,
        body=body or "",
        admin_id=admin_id,
    )
    session.add(msg)
    ticket = session.get(SupportTicket, ticket_id)
    if ticket:
        ticket.updated_at = datetime.utcnow()
        if sender == "customer" and ticket.status != "closed":
            ticket.status = "awaiting_admin"
        elif sender == "admin" and ticket.status != "closed":
            ticket.status = "awaiting_customer"
        # system messages do not flip awaiting_* status
    session.flush()
    return msg


def get_support_ticket(session: Session, ticket_id: int) -> SupportTicket | None:
    return session.get(SupportTicket, ticket_id)


def get_open_ticket_for_order(session: Session, order_id: int) -> SupportTicket | None:
    return session.scalar(
        select(SupportTicket)
        .where(
            SupportTicket.order_id == order_id,
            SupportTicket.status.in_(
                ("open", "awaiting_customer", "awaiting_admin")
            ),
        )
        .order_by(SupportTicket.id.desc())
        .limit(1)
    )


def get_open_ticket_for_customer(
    session: Session, telegram_id: int
) -> SupportTicket | None:
    return session.scalar(
        select(SupportTicket)
        .where(
            SupportTicket.telegram_id == telegram_id,
            SupportTicket.status.in_(
                ("open", "awaiting_customer", "awaiting_admin")
            ),
        )
        .order_by(SupportTicket.updated_at.desc())
        .limit(1)
    )


def list_support_tickets(
    session: Session,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int = 30,
    include_hidden: bool = False,
    telegram_id: int | None = None,
) -> list[SupportTicket]:
    q = select(SupportTicket).order_by(SupportTicket.updated_at.desc()).limit(limit)
    if statuses:
        q = q.where(SupportTicket.status.in_(statuses))
    if telegram_id is not None:
        q = q.where(SupportTicket.telegram_id == int(telegram_id))
    if not include_hidden:
        q = q.where(
            or_(
                SupportTicket.admin_hidden.is_(False),
                SupportTicket.admin_hidden.is_(None),
            )
        )
    return list(session.scalars(q))


def list_customer_tickets(
    session: Session, telegram_id: int, *, limit: int = 40
) -> list[SupportTicket]:
    """All tickets for a customer (open + closed), newest first."""
    return list(
        session.scalars(
            select(SupportTicket)
            .where(SupportTicket.telegram_id == int(telegram_id))
            .order_by(SupportTicket.updated_at.desc())
            .limit(limit)
        )
    )


def list_support_messages(session: Session, ticket_id: int) -> list[SupportMessage]:
    return list(
        session.scalars(
            select(SupportMessage)
            .where(SupportMessage.ticket_id == ticket_id)
            .order_by(SupportMessage.id.asc())
        )
    )


def close_support_ticket(session: Session, ticket_id: int) -> SupportTicket | None:
    ticket = session.get(SupportTicket, ticket_id)
    if not ticket:
        return None
    ticket.status = "closed"
    ticket.closed_at = datetime.utcnow()
    ticket.updated_at = datetime.utcnow()
    session.flush()
    return ticket


def hide_admin_tickets(
    session: Session, *, statuses: tuple[str, ...] | None = None
) -> int:
    q = select(SupportTicket).where(
        or_(SupportTicket.admin_hidden.is_(False), SupportTicket.admin_hidden.is_(None))
    )
    if statuses:
        q = q.where(SupportTicket.status.in_(tuple(statuses)))
    rows = list(session.scalars(q))
    for t in rows:
        t.admin_hidden = True
        t.updated_at = datetime.utcnow()
    session.flush()
    return len(rows)


SUPPORT_SUBJECT_MAX = 30
TICKET_CUSTOMER_REPLY_TTL_HOURS = 24


def autoclose_stale_customer_tickets(session: Session) -> list[SupportTicket]:
    """Close tickets waiting on the customer for longer than 24h (NetPlus-style)."""
    cutoff = datetime.utcnow() - timedelta(hours=TICKET_CUSTOMER_REPLY_TTL_HOURS)
    rows = list(
        session.scalars(
            select(SupportTicket).where(
                SupportTicket.status == "awaiting_customer",
                SupportTicket.updated_at <= cutoff,
            )
        )
    )
    closed: list[SupportTicket] = []
    for ticket in rows:
        ticket.status = "closed"
        ticket.closed_at = datetime.utcnow()
        ticket.updated_at = datetime.utcnow()
        add_support_message(
            session,
            ticket.id,
            sender="system",
            body="تیکت به‌خاطر نبود پاسخ مشتری تا ۲۴ ساعت بسته شد.",
        )
        closed.append(ticket)
    session.flush()
    return closed
