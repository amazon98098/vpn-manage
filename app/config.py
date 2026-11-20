"""Application configuration loaded from the .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    user_session: Path
    admin_bot_token: str
    customer_bot_token: str
    admin_ids: frozenset[int]
    netplus_bot: str
    finearts_bot: str
    reseller_card_number: str
    reseller_card_holder: str
    support_text: str
    payment_provider: str
    abangateway_api_key: str
    abangateway_api_base: str
    abangateway_callback_url: str
    abangateway_webhook_secret: str
    varizapp_api_key: str
    varizapp_key_id: str
    varizapp_api_base: str
    varizapp_callback_url: str
    varizapp_webhook_secret: str
    alert_max_age_hours: float
    notify_success: bool
    bot_proxy: str | None
    log_level: str
    renew_auto_approve_seconds: float
    renew_receipt_after_seconds: float
    renew_payment_timeout_seconds: float
    queue_gap_seconds: float
    info_cache_ttl_seconds: float
    info_http_timeout_seconds: float
    info_http_concurrency: int
    data_dir: Path = field(default=ROOT / "data")
    logs_dir: Path = field(default=ROOT / "logs")

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'vpn_manager.db'}"


def load_settings(require_bot: bool = True) -> Settings:
    missing = [k for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH") if not os.getenv(k)]
    if require_bot:
        missing += [k for k in ("ADMIN_BOT_TOKEN", "ADMIN_IDS") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing settings in .env: {', '.join(missing)}")

    admin_ids = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    settings = Settings(
        api_id=int(os.environ["TELEGRAM_API_ID"]),
        api_hash=os.environ["TELEGRAM_API_HASH"],
        user_session=_path(os.getenv("USER_SESSION", "data/sessions/main_account")),
        admin_bot_token=os.getenv("ADMIN_BOT_TOKEN", ""),
        customer_bot_token=os.getenv("CUSTOMER_BOT_TOKEN", ""),
        admin_ids=admin_ids,
        netplus_bot=os.getenv("NETPLUS_BOT", "@Bombejokerbot"),
        finearts_bot=os.getenv("FINEARTS_BOT", "@FineArtsVPNBot"),
        reseller_card_number=os.getenv("RESELLER_CARD_NUMBER", "").replace(" ", ""),
        reseller_card_holder=os.getenv("RESELLER_CARD_HOLDER", ""),
        support_text=os.getenv(
            "SUPPORT_TEXT",
            "پیام خود را همین‌جا بفرستید؛ پشتیبانی پاسخ می‌دهد.",
        ),
        payment_provider=(os.getenv("PAYMENT_PROVIDER", "aban") or "aban").strip().lower(),
        abangateway_api_key=os.getenv("ABANGATEWAY_API_KEY", "").strip(),
        abangateway_api_base=os.getenv(
            "ABANGATEWAY_API_BASE", "https://api.abangateway.ir/api/v1"
        ).rstrip("/"),
        abangateway_callback_url=os.getenv("ABANGATEWAY_CALLBACK_URL", "").strip(),
        abangateway_webhook_secret=os.getenv("ABANGATEWAY_WEBHOOK_SECRET", "").strip(),
        varizapp_api_key=os.getenv("VARIZAPP_API_KEY", "").strip(),
        varizapp_key_id=os.getenv("VARIZAPP_KEY_ID", "").strip(),
        varizapp_api_base=os.getenv(
            "VARIZAPP_API_BASE", "https://api.variz.app/api"
        ).rstrip("/"),
        # Required by Variz create; we poll verify instead of relying on redirect/webhook
        varizapp_callback_url=os.getenv(
            "VARIZAPP_CALLBACK_URL", "https://t.me/fxonevpnbot"
        ).strip(),
        varizapp_webhook_secret=os.getenv("VARIZAPP_WEBHOOK_SECRET", "").strip(),
        alert_max_age_hours=float(os.getenv("ALERT_MAX_AGE_HOURS", "12")),
        notify_success=_bool(os.getenv("NOTIFY_SUCCESS"), False),
        bot_proxy=os.getenv("BOT_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or None,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        renew_auto_approve_seconds=float(os.getenv("RENEW_AUTO_APPROVE_SECONDS", "180")),
        renew_receipt_after_seconds=float(os.getenv("RENEW_RECEIPT_AFTER_SECONDS", "240")),
        renew_payment_timeout_seconds=float(
            os.getenv("RENEW_PAYMENT_TIMEOUT_SECONDS", "900")
        ),
        queue_gap_seconds=float(os.getenv("QUEUE_GAP_SECONDS", "10")),
        info_cache_ttl_seconds=float(os.getenv("INFO_CACHE_TTL_SECONDS", "120")),
        info_http_timeout_seconds=float(os.getenv("INFO_HTTP_TIMEOUT_SECONDS", "20")),
        info_http_concurrency=int(os.getenv("INFO_HTTP_CONCURRENCY", "4")),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    settings.user_session.parent.mkdir(parents=True, exist_ok=True)
    return settings
