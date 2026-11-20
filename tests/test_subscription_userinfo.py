"""Unit tests for subscription-userinfo normalization (no network)."""
from datetime import date

from app.services.subscription_userinfo import UserinfoResult, redact_subscription_url


def test_redact_url() -> None:
    s = redact_subscription_url(
        "https://example.test:8445/sub/SECRETTOKENVALUE"
    )
    assert "SECRET" not in s
    assert s.endswith("/sub/…")
    assert "example.test:8445" in s


def test_userinfo_fields() -> None:
    # 25 GiB total, none used, expire far future
    ui = UserinfoResult(
        ok=True,
        upload_bytes=0,
        download_bytes=0,
        total_bytes=26843545600,
        expire_unix=1893456000,  # 2030-01-01-ish
    )
    fields = ui.as_subscription_fields()
    assert fields["traffic_limit_mb"] == 25600.0
    assert fields["used_traffic_mb"] == 0.0
    assert fields["remaining_traffic_mb"] == 25600.0
    assert isinstance(fields["expiration_date"], date)
