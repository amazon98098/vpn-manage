"""FX One admin control panel — FastAPI + Jinja (same DB as Telegram bots)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import asc, desc, func, or_, select

from app.config import load_settings
from app.database import repositories as repo
from app.database.database import get_session, init_db
from app.database.models import (
    CustomerOrder,
    CustomerProfile,
    CustomerServiceLink,
    Subscription,
    Vendor,
    WalletLedger,
)
from app.web.formatting import (
    card_digits,
    card_grouped,
    num,
    plan_family,
    plan_icon,
    price_plan_family,
    rial,
    status_icon,
    remain_days,
    toman,
    traffic,
    vendor_icon,
)
from app.web.i18n import (
    LANG_COOKIE,
    nav_for,
    normalize_lang,
    service_columns_for,
    translate,
)

ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
TEMPLATES.env.filters["traffic"] = traffic
TEMPLATES.env.filters["card_grouped"] = card_grouped
TEMPLATES.env.filters["plan_icon"] = plan_icon
TEMPLATES.env.filters["status_icon"] = status_icon
TEMPLATES.env.filters["vendor_icon"] = vendor_icon
TEMPLATES.env.filters["toman"] = toman
TEMPLATES.env.filters["rial"] = rial
TEMPLATES.env.filters["num"] = num
TEMPLATES.env.filters["remain_days"] = remain_days
COOKIE = "fx_admin_sess"

SERVICE_SORTABLE = {
    "id": Subscription.id,
    "username": Subscription.username,
    "product": Subscription.product_name,
    "status": Subscription.status,
    "remain": Subscription.remaining_traffic_mb,
    "limit": Subscription.traffic_limit_mb,
    "used": Subscription.used_traffic_mb,
    "expire": Subscription.expiration_date,
    "created": Subscription.created_at,
}

WARNINGS_SORTABLE = {
    "username",
    "vendor",
    "status",
    "remain",
    "expire",
    "owner",
}
ALMOST_OUT_SORTABLE = {
    "username",
    "status",
    "reasons",
    "remain",
    "expire",
    "owner",
}
CUSTOMERS_SORTABLE = {
    "telegram",
    "name",
    "wallet",
    "services",
    "created",
}


def _sort_alert_items(
    items: list[dict[str, Any]],
    *,
    sort: str,
    direction: str,
    allowed: set[str],
) -> list[dict[str, Any]]:
    """In-memory sort for warnings / almost-out item dicts."""
    key = sort if sort in allowed else ""
    reverse = direction != "asc"

    def _key(it: dict[str, Any]):
        sub = it.get("sub")
        if key == "username":
            return ((getattr(sub, "username", None) or "").lower(), int(getattr(sub, "id", 0) or 0))
        if key == "vendor":
            return ((it.get("vendor_key") or "").lower(), int(getattr(sub, "id", 0) or 0))
        if key == "status":
            return ((getattr(sub, "status", None) or "").lower(), int(getattr(sub, "id", 0) or 0))
        if key == "remain":
            val = getattr(sub, "remaining_traffic_mb", None)
            return (val is None, float(val) if val is not None else 0.0, int(getattr(sub, "id", 0) or 0))
        if key == "expire":
            val = getattr(sub, "expiration_date", None)
            return (val is None, val or datetime.min, int(getattr(sub, "id", 0) or 0))
        if key == "owner":
            owner = it.get("owner")
            return (owner is None, int(owner or 0), int(getattr(sub, "id", 0) or 0))
        if key == "reasons":
            reasons = it.get("reasons") or ()
            return (",".join(sorted(reasons)), int(getattr(sub, "id", 0) or 0))
        # Default: keep given order stable by id
        return (int(getattr(sub, "id", 0) or 0),)

    if not key:
        return items
    return sorted(items, key=_key, reverse=reverse)


def _settings():
    return load_settings(require_bot=False)


def _password() -> str:
    return (os.getenv("ADMIN_WEB_PASSWORD") or "").strip()


def _secret() -> str:
    raw = (os.getenv("ADMIN_WEB_SECRET") or "").strip()
    if raw:
        return raw
    return hashlib.sha256((_password() or "fx-admin").encode()).hexdigest()


def _sign(token: str) -> str:
    return hmac.new(_secret().encode(), token.encode(), hashlib.sha256).hexdigest()


def _make_session() -> str:
    token = secrets.token_urlsafe(24)
    return f"{token}.{_sign(token)}"


def _valid_session(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    token, sig = value.rsplit(".", 1)
    return hmac.compare_digest(_sign(token), sig)


class NotAuthenticated(Exception):
    pass


def _require_auth(request: Request) -> None:
    if not _password():
        raise HTTPException(
            status_code=503,
            detail="Set ADMIN_WEB_PASSWORD in .env to enable the panel.",
        )
    if not _valid_session(request.cookies.get(COOKIE)):
        raise NotAuthenticated()


def _lang(request: Request) -> str:
    return normalize_lang(request.cookies.get(LANG_COOKIE))


def _flash_actions(request: Request) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    i = 1
    while i <= 3:
        label = (request.query_params.get(f"act{i}") or "").strip()
        href = (request.query_params.get(f"act{i}href") or "").strip()
        if label and href and href.startswith("/"):
            out.append({"label": label, "href": href})
        i += 1
    return out


def _panel_badges() -> dict[str, int]:
    try:
        with get_session() as session:
            return repo.panel_badge_counts(session)
    except Exception:
        return {
            "orders": 0,
            "queue": 0,
            "tickets": 0,
            "almost_out": 0,
            "warnings": 0,
        }


def _ctx(request: Request, active: str, **extra: Any) -> dict[str, Any]:
    lang = _lang(request)

    def t(key: str, **kwargs: Any) -> str:
        return translate(lang, key, **kwargs)

    badges = extra.pop("badges", None)
    if badges is None:
        badges = _panel_badges()

    return {
        "request": request,
        "nav": nav_for(lang),
        "active": active,
        "now": datetime.utcnow(),
        "service_columns": service_columns_for(lang),
        "lang": lang,
        "dir": "rtl" if lang == "fa" else "ltr",
        "t": t,
        "flash_actions": _flash_actions(request),
        "badges": badges,
        **extra,
    }


def _parse_id_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if part.isdigit():
            out.append(int(part))
    return out


def _build_service_items(session, rows: list[Subscription]) -> list[dict[str, Any]]:
    items = []
    for sub in rows:
        v = repo.get_vendor(session, sub.vendor_id) if sub.vendor_id else None
        vkey = v.key if v else None
        link = repo.get_link_for_subscription(session, sub.id) or repo.get_link_for_username(
            session, sub.username
        )
        url = (sub.subscription_url or "").strip()
        items.append(
            {
                "sub": sub,
                "vendor_key": vkey or "—",
                "vendor_name": v.name if v else "—",
                "vendor_icon": vendor_icon(vkey),
                "plan_icon": plan_icon(sub, vendor_key=vkey),
                "status_icon": status_icon(sub.status),
                "owner": link.telegram_id if link else None,
                "has_url": bool(url.startswith("http")),
                "url": url if url.startswith("http") else "",
            }
        )
    return items


def create_app() -> FastAPI:
    settings = _settings()
    init_db(settings.db_url)
    with get_session() as session:
        repo.ensure_vendor(session, "netplus", "NetPlus", settings.netplus_bot)
        repo.ensure_vendor(session, "finearts", "FineArts", settings.finearts_bot)
        repo.ensure_default_price_plans(session)

    app = FastAPI(title="FX One Admin", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.exception_handler(NotAuthenticated)
    async def _auth_redirect(_request: Request, _exc: NotAuthenticated):
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if _valid_session(request.cookies.get(COOKIE)):
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            _ctx(
                request,
                "login",
                error=request.query_params.get("e"),
                badges={},
            ),
        )

    @app.post("/login")
    async def login_submit(password: str = Form(...)):
        expected = _password()
        if not expected or not hmac.compare_digest(password, expected):
            return RedirectResponse("/login?e=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE, _make_session(), httponly=True, samesite="lax", max_age=60 * 60 * 12
        )
        return resp

    @app.post("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.post("/lang")
    async def set_lang(request: Request, lang: str = Form("en")):
        from urllib.parse import urlparse

        next_lang = normalize_lang(lang)
        authenticated = _valid_session(request.cookies.get(COOKIE))
        dest = "/login" if not authenticated else "/"
        referer = (request.headers.get("referer") or "").strip()
        if referer:
            parsed = urlparse(referer)
            path = parsed.path or "/"
            if path.startswith("/") and (authenticated or path.startswith("/login")):
                dest = path + (f"?{parsed.query}" if parsed.query else "")
        resp = RedirectResponse(dest, status_code=303)
        resp.set_cookie(
            LANG_COOKIE,
            next_lang,
            httponly=False,
            samesite="lax",
            max_age=60 * 60 * 24 * 365,
        )
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _auth: None = Depends(_require_auth)):
        with get_session() as session:
            by_status = repo.count_by_status(session)
            vendors = list(session.scalars(select(Vendor)))
            vendor_counts = []
            for v in vendors:
                n = session.scalar(
                    select(func.count())
                    .select_from(Subscription)
                    .where(Subscription.vendor_id == v.id)
                )
                vendor_counts.append({"key": v.key, "name": v.name, "n": int(n or 0)})
            active_jobs = len(repo.list_active_renew_jobs(session, limit=500))
            open_orders = session.scalar(
                select(func.count())
                .select_from(CustomerOrder)
                .where(
                    CustomerOrder.status.in_(
                        (
                            "awaiting_payment",
                            "receipt_window",
                            "awaiting_review",
                            "fulfilling",
                        )
                    )
                )
            )
            customers = session.scalar(select(func.count()).select_from(CustomerProfile))
            unlinked = repo.count_unlinked_subscriptions(session)
            _almost_items, almost = repo.list_local_almost_out(session, limit=200)
            _warn_rows, warnings_n = repo.list_warning_subscriptions(
                session, offset=0, limit=1
            )
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            _ctx(
                request,
                "dashboard",
                by_status=by_status,
                vendor_counts=vendor_counts,
                active_jobs=active_jobs,
                open_orders=int(open_orders or 0),
                customers=int(customers or 0),
                unlinked=unlinked,
                almost=almost,
                warnings_n=int(warnings_n or 0),
            ),
        )

    def _services_query(
        *,
        q: str,
        vendor: str,
        status_f: str,
        sort: str,
        direction: str,
        page: int,
        page_size: int = 40,
        link_f: str = "",
        plan_f: str = "",
    ):
        page = max(0, page)
        with get_session() as session:
            vendors = list(session.scalars(select(Vendor).order_by(Vendor.id)))
            stmt = select(Subscription)
            if vendor:
                v = repo.get_vendor_by_key(session, vendor)
                if v:
                    stmt = stmt.where(Subscription.vendor_id == v.id)
            if status_f:
                stmt = stmt.where(Subscription.status == status_f)
            if link_f == "has":
                stmt = stmt.where(
                    Subscription.subscription_url.is_not(None),
                    Subscription.subscription_url.like("http%"),
                )
            elif link_f == "missing":
                stmt = stmt.where(
                    or_(
                        Subscription.subscription_url.is_(None),
                        Subscription.subscription_url == "",
                        ~Subscription.subscription_url.like("http%"),
                    )
                )
            if plan_f == "economy":
                fa = repo.get_vendor_by_key(session, "finearts")
                if fa:
                    stmt = stmt.where(Subscription.vendor_id != fa.id)
                    stmt = stmt.where(
                        or_(
                            Subscription.product_name.is_(None),
                            ~Subscription.product_name.contains("نامحدود"),
                        )
                    )
            elif plan_f == "volume":
                fa = repo.get_vendor_by_key(session, "finearts")
                conds = [Subscription.product_name.contains("نامحدود")]
                if fa:
                    conds.append(Subscription.vendor_id == fa.id)
                stmt = stmt.where(or_(*conds))
            if q.strip():
                like = f"%{q.strip()}%"
                stmt = stmt.where(
                    or_(
                        Subscription.username.ilike(like),
                        Subscription.product_name.ilike(like),
                        Subscription.vendor_service_id.ilike(like),
                    )
                )
            col = SERVICE_SORTABLE.get(sort, Subscription.id)
            stmt = stmt.order_by(asc(col) if direction == "asc" else desc(col))
            total = session.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = list(
                session.scalars(stmt.offset(page * page_size).limit(page_size))
            )
            items = _build_service_items(session, rows)
            return {
                "items": items,
                "vendors": vendors,
                "q": q,
                "vendor": vendor,
                "status_f": status_f,
                "link_f": link_f,
                "plan_f": plan_f,
                "sort": sort,
                "direction": direction,
                "page": page,
                "pages": max(1, ((int(total or 0)) + page_size - 1) // page_size),
                "total": int(total or 0),
            }

    @app.get("/services", response_class=HTMLResponse)
    async def services(
        request: Request,
        q: str = "",
        vendor: str = "",
        status_f: str = "",
        link_f: str = "",
        plan_f: str = "",
        sort: str = "id",
        direction: str = "desc",
        page: int = 0,
        partial: int = 0,
        _auth: None = Depends(_require_auth),
    ):
        data = _services_query(
            q=q,
            vendor=vendor,
            status_f=status_f,
            link_f=link_f if link_f in {"", "has", "missing"} else "",
            plan_f=plan_f if plan_f in {"", "economy", "volume"} else "",
            sort=sort if sort in SERVICE_SORTABLE else "id",
            direction="asc" if direction == "asc" else "desc",
            page=page,
        )
        tpl = "services_table.html" if partial else "services.html"
        return TEMPLATES.TemplateResponse(
            request,
            tpl,
            _ctx(request, "services", flash=request.query_params.get("ok"), **data),
        )

    @app.get("/services/export.csv")
    async def services_export(
        q: str = "",
        vendor: str = "",
        status_f: str = "",
        link_f: str = "",
        plan_f: str = "",
        _auth: None = Depends(_require_auth),
    ):
        import csv
        import io

        data = _services_query(
            q=q,
            vendor=vendor,
            status_f=status_f,
            link_f=link_f if link_f in {"", "has", "missing"} else "",
            plan_f=plan_f if plan_f in {"", "economy", "volume"} else "",
            sort="username",
            direction="asc",
            page=0,
            page_size=5000,
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "id",
                "username",
                "vendor",
                "plan_icon",
                "product",
                "status",
                "remain_mb",
                "used_mb",
                "limit_mb",
                "remain_days",
                "owner",
                "has_url",
            ]
        )
        for it in data["items"]:
            sub = it["sub"]
            w.writerow(
                [
                    sub.id,
                    sub.username,
                    it["vendor_key"],
                    it["plan_icon"],
                    sub.product_name or "",
                    sub.status,
                    sub.remaining_traffic_mb,
                    sub.used_traffic_mb,
                    sub.traffic_limit_mb,
                    remain_days(sub.expiration_date),
                    it["owner"] or "",
                    "1" if it["has_url"] else "0",
                ]
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="services_export.csv"'
            },
        )

    @app.post("/services/audit-urls")
    async def services_audit_urls(_auth: None = Depends(_require_auth)):
        """Clear subscription URLs whose filename != local username (exact)."""
        from app.database.models import USABLE_SUBSCRIPTION_STATUSES
        from app.services.subscription_userinfo import (
            fetch_subscription_userinfo,
            url_username_matches,
        )

        cleared = 0
        checked = 0
        with get_session() as session:
            rows = list(
                session.scalars(
                    select(Subscription).where(
                        Subscription.subscription_url.is_not(None),
                        Subscription.status.in_(tuple(USABLE_SUBSCRIPTION_STATUSES)),
                    )
                )
            )
            for sub in rows:
                url = (sub.subscription_url or "").strip()
                if not url.startswith("http"):
                    continue
                checked += 1
                ui = fetch_subscription_userinfo(url, timeout=18.0)
                if ui.ok and not url_username_matches(sub.username, ui.filename):
                    sub.subscription_url = None
                    cleared += 1
            session.flush()
        return RedirectResponse(
            f"/services?ok=audit-{checked}-cleared-{cleared}", status_code=303
        )

    def _wake_renew_queue() -> bool:
        from app.telegram import customer_bridge

        return customer_bridge.wake_renew_queue()

    def _username_busy(session, username: str) -> bool:
        return any(
            j.username == username for j in repo.list_active_renew_jobs(session, limit=500)
        )

    def _plan_cards(session, plans: list) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in plans:
            vkey = repo.vendor_key_for_price_plan(p)
            days = int(p.duration_days) if p.duration_days is not None else 0
            out.append(
                {
                    "id": p.id,
                    "key": p.key,
                    "title": p.title,
                    "price_toman": int(p.price_toman or 0),
                    "traffic_gb": p.traffic_gb,
                    "duration_days": days,
                    "vendor_key": vkey,
                    "vendor_icon": vendor_icon(vkey),
                    "plan_icon": "💎" if days <= 0 or vkey == "finearts" else "✨",
                }
            )
        return out

    def _vendor_job_from_plan(session, plan, *, kind: str, username: str, sub=None):
        """Build create/renew job args from an enabled PricePlan (customer-bot parity)."""
        vkey = repo.vendor_key_for_price_plan(plan)
        settings = _settings()
        if vkey == "finearts":
            vrow = repo.ensure_vendor(
                session, "finearts", "FineArts", settings.finearts_bot
            )
        else:
            vrow = repo.ensure_vendor(
                session, "netplus", "NetPlus", settings.netplus_bot
            )
        fa_pkg = repo.finearts_package_callback_for_plan(plan)
        days = int(plan.duration_days) if plan.duration_days is not None else 0
        plan_id = int(plan.id) if plan and plan.id else None
        if kind == "create":
            if vkey == "finearts":
                return dict(
                    username=username,
                    vendor_service_id=None,
                    subscription_id=None,
                    plan_mode="create",
                    requested_by=0,
                    duration_callback=None,
                    package_callback=fa_pkg,
                    kind="create",
                    vendor_id=vrow.id if vrow else None,
                    plan_id=plan_id,
                )
            # NetPlus create uses productmonth_* callbacks; package auto-picked via plan_id GB.
            if days >= 90:
                dur = "productmonth_91"
            elif days >= 60:
                dur = "productmonth_61"
            else:
                dur = "productmonth_31"
            return dict(
                username=username,
                vendor_service_id=None,
                subscription_id=None,
                plan_mode="create",
                requested_by=0,
                duration_callback=dur,
                package_callback=None,
                kind="create",
                vendor_id=vrow.id if vrow else None,
                plan_id=plan_id,
            )
        # renew
        if vkey == "finearts" or days <= 0:
            return dict(
                username=username,
                vendor_service_id=sub.vendor_service_id if sub else None,
                subscription_id=sub.id if sub else None,
                plan_mode="current",
                requested_by=0,
                duration_callback=None,
                package_callback=fa_pkg,
                kind="renew",
                vendor_id=(sub.vendor_id if sub and sub.vendor_id else (vrow.id if vrow else None)),
                plan_id=plan_id,
            )
        # NetPlus renew with selected sell plan = change duration path
        return dict(
            username=username,
            vendor_service_id=sub.vendor_service_id if sub else None,
            subscription_id=sub.id if sub else None,
            plan_mode="change",
            requested_by=0,
            duration_callback=repo.renew_extend_callback_for_days(days) or None,
            package_callback=None,
            kind="renew",
            vendor_id=(sub.vendor_id if sub and sub.vendor_id else (vrow.id if vrow else None)),
            plan_id=plan_id,
        )

    @app.get("/services/create", response_class=HTMLResponse)
    async def service_create_page(
        request: Request, _auth: None = Depends(_require_auth)
    ):
        with get_session() as session:
            plans = _plan_cards(
                session, repo.list_price_plans(session, enabled_only=True)
            )
        return TEMPLATES.TemplateResponse(
            request,
            "service_create.html",
            _ctx(
                request,
                "services",
                plans=plans,
                err=request.query_params.get("err"),
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/services/create")
    async def service_create_submit(
        username: str = Form(""),
        plan_id: int = Form(0),
        owner_tg: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        uname = (username or "").strip()
        if not uname or len(uname) < 3:
            return RedirectResponse("/services/create?err=username", status_code=303)
        with get_session() as session:
            plan = repo.get_price_plan(session, int(plan_id or 0))
            if not plan or not plan.enabled:
                return RedirectResponse("/services/create?err=plan", status_code=303)
            ok, reason = repo.is_username_available_for_create(session, uname, 0)
            if not ok:
                return RedirectResponse(
                    f"/services/create?err={reason or 'username'}", status_code=303
                )
            if _username_busy(session, uname):
                return RedirectResponse("/services/create?err=busy", status_code=303)
            owner_digits = "".join(ch for ch in (owner_tg or "") if ch.isdigit())
            if owner_digits:
                if len(owner_digits) < 5 or not repo.get_customer_profile(
                    session, int(owner_digits)
                ):
                    return RedirectResponse(
                        "/services/create?err=owner", status_code=303
                    )
            args = _vendor_job_from_plan(session, plan, kind="create", username=uname)
            job = repo.create_renew_job(session, **args)
            job_id = job.id
            if owner_digits:
                # Link after vendor create_ok (subscription row may not exist yet).
                repo.set_state(session, f"renew_job_owner:{job_id}", owner_digits)
        if not _wake_renew_queue():
            return RedirectResponse(
                f"/queue?ok=queued-{job_id}&err=queue-offline", status_code=303
            )
        return RedirectResponse(f"/queue?ok=create-{job_id}", status_code=303)

    @app.get("/services/{sub_id}/renew", response_class=HTMLResponse)
    async def service_renew_page(
        request: Request, sub_id: int, _auth: None = Depends(_require_auth)
    ):
        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404)
            v = repo.get_vendor(session, sub.vendor_id) if sub.vendor_id else None
            busy = _username_busy(session, sub.username)
            current = repo.find_renew_plan_for_username(session, sub.username)
            all_plans = repo.list_price_plans(session, enabled_only=True)
            family = plan_family(sub, vendor_key=v.key if v else None)
            plans = _plan_cards(session, all_plans)
            for card in plans:
                card["compatible"] = price_plan_family(
                    next((p for p in all_plans if p.id == card["id"]), None)
                ) == family
            current_card = None
            if current:
                cards = _plan_cards(session, [current])
                current_card = cards[0] if cards else None
                if current_card:
                    current_card["compatible"] = True
        return TEMPLATES.TemplateResponse(
            request,
            "service_renew.html",
            _ctx(
                request,
                "services",
                sub=sub,
                vendor=v,
                vendor_key=(v.key if v else "") or "",
                busy=busy,
                plans=plans,
                current_plan=current_card,
                renew_family=family,
                plan_icon=plan_icon(sub, vendor_key=v.key if v else None),
                err=request.query_params.get("err"),
            ),
        )

    @app.post("/services/{sub_id}/renew")
    async def service_renew_submit(
        sub_id: int,
        choice: str = Form("current"),
        _auth: None = Depends(_require_auth),
    ):
        choice_key = (choice or "current").strip().lower()
        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404)
            uname = sub.username
            if _username_busy(session, uname):
                return RedirectResponse(
                    f"/services/{sub_id}/renew?err=busy", status_code=303
                )
            if choice_key == "current":
                # Same as admin bot “تمدید پلن فعلی” — no sell-plan package required.
                job = repo.create_renew_job(
                    session,
                    username=uname,
                    vendor_service_id=sub.vendor_service_id,
                    subscription_id=sub.id,
                    plan_mode="current",
                    requested_by=0,
                    duration_callback=None,
                    package_callback=None,
                    kind="renew",
                    vendor_id=sub.vendor_id,
                )
            else:
                try:
                    pid = int(choice_key)
                except ValueError:
                    return RedirectResponse(
                        f"/services/{sub_id}/renew?err=plan", status_code=303
                    )
                plan = repo.get_price_plan(session, pid)
                if not plan or not plan.enabled:
                    return RedirectResponse(
                        f"/services/{sub_id}/renew?err=plan", status_code=303
                    )
                v = repo.get_vendor(session, sub.vendor_id) if sub.vendor_id else None
                family = plan_family(sub, vendor_key=v.key if v else None)
                if price_plan_family(plan) != family:
                    return RedirectResponse(
                        f"/services/{sub_id}/renew?err=family", status_code=303
                    )
                args = _vendor_job_from_plan(
                    session, plan, kind="renew", username=uname, sub=sub
                )
                job = repo.create_renew_job(session, **args)
            job_id = job.id
        if not _wake_renew_queue():
            return RedirectResponse(
                f"/queue?ok=queued-{job_id}&err=queue-offline", status_code=303
            )
        return RedirectResponse(f"/queue?ok=renew-{job_id}", status_code=303)

    @app.get("/services/{sub_id}", response_class=HTMLResponse)
    async def service_detail(
        request: Request, sub_id: int, _auth: None = Depends(_require_auth)
    ):
        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404, "Service not found")
            v = repo.get_vendor(session, sub.vendor_id) if sub.vendor_id else None
            link = repo.get_link_for_subscription(session, sub.id) or repo.get_link_for_username(
                session, sub.username
            )
        return TEMPLATES.TemplateResponse(
            request,
            "service_detail.html",
            _ctx(
                request,
                "services",
                sub=sub,
                vendor=v,
                owner=link.telegram_id if link else None,
                plan_icon=plan_icon(sub, vendor_key=v.key if v else None),
                status_icon=status_icon(sub.status),
                has_url=bool((sub.subscription_url or "").startswith("http")),
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/services/{sub_id}/save")
    async def service_save(
        sub_id: int,
        product_name: str = Form(""),
        status: str = Form("active"),
        owner_tg: str = Form(""),
        subscription_url: str = Form(""),
        clear_owner: str = Form("off"),
        _auth: None = Depends(_require_auth),
    ):
        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404)
            sub.product_name = (product_name or "").strip() or sub.product_name
            sub.status = (status or sub.status).strip()
            url = (subscription_url or "").strip()
            if url:
                from app.services.subscription_refresh import (
                    assign_subscription_url_if_matches,
                )

                if not assign_subscription_url_if_matches(
                    session, sub, url, apply_traffic=True
                ):
                    raise HTTPException(
                        400,
                        "URL filename does not exactly match this service username",
                    )
            if clear_owner in {"on", "1", "true", "yes"}:
                repo.unlink_subscription_owner(session, sub.id)
            else:
                digits = "".join(ch for ch in owner_tg if ch.isdigit())
                if digits and repo.get_customer_profile(session, int(digits)):
                    repo.set_local_owner(
                        session,
                        to_telegram_id=int(digits),
                        initiated_by_telegram_id=0,
                        source="admin_web",
                        subscription=sub,
                        note="web_panel",
                    )
        return RedirectResponse(f"/services/{sub_id}?ok=1", status_code=303)

    @app.post("/services/bulk")
    async def services_bulk(
        action: str = Form(...),
        ids: str = Form(""),
        owner_tg: str = Form(""),
        status: str = Form("active"),
        _auth: None = Depends(_require_auth),
    ):
        id_list = _parse_id_list(ids)
        with get_session() as session:
            if action == "unlink":
                for sid in id_list:
                    repo.unlink_subscription_owner(session, sid)
            elif action == "assign":
                digits = "".join(ch for ch in owner_tg if ch.isdigit())
                if len(digits) >= 5 and repo.get_customer_profile(session, int(digits)):
                    repo.assign_subscriptions_to_owner(
                        session,
                        subscription_ids=id_list,
                        to_telegram_id=int(digits),
                        initiated_by_telegram_id=0,
                        note="admin_web_bulk",
                    )
            elif action == "status":
                st = (status or "active").strip()
                for sid in id_list:
                    sub = session.get(Subscription, sid)
                    if sub:
                        sub.status = st
            elif action == "clear_url":
                for sid in id_list:
                    sub = session.get(Subscription, sid)
                    if sub:
                        sub.subscription_url = None
            elif action == "refresh_url":
                from app.services.subscription_refresh import refresh_subscription_userinfo

                for sid in id_list:
                    sub = session.get(Subscription, sid)
                    if sub and (sub.subscription_url or "").strip().startswith("http"):
                        try:
                            refresh_subscription_userinfo(
                                session, subscription=sub, force=True
                            )
                        except Exception:
                            pass
            elif action == "refresh_vendor":
                pass  # handled after session via renew_queue
            elif action == "delete":
                for sid in id_list:
                    sub = session.get(Subscription, sid)
                    if not sub:
                        continue
                    repo.unlink_subscription_owner(session, sid)
                    # Null renew job FKs then drop inventory row
                    from app.database.models import RenewJob
                    from sqlalchemy import update

                    session.execute(
                        update(RenewJob)
                        .where(RenewJob.subscription_id == sid)
                        .values(subscription_id=None)
                    )
                    session.delete(sub)
            session.flush()
        if action == "refresh_vendor":
            from app.telegram import customer_bridge

            rq = customer_bridge.renew_queue
            names: list[str] = []
            with get_session() as session:
                for sid in id_list:
                    sub = session.get(Subscription, sid)
                    if sub and sub.username:
                        names.append(sub.username)
            ok_n = 0
            if rq is not None:
                for uname in names:
                    try:
                        result = await rq.refresh_subscription(uname)
                        if result == "ok":
                            ok_n += 1
                    except Exception:
                        pass
            return RedirectResponse(
                f"/services?ok=vendor-refresh-{ok_n}-of-{len(names)}",
                status_code=303,
            )
        if action == "refresh_url":
            return RedirectResponse("/services?ok=refreshed", status_code=303)
        return RedirectResponse("/services?ok=1", status_code=303)

    @app.post("/services/{sub_id}/refresh-url")
    async def service_refresh_url(sub_id: int, _auth: None = Depends(_require_auth)):
        from app.services.subscription_refresh import refresh_subscription_userinfo

        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404)
            url = (sub.subscription_url or "").strip()
            if not url.startswith("http"):
                return RedirectResponse(
                    f"/services/{sub_id}?ok=no-url", status_code=303
                )
            try:
                refresh_subscription_userinfo(session, subscription=sub, force=True)
            except Exception:
                return RedirectResponse(
                    f"/services/{sub_id}?ok=refresh-failed", status_code=303
                )
        return RedirectResponse(f"/services/{sub_id}?ok=refreshed", status_code=303)

    @app.post("/services/{sub_id}/refresh-vendor")
    async def service_refresh_vendor(sub_id: int, _auth: None = Depends(_require_auth)):
        from app.telegram import customer_bridge

        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404)
            username = sub.username
        rq = customer_bridge.renew_queue
        if rq is None:
            return RedirectResponse(
                f"/services/{sub_id}?ok=vendor-unavailable", status_code=303
            )
        try:
            result = await rq.refresh_subscription(username)
        except Exception:
            return RedirectResponse(
                f"/services/{sub_id}?ok=vendor-failed", status_code=303
            )
        ok = "refreshed" if result == "ok" else "vendor-failed"
        return RedirectResponse(f"/services/{sub_id}?ok={ok}", status_code=303)

    @app.post("/services/{sub_id}/unlink")
    async def service_unlink(sub_id: int, _auth: None = Depends(_require_auth)):
        with get_session() as session:
            repo.unlink_subscription_owner(session, sub_id)
        return RedirectResponse(f"/services/{sub_id}?ok=unlinked", status_code=303)

    @app.get("/customers", response_class=HTMLResponse)
    async def customers(
        request: Request,
        q: str = "",
        page: int = 0,
        sort: str = "created",
        direction: str = "desc",
        partial: int = 0,
        _auth: None = Depends(_require_auth),
    ):
        page = max(0, page)
        page_size = 40
        sort = sort if sort in CUSTOMERS_SORTABLE else "created"
        direction = "asc" if direction == "asc" else "desc"
        with get_session() as session:
            stmt = select(CustomerProfile)
            if q.strip():
                digits = "".join(ch for ch in q if ch.isdigit())
                like = f"%{q.strip()}%"
                clauses = [
                    CustomerProfile.username.ilike(like),
                    CustomerProfile.full_name.ilike(like),
                ]
                if digits:
                    try:
                        clauses.append(CustomerProfile.telegram_id == int(digits))
                    except ValueError:
                        pass
                stmt = stmt.where(or_(*clauses))
            svc_count = (
                select(func.count(CustomerServiceLink.id))
                .where(
                    CustomerServiceLink.telegram_id == CustomerProfile.telegram_id
                )
                .correlate(CustomerProfile)
                .scalar_subquery()
            )
            name_key = func.lower(
                func.coalesce(
                    CustomerProfile.full_name,
                    CustomerProfile.username,
                    "",
                )
            )
            order_map = {
                "telegram": CustomerProfile.telegram_id,
                "name": name_key,
                "wallet": CustomerProfile.wallet_toman,
                "services": svc_count,
                "created": CustomerProfile.created_at,
            }
            order_col = order_map[sort]
            order_expr = asc(order_col) if direction == "asc" else desc(order_col)
            stmt = stmt.order_by(order_expr, desc(CustomerProfile.id))
            total = session.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = list(session.scalars(stmt.offset(page * page_size).limit(page_size)))
            items = []
            for p in rows:
                links = repo.list_customer_services(session, p.telegram_id)
                items.append({"profile": p, "services": len(links)})
        data = _ctx(
            request,
            "customers",
            items=items,
            q=q,
            page=page,
            pages=max(1, ((int(total or 0)) + page_size - 1) // page_size),
            total=int(total or 0),
            sort=sort,
            direction=direction,
            flash=request.query_params.get("ok"),
        )
        tpl = "customers_table.html" if partial else "customers.html"
        return TEMPLATES.TemplateResponse(request, tpl, data)

    @app.get("/customers/{tg_id}", response_class=HTMLResponse)
    async def customer_detail(
        request: Request, tg_id: int, _auth: None = Depends(_require_auth)
    ):
        with get_session() as session:
            profile = repo.get_customer_profile(session, tg_id)
            if not profile:
                raise HTTPException(404)
            links = repo.list_customer_services(session, tg_id)
            ledger = repo.list_wallet_ledger(session, tg_id, limit=30)
            orders = repo.list_customer_orders_for_user(session, tg_id, limit=20)
        return TEMPLATES.TemplateResponse(
            request,
            "customer_detail.html",
            _ctx(
                request,
                "customers",
                profile=profile,
                links=links,
                ledger=ledger,
                orders=orders,
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/customers/{tg_id}/wallet")
    async def customer_wallet_adjust(
        tg_id: int,
        amount: int = Form(...),
        note: str = Form("web_adjust"),
        _auth: None = Depends(_require_auth),
    ):
        with get_session() as session:
            if not repo.get_customer_profile(session, tg_id):
                raise HTTPException(404)
            repo.credit_wallet(
                session,
                tg_id,
                int(amount),
                kind="adjust",
                note=(note or "web_adjust")[:200],
                allow_overdraft=True,
            )
        return RedirectResponse(f"/customers/{tg_id}?ok=1", status_code=303)

    @app.post("/customers/{tg_id}/delete")
    async def customer_delete(tg_id: int, _auth: None = Depends(_require_auth)):
        with get_session() as session:
            repo.delete_customer_profile_keep_services(session, tg_id)
        return RedirectResponse("/customers?ok=deleted", status_code=303)

    @app.post("/customers/bulk")
    async def customers_bulk(
        action: str = Form(...),
        ids: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        id_list = _parse_id_list(ids)
        with get_session() as session:
            if action == "delete":
                for tg in id_list:
                    repo.delete_customer_profile_keep_services(session, tg)
        return RedirectResponse("/customers?ok=bulk", status_code=303)

    @app.get("/orders", response_class=HTMLResponse)
    async def orders(
        request: Request,
        status_f: str = "",
        page: int = 0,
        _auth: None = Depends(_require_auth),
    ):
        page = max(0, page)
        page_size = 40
        with get_session() as session:
            stmt = select(CustomerOrder).order_by(CustomerOrder.id.desc())
            if status_f:
                stmt = stmt.where(CustomerOrder.status == status_f)
            total = session.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = list(session.scalars(stmt.offset(page * page_size).limit(page_size)))
        return TEMPLATES.TemplateResponse(
            request,
            "orders.html",
            _ctx(
                request,
                "orders",
                rows=rows,
                status_f=status_f,
                page=page,
                pages=max(1, ((int(total or 0)) + page_size - 1) // page_size),
                total=int(total or 0),
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/orders/clear")
    async def orders_clear(
        scope: str = Form("all"),
        status_f: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        with get_session() as session:
            if scope == "filtered" and status_f:
                n = repo.clear_customer_orders(session, statuses=(status_f,))
            else:
                n = repo.clear_customer_orders(session)
        return RedirectResponse(f"/orders?ok=cleared-{n}", status_code=303)

    @app.post("/orders/bulk")
    async def orders_bulk(
        action: str = Form(...),
        ids: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        id_list = _parse_id_list(ids)
        with get_session() as session:
            if action == "delete":
                repo.delete_customer_orders_by_ids(session, id_list)
        return RedirectResponse("/orders?ok=1", status_code=303)

    @app.post("/orders/approve-all")
    async def orders_approve_all(_auth: None = Depends(_require_auth)):
        from app.services.admin_order_actions import approve_customer_order
        from app.telegram import customer_bridge

        with get_session() as session:
            open_rows = repo.list_customer_orders_by_statuses(
                session,
                ("awaiting_payment", "receipt_window", "awaiting_review", "approved"),
                limit=500,
            )
            ids = [int(o.id) for o in open_rows]
        ok_n = 0
        fail_n = 0
        for oid in ids:
            try:
                result = await customer_bridge.run_on_bot_loop(
                    approve_customer_order(
                        oid, source="admin_web_bulk", requested_by=0
                    )
                )
                if result.get("ok"):
                    ok_n += 1
                else:
                    fail_n += 1
            except Exception:
                fail_n += 1
                log.exception("approve-all failed order=%s", oid)
        return RedirectResponse(
            f"/orders?ok=approve-all-{ok_n}-fail-{fail_n}", status_code=303
        )

    @app.post("/orders/{order_id}/approve")
    async def order_approve(order_id: int, _auth: None = Depends(_require_auth)):
        from app.services.admin_order_actions import approve_customer_order
        from app.telegram import customer_bridge

        with get_session() as session:
            order = repo.get_customer_order(session, order_id)
            if not order:
                raise HTTPException(404)
        try:
            result = await customer_bridge.run_on_bot_loop(
                approve_customer_order(
                    order_id, source="admin_web", requested_by=0
                )
            )
        except Exception:
            return RedirectResponse("/orders?ok=approve-failed", status_code=303)
        if not result.get("ok"):
            return RedirectResponse(
                f"/orders?ok=approve-err-{result.get('error') or 'fail'}",
                status_code=303,
            )
        return RedirectResponse("/orders?ok=approved", status_code=303)

    @app.post("/orders/{order_id}/reject")
    async def order_reject(order_id: int, _auth: None = Depends(_require_auth)):
        from app.services.admin_order_actions import reject_customer_order
        from app.telegram import customer_bridge

        with get_session() as session:
            order = repo.get_customer_order(session, order_id)
            if not order:
                raise HTTPException(404)
        try:
            result = await customer_bridge.run_on_bot_loop(
                reject_customer_order(
                    order_id,
                    reason="سفارش از پلن ادمین رد شد.",
                    admin_id=0,
                    source="admin_web",
                )
            )
        except Exception:
            return RedirectResponse("/orders?ok=reject-failed", status_code=303)
        if not result.get("ok"):
            return RedirectResponse(
                f"/orders?ok=reject-err-{result.get('error') or 'fail'}",
                status_code=303,
            )
        return RedirectResponse("/orders?ok=rejected", status_code=303)

    @app.get("/tickets", response_class=HTMLResponse)
    async def tickets(
        request: Request,
        status_f: str = "",
        page: int = 0,
        _auth: None = Depends(_require_auth),
    ):
        page = max(0, page)
        page_size = 40
        with get_session() as session:
            statuses = (status_f,) if status_f else None
            rows = repo.list_support_tickets(
                session, statuses=statuses, limit=500, include_hidden=False
            )
            total = len(rows)
            chunk = rows[page * page_size : page * page_size + page_size]
        return TEMPLATES.TemplateResponse(
            request,
            "tickets.html",
            _ctx(
                request,
                "tickets",
                rows=chunk,
                status_f=status_f,
                page=page,
                pages=max(1, (total + page_size - 1) // page_size) if total else 1,
                total=total,
                flash=request.query_params.get("ok"),
            ),
        )

    @app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
    async def ticket_detail(
        request: Request, ticket_id: int, _auth: None = Depends(_require_auth)
    ):
        with get_session() as session:
            ticket = repo.get_support_ticket(session, ticket_id)
            if not ticket:
                raise HTTPException(404)
            messages = repo.list_support_messages(session, ticket_id)
        return TEMPLATES.TemplateResponse(
            request,
            "ticket_detail.html",
            _ctx(
                request,
                "tickets",
                ticket=ticket,
                messages=messages,
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/tickets/{ticket_id}/close")
    async def ticket_close(ticket_id: int, _auth: None = Depends(_require_auth)):
        with get_session() as session:
            repo.close_support_ticket(session, ticket_id)
        return RedirectResponse(f"/tickets/{ticket_id}?ok=closed", status_code=303)

    @app.post("/tickets/{ticket_id}/reply")
    async def ticket_reply(
        ticket_id: int, body: str = Form(""), _auth: None = Depends(_require_auth)
    ):
        text = (body or "").strip()
        if not text:
            return RedirectResponse(f"/tickets/{ticket_id}?ok=empty", status_code=303)
        with get_session() as session:
            ticket = repo.get_support_ticket(session, ticket_id)
            if not ticket:
                raise HTTPException(404)
            repo.add_support_message(
                session, ticket_id, sender="admin", body=text, admin_id=0
            )
            ticket.status = "awaiting_customer"
            ticket.updated_at = datetime.utcnow()
            tg = ticket.telegram_id
        from app.telegram import customer_bridge

        async def _send_ticket_reply() -> None:
            bot = customer_bridge.customer_bot
            if not bot:
                return
            await bot.send_message(tg, f"💬 پاسخ پشتیبانی:\n{text}")
            with get_session() as session:
                repo.set_state(session, f"cust_flow:{tg}", f"ticket:{ticket_id}")

        try:
            await customer_bridge.run_on_bot_loop(_send_ticket_reply())
        except Exception:
            return RedirectResponse(f"/tickets/{ticket_id}?ok=send-failed", status_code=303)
        return RedirectResponse(f"/tickets/{ticket_id}?ok=1", status_code=303)

    @app.post("/tickets/bulk")
    async def tickets_bulk(
        action: str = Form(...),
        ids: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        id_list = _parse_id_list(ids)
        with get_session() as session:
            if action == "close":
                for tid in id_list:
                    repo.close_support_ticket(session, tid)
            elif action == "hide":
                for tid in id_list:
                    t = repo.get_support_ticket(session, tid)
                    if t:
                        t.admin_hidden = True
        return RedirectResponse("/tickets?ok=bulk", status_code=303)

    @app.get("/transactions", response_class=HTMLResponse)
    async def transactions(
        request: Request,
        q: str = "",
        page: int = 0,
        _auth: None = Depends(_require_auth),
    ):
        page = max(0, page)
        page_size = 50
        with get_session() as session:
            stmt = select(WalletLedger).order_by(WalletLedger.id.desc())
            digits = "".join(ch for ch in q if ch.isdigit())
            if digits:
                try:
                    stmt = stmt.where(WalletLedger.telegram_id == int(digits))
                except ValueError:
                    pass
            total = session.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = list(session.scalars(stmt.offset(page * page_size).limit(page_size)))
        return TEMPLATES.TemplateResponse(
            request,
            "transactions.html",
            _ctx(
                request,
                "transactions",
                rows=rows,
                q=q,
                page=page,
                pages=max(1, ((int(total or 0)) + page_size - 1) // page_size),
                total=int(total or 0),
            ),
        )

    @app.get("/queue", response_class=HTMLResponse)
    async def queue(request: Request, _auth: None = Depends(_require_auth)):
        with get_session() as session:
            active_jobs = repo.list_active_renew_jobs(session, limit=50)
            recent_jobs = repo.list_recent_terminal_jobs(session, limit=20)
        return TEMPLATES.TemplateResponse(
            request,
            "queue.html",
            _ctx(
                request,
                "queue",
                active_jobs=active_jobs,
                recent_jobs=recent_jobs,
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/queue/clear")
    async def queue_clear(_auth: None = Depends(_require_auth)):
        with get_session() as session:
            repo.clear_renew_queue(session)
        return RedirectResponse("/queue?ok=cleared", status_code=303)

    @app.post("/queue/{job_id}/cancel")
    async def queue_cancel(job_id: int, _auth: None = Depends(_require_auth)):
        with get_session() as session:
            job = repo.get_renew_job(session, job_id)
            if job and job.status != "completed":
                repo.cancel_renew_job(job, "cancelled_by_web")
        return RedirectResponse("/queue?ok=1", status_code=303)

    @app.post("/queue/bulk")
    async def queue_bulk(
        action: str = Form(...),
        ids: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        id_list = _parse_id_list(ids)
        with get_session() as session:
            if action == "cancel":
                for jid in id_list:
                    job = repo.get_renew_job(session, jid)
                    if job and job.status != "completed":
                        repo.cancel_renew_job(job, "cancelled_by_web_bulk")
        return RedirectResponse("/queue?ok=1", status_code=303)

    @app.get("/ownership", response_class=HTMLResponse)
    async def ownership(
        request: Request, page: int = 0, _auth: None = Depends(_require_auth)
    ):
        page = max(0, page)
        page_size = 20
        with get_session() as session:
            rows, total = repo.list_unlinked_subscriptions_page(
                session, offset=page * page_size, limit=page_size
            )
            items = _build_service_items(session, rows)
        return TEMPLATES.TemplateResponse(
            request,
            "ownership.html",
            _ctx(
                request,
                "ownership",
                rows=rows,
                items=items,
                page=page,
                pages=max(1, (total + page_size - 1) // page_size) if total else 1,
                total=total,
                flash=request.query_params.get("ok"),
                err=request.query_params.get("e"),
            ),
        )

    @app.post("/ownership/assign")
    async def ownership_assign(
        owner_tg: str = Form(...),
        sub_ids: str = Form(...),
        _auth: None = Depends(_require_auth),
    ):
        digits = "".join(ch for ch in owner_tg if ch.isdigit())
        if len(digits) < 5:
            return RedirectResponse("/ownership?e=tg", status_code=303)
        ids = _parse_id_list(sub_ids)
        with get_session() as session:
            if not repo.get_customer_profile(session, int(digits)):
                return RedirectResponse("/ownership?e=profile", status_code=303)
            repo.assign_subscriptions_to_owner(
                session,
                subscription_ids=ids,
                to_telegram_id=int(digits),
                initiated_by_telegram_id=0,
                note="admin_web",
            )
        return RedirectResponse("/ownership?ok=1", status_code=303)

    @app.get("/prices", response_class=HTMLResponse)
    async def prices(request: Request, _auth: None = Depends(_require_auth)):
        with get_session() as session:
            plans = repo.list_price_plans(session, enabled_only=False)
        return TEMPLATES.TemplateResponse(
            request,
            "prices.html",
            _ctx(
                request,
                "prices",
                plans=plans,
                flash=request.query_params.get("ok"),
                warn=request.query_params.get("warn"),
                renamed=request.query_params.get("renamed"),
            ),
        )

    @app.post("/prices/{plan_id}")
    async def price_save(
        plan_id: int,
        price_toman: int = Form(...),
        title: str = Form(""),
        enabled: str = Form("off"),
        also_rename_products: str = Form("off"),
        _auth: None = Depends(_require_auth),
    ):
        warn = ""
        renamed = 0
        with get_session() as session:
            plan = repo.get_price_plan(session, plan_id)
            if not plan:
                raise HTTPException(404)
            old_title = (plan.title or "").strip()
            new_title = (title or "").strip() or old_title
            plan.price_toman = int(price_toman)
            plan.enabled = enabled in {"on", "1", "true", "yes"}
            if new_title != old_title:
                do_products = also_rename_products in {"on", "1", "true", "yes"}
                _plan, renamed = repo.rename_price_plan(
                    session,
                    plan_id,
                    title=new_title,
                    also_rename_product_names=do_products,
                )
                if not do_products:
                    warn = "1"
            plan.updated_at = datetime.utcnow()
        qs = "ok=1"
        if warn:
            qs += "&warn=rename"
        if renamed:
            qs += f"&renamed={renamed}"
        return RedirectResponse(f"/prices?{qs}", status_code=303)

    @app.post("/prices/{plan_id}/move")
    async def price_move(
        plan_id: int, direction: str = Form(...), _auth: None = Depends(_require_auth)
    ):
        with get_session() as session:
            repo.reorder_price_plan(session, plan_id, direction=direction)
        return RedirectResponse("/prices?ok=1", status_code=303)

    @app.post("/prices/{plan_id}/delete")
    async def price_delete(plan_id: int, _auth: None = Depends(_require_auth)):
        with get_session() as session:
            repo.delete_price_plan(session, plan_id)
        return RedirectResponse("/prices?ok=deleted", status_code=303)

    @app.post("/prices/save-all")
    async def prices_save_all(request: Request, _auth: None = Depends(_require_auth)):
        form = await request.form()
        updated = 0
        with get_session() as session:
            plans = repo.list_price_plans(session, enabled_only=False)
            for plan in plans:
                pid = plan.id
                title_key = f"title_{pid}"
                price_key = f"price_toman_{pid}"
                enabled_key = f"enabled_{pid}"
                if title_key not in form and price_key not in form:
                    continue
                title_raw = form.get(title_key)
                price_raw = form.get(price_key)
                if title_raw is not None:
                    new_title = str(title_raw).strip()
                    if new_title:
                        plan.title = new_title
                if price_raw is not None:
                    try:
                        plan.price_toman = int(str(price_raw).replace(",", ""))
                    except ValueError:
                        pass
                plan.enabled = enabled_key in form
                plan.updated_at = datetime.utcnow()
                updated += 1
            session.flush()
        return RedirectResponse(f"/prices?ok=saved-{updated}", status_code=303)

    @app.get("/warnings", response_class=HTMLResponse)
    async def vendor_warnings(
        request: Request,
        q: str = "",
        status_f: str = "",
        page: int = 0,
        sort: str = "expire",
        direction: str = "asc",
        _auth: None = Depends(_require_auth),
    ):
        page = max(0, page)
        page_size = 40
        status_ok = {"warning", "expired", "volume_exhausted"}
        status_f = status_f if status_f in status_ok else ""
        sort = sort if sort in WARNINGS_SORTABLE else "expire"
        direction = "asc" if direction == "asc" else "desc"
        with get_session() as session:
            rows, _total_all = repo.list_warning_subscriptions(
                session, offset=0, limit=10000
            )
            items: list[dict[str, Any]] = []
            for sub in rows:
                if status_f and (sub.status or "") != status_f:
                    continue
                if q.strip():
                    needle = q.strip().lower()
                    hay = (sub.username or "").lower()
                    if needle not in hay and needle not in (
                        sub.product_name or ""
                    ).lower():
                        continue
                v = repo.get_vendor(session, sub.vendor_id) if sub.vendor_id else None
                vkey = v.key if v else None
                link = repo.get_link_for_subscription(session, sub.id)
                url = (sub.subscription_url or "").strip()
                items.append(
                    {
                        "sub": sub,
                        "vendor_key": vkey or "—",
                        "vendor_icon": vendor_icon(vkey),
                        "plan_icon": plan_icon(sub, vendor_key=vkey),
                        "status_icon": status_icon(sub.status),
                        "owner": link.telegram_id if link else None,
                        "has_url": bool(url.startswith("http")),
                    }
                )
            items = _sort_alert_items(
                items, sort=sort, direction=direction, allowed=WARNINGS_SORTABLE
            )
            total = len(items)
            chunk = items[page * page_size : page * page_size + page_size]
        return TEMPLATES.TemplateResponse(
            request,
            "warnings.html",
            _ctx(
                request,
                "warnings",
                items=chunk,
                q=q,
                status_f=status_f,
                page=page,
                pages=max(1, (total + page_size - 1) // page_size) if total else 1,
                total=total,
                sort=sort,
                direction=direction,
                flash=request.query_params.get("ok"),
            ),
        )

    def _warnings_back(
        q: str = "",
        status_f: str = "",
        page: int = 0,
        sort: str = "",
        direction: str = "",
    ) -> str:
        from urllib.parse import urlencode

        params: dict[str, str] = {}
        if q.strip():
            params["q"] = q.strip()
        if status_f.strip():
            params["status_f"] = status_f.strip()
        if sort.strip():
            params["sort"] = sort.strip()
        if direction.strip():
            params["direction"] = direction.strip()
        if page > 0:
            params["page"] = str(page)
        qs = urlencode(params)
        return f"/warnings?{qs}" if qs else "/warnings"

    @app.post("/warnings/clear-hidden")
    async def warnings_clear_hidden(_auth: None = Depends(_require_auth)):
        with get_session() as session:
            repo.clear_vendor_warning_hidden(session)
        return RedirectResponse("/warnings?ok=unhidden", status_code=303)

    @app.post("/warnings/check-renewed")
    async def warnings_check_renewed(_auth: None = Depends(_require_auth)):
        with get_session() as session:
            stats = repo.clear_recently_renewed_from_alert_lists(session)
        return RedirectResponse(
            "/warnings?ok="
            f"cleared-{stats['vendor_cleared']}"
            f"-stamped-{stats['stamped']}"
            f"-recent-{stats['recent_renewed']}",
            status_code=303,
        )

    @app.post("/warnings/hide-all")
    async def warnings_hide_all(_auth: None = Depends(_require_auth)):
        with get_session() as session:
            n = repo.hide_all_vendor_warnings_visible(session)
        return RedirectResponse(f"/warnings?ok=hidden-{n}", status_code=303)

    @app.post("/warnings/{sub_id}/refresh-url")
    async def warnings_refresh_url(
        sub_id: int,
        q: str = Form(""),
        status_f: str = Form(""),
        page: int = Form(0),
        sort: str = Form(""),
        direction: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        from app.services.subscription_refresh import refresh_subscription_userinfo

        back = _warnings_back(q, status_f, int(page or 0), sort, direction)
        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404)
            if not (sub.subscription_url or "").strip().startswith("http"):
                return RedirectResponse(f"{back}&ok=no-url", status_code=303)
            try:
                refresh_subscription_userinfo(session, subscription=sub, force=True)
            except Exception:
                return RedirectResponse(f"{back}&ok=refresh-failed", status_code=303)
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}ok=refreshed", status_code=303)

    @app.post("/warnings/{sub_id}/hide")
    async def warnings_hide(
        sub_id: int,
        q: str = Form(""),
        status_f: str = Form(""),
        page: int = Form(0),
        sort: str = Form(""),
        direction: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        back = _warnings_back(q, status_f, int(page or 0), sort, direction)
        with get_session() as session:
            repo.hide_vendor_warning_subscription(session, sub_id)
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}ok=hidden", status_code=303)

    @app.post("/warnings/{sub_id}/mark-active")
    async def warnings_mark_active(
        sub_id: int,
        q: str = Form(""),
        status_f: str = Form(""),
        page: int = Form(0),
        sort: str = Form(""),
        direction: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        back = _warnings_back(q, status_f, int(page or 0), sort, direction)
        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404)
            sub.status = "active"
            sub.vendor_alert_at = None
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}ok=active", status_code=303)

    @app.get("/almost-out", response_class=HTMLResponse)
    async def almost_out(
        request: Request,
        q: str = "",
        reason_f: str = "",
        page: int = 0,
        sort: str = "expire",
        direction: str = "asc",
        _auth: None = Depends(_require_auth),
    ):
        page = max(0, page)
        page_size = 40
        sort = sort if sort in ALMOST_OUT_SORTABLE else "expire"
        direction = "asc" if direction == "asc" else "desc"
        with get_session() as session:
            raw_items, _total_all = repo.list_local_almost_out(
                session, offset=0, limit=10000
            )
            items: list[dict[str, Any]] = []
            for sub, reasons in raw_items:
                if reason_f and reason_f not in reasons:
                    continue
                if q.strip():
                    needle = q.strip().lower()
                    hay = (sub.username or "").lower()
                    if needle not in hay and needle not in (sub.product_name or "").lower():
                        continue
                v = repo.get_vendor(session, sub.vendor_id) if sub.vendor_id else None
                vkey = v.key if v else None
                link = repo.get_link_for_subscription(session, sub.id)
                url = (sub.subscription_url or "").strip()
                items.append(
                    {
                        "sub": sub,
                        "reasons": reasons,
                        "vendor_key": vkey or "—",
                        "vendor_icon": vendor_icon(vkey),
                        "plan_icon": plan_icon(sub, vendor_key=vkey),
                        "status_icon": status_icon(sub.status),
                        "owner": link.telegram_id if link else None,
                        "has_url": bool(url.startswith("http")),
                    }
                )
            items = _sort_alert_items(
                items, sort=sort, direction=direction, allowed=ALMOST_OUT_SORTABLE
            )
            total = len(items)
            chunk = items[page * page_size : page * page_size + page_size]
            days_th = repo.get_alert_days_threshold(session)
            traffic_th = repo.get_alert_traffic_mb_threshold(session)
            life_scan = repo.get_lifecycle_scan_interval_seconds(session)
            from app.utils.duration import format_seconds_en, format_seconds_fa

            life_scan_label = (
                format_seconds_fa(life_scan)
                if _lang(request) == "fa"
                else format_seconds_en(life_scan)
            )
        return TEMPLATES.TemplateResponse(
            request,
            "almost_out.html",
            _ctx(
                request,
                "almost_out",
                items=chunk,
                q=q,
                reason_f=reason_f,
                page=page,
                pages=max(1, (total + page_size - 1) // page_size) if total else 1,
                total=total,
                sort=sort,
                direction=direction,
                days_th=days_th,
                traffic_th=traffic_th,
                life_scan=life_scan,
                life_scan_label=life_scan_label,
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/almost-out/check")
    async def almost_out_check_all(_auth: None = Depends(_require_auth)):
        from app.services.lifecycle_alerts import check_all_services_for_almost_out

        try:
            # Owns its own short DB sessions (do not wrap in get_session here).
            stats = check_all_services_for_almost_out(force=True)
        except Exception:
            log.exception("almost-out check-all failed")
            return RedirectResponse("/almost-out?ok=check-failed", status_code=303)
        return RedirectResponse(
            "/almost-out?ok="
            f"checked-{stats['checked']}-almost-{stats['almost_out']}"
            f"-fail-{stats['failed']}",
            status_code=303,
        )

    @app.post("/almost-out/check-renewed")
    async def almost_out_check_renewed(_auth: None = Depends(_require_auth)):
        with get_session() as session:
            stats = repo.clear_recently_renewed_from_alert_lists(session)
        return RedirectResponse(
            "/almost-out?ok="
            f"renew-sync-stamped-{stats['stamped']}"
            f"-recent-{stats['recent_renewed']}",
            status_code=303,
        )

    def _almost_out_back(
        q: str = "",
        reason_f: str = "",
        page: int = 0,
        sort: str = "",
        direction: str = "",
    ) -> str:
        from urllib.parse import urlencode

        params: dict[str, str] = {}
        if q.strip():
            params["q"] = q.strip()
        if reason_f.strip():
            params["reason_f"] = reason_f.strip()
        if sort.strip():
            params["sort"] = sort.strip()
        if direction.strip():
            params["direction"] = direction.strip()
        if page > 0:
            params["page"] = str(page)
        qs = urlencode(params)
        return f"/almost-out?{qs}" if qs else "/almost-out"

    @app.post("/almost-out/clear-hidden")
    async def almost_out_clear_hidden(_auth: None = Depends(_require_auth)):
        with get_session() as session:
            repo.clear_almost_out_hidden(session)
        return RedirectResponse("/almost-out?ok=unhidden", status_code=303)

    @app.post("/almost-out/{sub_id}/refresh-url")
    async def almost_out_refresh_url(
        sub_id: int,
        q: str = Form(""),
        reason_f: str = Form(""),
        page: int = Form(0),
        sort: str = Form(""),
        direction: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        from app.services.subscription_refresh import refresh_subscription_userinfo

        back = _almost_out_back(q, reason_f, int(page or 0), sort, direction)
        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404)
            if not (sub.subscription_url or "").strip().startswith("http"):
                return RedirectResponse(f"{back}&ok=no-url", status_code=303)
            try:
                refresh_subscription_userinfo(session, subscription=sub, force=True)
            except Exception:
                return RedirectResponse(f"{back}&ok=refresh-failed", status_code=303)
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}ok=refreshed", status_code=303)

    @app.post("/almost-out/{sub_id}/hide")
    async def almost_out_hide(
        sub_id: int,
        q: str = Form(""),
        reason_f: str = Form(""),
        page: int = Form(0),
        sort: str = Form(""),
        direction: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        back = _almost_out_back(q, reason_f, int(page or 0), sort, direction)
        with get_session() as session:
            repo.hide_almost_out_subscription(session, sub_id)
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}ok=hidden", status_code=303)

    @app.post("/almost-out/{sub_id}/mark-active")
    async def almost_out_mark_active(
        sub_id: int,
        q: str = Form(""),
        reason_f: str = Form(""),
        page: int = Form(0),
        sort: str = Form(""),
        direction: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        back = _almost_out_back(q, reason_f, int(page or 0), sort, direction)
        with get_session() as session:
            sub = session.get(Subscription, sub_id)
            if not sub:
                raise HTTPException(404)
            sub.status = "active"
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}ok=active", status_code=303)

    @app.get("/messages", response_class=HTMLResponse)
    async def messages_page(
        request: Request,
        tab: str = Query("customer"),
        _auth: None = Depends(_require_auth),
    ):
        bucket = "admin" if (tab or "").strip().lower() == "admin" else "customer"
        with get_session() as session:
            all_rows = repo.list_bot_messages(session)
        if bucket == "admin":
            rows = [r for r in all_rows if str(r.key).startswith("admin.")]
        else:
            rows = [r for r in all_rows if not str(r.key).startswith("admin.")]
        return TEMPLATES.TemplateResponse(
            request,
            "messages.html",
            _ctx(
                request,
                "messages",
                rows=rows,
                tab=bucket,
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/messages/{key}/save")
    async def messages_save(
        key: str,
        body: str = Form(""),
        tab: str = Form("customer"),
        _auth: None = Depends(_require_auth),
    ):
        safe_key = (key or "").strip()
        if not safe_key:
            raise HTTPException(400)
        bucket = "admin" if (tab or "").strip().lower() == "admin" else "customer"
        with get_session() as session:
            repo.set_bot_message(session, safe_key, body)
        return RedirectResponse(
            f"/messages?tab={bucket}&ok={safe_key}", status_code=303
        )

    @app.post("/messages/save-all")
    async def messages_save_all(request: Request, _auth: None = Depends(_require_auth)):
        form = await request.form()
        bucket = (
            "admin"
            if str(form.get("tab") or "").strip().lower() == "admin"
            else "customer"
        )
        n = 0
        with get_session() as session:
            for key, value in form.multi_items():
                if not str(key).startswith("body__"):
                    continue
                msg_key = str(key)[len("body__") :].strip()
                if not msg_key:
                    continue
                repo.set_bot_message(session, msg_key, str(value or ""))
                n += 1
        return RedirectResponse(
            f"/messages?tab={bucket}&ok=all_{n}", status_code=303
        )

    @app.get("/menus", response_class=HTMLResponse)
    async def menus_page(
        request: Request,
        bot: str = Query("customer"),
        _auth: None = Depends(_require_auth),
    ):
        from app.telegram import menu_config as menu_cfg

        which = "admin" if (bot or "").strip().lower() == "admin" else "customer"
        return TEMPLATES.TemplateResponse(
            request,
            "menus.html",
            _ctx(
                request,
                "menus",
                bot=which,
                snap=menu_cfg.panel_snapshot(which),
                flash=request.query_params.get("ok"),
                err=request.query_params.get("err"),
            ),
        )

    @app.post("/menus/{bot}/save-all")
    async def menus_save_all(
        bot: str, request: Request, _auth: None = Depends(_require_auth)
    ):
        from app.telegram import menu_config as menu_cfg
        from urllib.parse import quote

        which = "admin" if (bot or "").strip().lower() == "admin" else "customer"
        form = await request.form()

        def _labels(prefix: str) -> dict[str, str]:
            out: dict[str, str] = {}
            for key, value in form.multi_items():
                k = str(key)
                if not k.startswith(prefix):
                    continue
                action = k[len(prefix) :]
                text = str(value or "").strip()
                if action and text:
                    out[action] = text
            return out

        try:
            main_layout, _ignored = menu_cfg.parse_layout_text(
                which, str(form.get("layout_main") or "")
            )
            menu_cfg.save_reply_menu(which, main_layout, _labels("label_main_"))

            if which == "customer":
                support_raw = str(form.get("layout_support") or "").strip()
                if support_raw:
                    support_layout, _ign2 = menu_cfg.parse_layout_text(
                        "customer_support", support_raw
                    )
                    menu_cfg.save_reply_menu(
                        "customer_support",
                        support_layout,
                        _labels("label_support_"),
                    )

            inline: dict[str, dict[str, str]] = {}
            order_map: dict[str, list[str]] = {}
            for key, value in form.multi_items():
                k = str(key)
                if k.startswith("inline_order__"):
                    menu_id = k[len("inline_order__") :]
                    order_map[menu_id] = [
                        p.strip()
                        for p in str(value or "").split(",")
                        if p.strip()
                    ]
                    continue
                if not k.startswith("inline__"):
                    continue
                rest = k[len("inline__") :]
                if "__" not in rest:
                    continue
                menu_id, item_id = rest.split("__", 1)
                text = str(value or "").strip()
                if not text:
                    continue
                inline.setdefault(menu_id, {})[item_id] = text
            if which == "admin":
                menu_cfg.save_admin_inline_labels(inline, order=order_map)
            else:
                menu_cfg.save_customer_inline_labels(inline, order=order_map)
        except ValueError as exc:
            return RedirectResponse(
                f"/menus?bot={which}&err={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(f"/menus?bot={which}&ok=saved", status_code=303)

    @app.post("/menus/{bot}/save")
    async def menus_save(
        bot: str,
        layout_text: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        from app.telegram import menu_config as menu_cfg

        which = "admin" if (bot or "").strip().lower() == "admin" else "customer"
        try:
            layout, labels = menu_cfg.parse_layout_text(which, layout_text)
            menu_cfg.save_reply_menu(which, layout, labels)
        except ValueError as exc:
            from urllib.parse import quote

            return RedirectResponse(
                f"/menus?bot={which}&err={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(f"/menus?bot={which}&ok=saved", status_code=303)

    @app.post("/menus/{bot}/reset")
    async def menus_reset(bot: str, _auth: None = Depends(_require_auth)):
        from app.telegram import menu_config as menu_cfg

        which = "admin" if (bot or "").strip().lower() == "admin" else "customer"
        menu_cfg.reset_menu(which)
        if which == "customer":
            menu_cfg.reset_menu("customer_support")
        else:
            menu_cfg.reset_menu("admin_inline")
        return RedirectResponse(f"/menus?bot={which}&ok=reset", status_code=303)

    @app.get("/vendors", response_class=HTMLResponse)
    async def vendors(request: Request, _auth: None = Depends(_require_auth)):
        settings = _settings()
        with get_session() as session:
            rows = list(session.scalars(select(Vendor).order_by(Vendor.id)))
            enriched = []
            for v in rows:
                n = session.scalar(
                    select(func.count())
                    .select_from(Subscription)
                    .where(Subscription.vendor_id == v.id)
                )
                enriched.append({"v": v, "n": int(n or 0)})
            sync_report = (repo.get_state(session, "admin_web_vendor_sync_report") or "").strip()
        return TEMPLATES.TemplateResponse(
            request,
            "vendors.html",
            _ctx(
                request,
                "vendors",
                rows=enriched,
                netplus=settings.netplus_bot,
                finearts=settings.finearts_bot,
                sync_report=sync_report,
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/vendors/{vendor_id}/strategy")
    async def vendor_strategy(
        vendor_id: int,
        info_refresh_strategy: str = Form("automatic"),
        enabled: str = Form("on"),
        _auth: None = Depends(_require_auth),
    ):
        with get_session() as session:
            v = session.get(Vendor, vendor_id)
            if not v:
                raise HTTPException(404)
            v.info_refresh_strategy = (info_refresh_strategy or "automatic").strip()
            v.enabled = enabled in {"on", "1", "true", "yes"}
        return RedirectResponse("/vendors", status_code=303)

    @app.post("/vendors/sync")
    async def vendors_sync(_auth: None = Depends(_require_auth)):
        import logging

        from app.services.inventory_incremental_sync import run_manual_incremental_sync
        from app.telegram import customer_bridge

        log = logging.getLogger(__name__)
        rq = customer_bridge.renew_queue
        settings = _settings()
        lines: list[str] = []
        with get_session() as session:
            vendors = list(session.scalars(select(Vendor).order_by(Vendor.id)))
        if rq is None:
            summary = "renew_queue unavailable (start vpn-manager bots)"
            with get_session() as session:
                repo.set_state(session, "admin_web_vendor_sync_report", summary)
            return RedirectResponse(
                "/vendors?ok=sync-unavailable&act1=Vendors&act1href=/vendors",
                status_code=303,
            )
        for v in vendors:
            if not v.enabled:
                lines.append(f"{v.key}: skipped (disabled)")
                continue
            bot = (v.telegram_username or "").strip()
            if not bot:
                if v.key == "finearts":
                    bot = settings.finearts_bot
                else:
                    bot = settings.netplus_bot
            try:
                report = await run_manual_incremental_sync(
                    renew_queue=rq,
                    vendor_key=v.key,
                    vendor_name=v.name,
                    bot_username=bot,
                    max_pages=50,
                )
                live = getattr(report, "live_n", None)
                if live is None:
                    live = getattr(report, "live_count", "?")
                added = getattr(report, "added", getattr(report, "added_count", 0))
                updated = getattr(report, "updated", getattr(report, "updated_count", 0))
                removed = getattr(report, "removed", getattr(report, "removed_count", 0))
                lines.append(
                    f"{v.key}: live={live} +{added} ~{updated} -{removed}"
                )
            except Exception as exc:
                log.exception("vendor sync %s failed", v.key)
                lines.append(f"{v.key}: error ({exc})")
        summary = "; ".join(lines) if lines else "no vendors"
        with get_session() as session:
            repo.set_state(session, "admin_web_vendor_sync_report", summary[:4000])
        return RedirectResponse(
            "/vendors?ok=sync&act1=Vendors&act1href=/vendors",
            status_code=303,
        )

    @app.get("/api/panel-status")
    async def panel_status(_auth: None = Depends(_require_auth)):
        badges = _panel_badges()
        return {
            "ok": True,
            "badges": badges,
            "ts": datetime.utcnow().isoformat() + "Z",
        }

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, _auth: None = Depends(_require_auth)):
        settings = _settings()
        with get_session() as session:
            card_n, card_h = repo.get_payment_card(
                session, settings.reseller_card_number, settings.reseller_card_holder
            )
            wallet_min, wallet_max = repo.get_wallet_limits(session)
            order_ttl = repo.get_order_ttl_seconds(session)
            reminder_delays = repo.get_reminder_delays_seconds(session)
            userinfo_ttl = repo.get_userinfo_ttl_seconds(session)
            alert_days = repo.get_alert_days_threshold(session)
            alert_traffic = repo.get_alert_traffic_mb_threshold(session)
            life_cool = repo.get_lifecycle_alert_cooldown_seconds(session)
            life_scan = repo.get_lifecycle_scan_interval_seconds(session)
            admin_alert_max_repeats = repo.get_admin_vendor_alert_max_repeats(session)
            admin_alert_window_hours = repo.get_admin_vendor_alert_window_hours(session)
            admin_alert_deleted_suppress_hours = (
                repo.get_admin_vendor_deleted_suppress_hours(session)
            )
            orders_manual_approval = repo.is_orders_manual_approval(session)
            vendor_settle_mode = repo.get_vendor_settle_mode(session)
            customer_bot_enabled = repo.is_customer_bot_enabled(session)
            customer_bot_disabled_message = repo.get_customer_bot_disabled_message(
                session
            )
        from app.utils.duration import (
            format_minutes_csv,
            seconds_to_minutes_value,
        )

        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                "settings",
                card_number=card_n,
                card_number_display=card_grouped(card_n),
                card_holder=card_h,
                payment_provider=settings.payment_provider,
                wallet_min=wallet_min,
                wallet_max=wallet_max,
                order_ttl_min=seconds_to_minutes_value(order_ttl),
                reminder_delays_min=format_minutes_csv(reminder_delays),
                userinfo_ttl_min=seconds_to_minutes_value(userinfo_ttl),
                alert_days=alert_days,
                alert_traffic=alert_traffic,
                life_cool_min=seconds_to_minutes_value(life_cool),
                life_scan_min=seconds_to_minutes_value(life_scan),
                admin_alert_max_repeats=admin_alert_max_repeats,
                admin_alert_window_hours=admin_alert_window_hours,
                admin_alert_deleted_suppress_hours=admin_alert_deleted_suppress_hours,
                orders_manual_approval=orders_manual_approval,
                vendor_settle_mode=vendor_settle_mode,
                customer_bot_enabled=customer_bot_enabled,
                customer_bot_disabled_message=customer_bot_disabled_message,
                flash=request.query_params.get("ok"),
            ),
        )

    @app.post("/settings/card")
    async def settings_card(
        card_number: str = Form(""),
        card_holder: str = Form(""),
        _auth: None = Depends(_require_auth),
    ):
        with get_session() as session:
            repo.set_payment_card(session, card_digits(card_number), card_holder)
        return RedirectResponse("/settings?ok=1", status_code=303)

    @app.post("/settings/save-all")
    async def settings_save_all(request: Request, _auth: None = Depends(_require_auth)):
        form = await request.form()
        with get_session() as session:
            repo.set_payment_card(
                session,
                card_digits(str(form.get("card_number") or "")),
                str(form.get("card_holder") or ""),
            )
            try:
                mn = int(str(form.get("wallet_min") or "0").replace(",", ""))
                mx = int(str(form.get("wallet_max") or "0").replace(",", ""))
                repo.set_wallet_limits(session, mn, mx)
            except ValueError:
                pass
            from app.utils.duration import minutes_to_seconds, parse_minutes_list

            try:
                repo.set_order_ttl_seconds(
                    session,
                    minutes_to_seconds(str(form.get("order_ttl_min") or "0")),
                )
            except ValueError:
                pass
            delay_parts = parse_minutes_list(str(form.get("reminder_delays_min") or ""))
            if delay_parts:
                repo.set_reminder_delays_seconds(session, delay_parts)
            try:
                repo.set_userinfo_ttl_seconds(
                    session,
                    minutes_to_seconds(str(form.get("userinfo_ttl_min") or "0")),
                )
            except ValueError:
                pass
            try:
                repo.set_alert_days_threshold(
                    session, int(float(str(form.get("alert_days") or "0")))
                )
            except ValueError:
                pass
            try:
                repo.set_alert_traffic_mb_threshold(
                    session, float(str(form.get("alert_traffic") or "0"))
                )
            except ValueError:
                pass
            try:
                repo.set_lifecycle_alert_cooldown_seconds(
                    session,
                    minutes_to_seconds(str(form.get("life_cool_min") or "0")),
                )
            except ValueError:
                pass
            try:
                repo.set_lifecycle_scan_interval_seconds(
                    session,
                    minutes_to_seconds(str(form.get("life_scan_min") or "0")),
                )
            except ValueError:
                pass
            repo.set_orders_manual_approval(
                session,
                str(form.get("orders_manual_approval") or "").strip()
                in {"1", "on", "true", "yes"},
            )
            repo.set_vendor_settle_mode(
                session, str(form.get("vendor_settle_mode") or "wallet")
            )
            try:
                repo.set_admin_vendor_alert_max_repeats(
                    session,
                    int(float(str(form.get("admin_alert_max_repeats") or "0"))),
                )
            except ValueError:
                pass
            try:
                repo.set_admin_vendor_alert_window_hours(
                    session,
                    int(float(str(form.get("admin_alert_window_hours") or "0"))),
                )
            except ValueError:
                pass
            try:
                repo.set_admin_vendor_deleted_suppress_hours(
                    session,
                    int(
                        float(
                            str(form.get("admin_alert_deleted_suppress_hours") or "0")
                        )
                    ),
                )
            except ValueError:
                pass
            enabled = str(form.get("customer_bot_enabled") or "").strip() in {
                "1",
                "on",
                "true",
                "yes",
            }
            repo.set_customer_bot_maintenance(
                session,
                enabled=enabled,
                message=str(form.get("customer_bot_disabled_message") or ""),
            )
        return RedirectResponse("/settings?ok=saved", status_code=303)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.getenv("ADMIN_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("ADMIN_WEB_PORT", "8787"))
    uvicorn.run("app.web.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
