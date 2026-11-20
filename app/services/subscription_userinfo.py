"""HTTP subscription-userinfo fetch (Clash-compatible headers only).

Never log full subscription URLs, tokens, or configuration body contents.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)


@dataclass
class UserinfoResult:
    ok: bool
    error: str | None = None
    http_status: int | None = None
    upload_bytes: int | None = None
    download_bytes: int | None = None
    total_bytes: int | None = None
    expire_unix: int | None = None
    filename: str | None = None

    @property
    def used_bytes(self) -> int | None:
        if self.upload_bytes is None or self.download_bytes is None:
            return None
        return int(self.upload_bytes) + int(self.download_bytes)

    @property
    def remaining_bytes(self) -> int | None:
        if self.total_bytes is None or self.used_bytes is None:
            return None
        return int(self.total_bytes) - int(self.used_bytes)

    def as_subscription_fields(self) -> dict[str, Any]:
        """Normalized fields for Subscription columns (MB / date)."""
        out: dict[str, Any] = {}
        if self.total_bytes is not None:
            out["traffic_limit_mb"] = self.total_bytes / (1024 * 1024)
        if self.used_bytes is not None:
            out["used_traffic_mb"] = self.used_bytes / (1024 * 1024)
        if self.remaining_bytes is not None:
            out["remaining_traffic_mb"] = self.remaining_bytes / (1024 * 1024)
        if self.expire_unix is not None and self.expire_unix > 0:
            expire_dt = datetime.fromtimestamp(
                self.expire_unix, tz=timezone.utc
            ).replace(tzinfo=None)
            out["expiration_date"] = expire_dt
        return out


def redact_subscription_url(url: str) -> str:
    p = urlparse(url or "")
    host = p.netloc or "?"
    path = "/up/…" if "/up/" in (p.path or "") else "/sub/…"
    return f"{p.scheme}://{host}{path}"


def normalize_url_username(value: str | None) -> str:
    """Normalize content-disposition filename / service username for exact compare."""
    raw = (value or "").strip()
    if not raw:
        return ""
    # filename may include .txt / quotes noise
    name = raw.split("/")[-1].split("\\")[-1]
    if "." in name and name.rsplit(".", 1)[-1].lower() in {
        "txt",
        "conf",
        "yaml",
        "yml",
        "json",
    }:
        name = name.rsplit(".", 1)[0]
    return name.strip().lower()


def url_username_matches(expected_username: str, url_filename: str | None) -> bool:
    """True only when URL filename equals local username exactly (no substring)."""
    want = normalize_url_username(expected_username)
    got = normalize_url_username(url_filename)
    return bool(want) and bool(got) and want == got


def fetch_subscription_userinfo(
    url: str,
    *,
    timeout: float = 20.0,
    user_agent: str = "v2rayN/6.0",
) -> UserinfoResult:
    """GET subscription URL; parse subscription-userinfo; discard body."""
    if not (url or "").strip().startswith("http"):
        return UserinfoResult(ok=False, error="invalid_url")
    req = Request(
        url.strip(),
        headers={"User-Agent": user_agent, "Accept": "*/*"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            # Discard body after length check — never decode configs.
            length = int(headers.get("content-length") or 0)
            to_read = min(max(length, 1), 64)
            _ = resp.read(to_read)
    except HTTPError as e:
        log.info(
            "userinfo HTTPError status=%s url=%s",
            e.code,
            redact_subscription_url(url),
        )
        return UserinfoResult(ok=False, error=f"http_{e.code}", http_status=e.code)
    except URLError as e:
        log.info("userinfo URLError url=%s reason=%s", redact_subscription_url(url), type(e.reason).__name__)
        return UserinfoResult(ok=False, error="url_error")
    except Exception as e:
        log.info(
            "userinfo error url=%s exc=%s",
            redact_subscription_url(url),
            type(e).__name__,
        )
        return UserinfoResult(ok=False, error=type(e).__name__)

    raw = headers.get("subscription-userinfo")
    if not raw:
        return UserinfoResult(
            ok=False, error="missing_userinfo", http_status=int(status or 0)
        )

    parts: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        parts[k.strip()] = v.strip()

    required = ("upload", "download", "total", "expire")
    if not all(k in parts for k in required):
        return UserinfoResult(
            ok=False, error="incomplete_userinfo", http_status=int(status or 0)
        )
    try:
        upload = int(parts["upload"])
        download = int(parts["download"])
        total = int(parts["total"])
        expire = int(parts["expire"])
    except ValueError:
        return UserinfoResult(
            ok=False, error="invalid_userinfo_numbers", http_status=int(status or 0)
        )
    if total < 0 or upload < 0 or download < 0:
        return UserinfoResult(
            ok=False, error="negative_traffic", http_status=int(status or 0)
        )

    filename = None
    disp = headers.get("content-disposition") or ""
    m = re.search(r'filename="?([^";]+)"?', disp)
    if m:
        filename = m.group(1).strip()

    # HTTP 200 alone is not "healthy"; caller decides using expire/traffic/status UI.
    return UserinfoResult(
        ok=True,
        http_status=int(status or 0),
        upload_bytes=upload,
        download_bytes=download,
        total_bytes=total,
        expire_unix=expire,
        filename=filename,
    )
