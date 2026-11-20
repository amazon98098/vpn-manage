"""In-process registry of vendor bot drivers + capabilities."""
from __future__ import annotations

from typing import Any

from app.vendors.base import VendorBotDriver, VendorCapabilities

DEFAULT_VENDOR_KEY = "netplus"

_DRIVERS: dict[str, Any] = {}
_CAPS: dict[str, VendorCapabilities] = {}

# Built-in capability catalog (drivers register instances at process start).
KNOWN_CAPABILITIES: dict[str, VendorCapabilities] = {
    "netplus": VendorCapabilities(
        key="netplus",
        display_name="NetPlus",
        bot_create=True,
        bot_renew=True,
        bot_change_link=True,
        bot_inventory_list=True,
        url_userinfo=True,
    ),
    "finearts": VendorCapabilities(
        key="finearts",
        display_name="FineArts",
        bot_create=True,
        bot_renew=True,
        bot_change_link=True,
        bot_inventory_list=True,
        url_userinfo=True,
    ),
}


def register_vendor_driver(
    driver: VendorBotDriver | Any,
    capabilities: VendorCapabilities | None = None,
) -> str:
    """Register a live driver instance. Returns vendor key."""
    key = (
        (capabilities.key if capabilities else None)
        or getattr(driver, "vendor_key", None)
        or DEFAULT_VENDOR_KEY
    )
    key = str(key).strip().lower()
    caps = capabilities or KNOWN_CAPABILITIES.get(key)
    if caps is None:
        caps = VendorCapabilities(key=key, display_name=key)
    _DRIVERS[key] = driver
    _CAPS[key] = caps
    if not getattr(driver, "vendor_key", None):
        try:
            driver.vendor_key = key  # type: ignore[attr-defined]
        except Exception:
            pass
    return key


def get_vendor_driver(key: str | None) -> Any | None:
    if not key:
        return _DRIVERS.get(DEFAULT_VENDOR_KEY)
    return _DRIVERS.get(str(key).strip().lower())


def get_vendor_capabilities(key: str | None) -> VendorCapabilities | None:
    if not key:
        key = DEFAULT_VENDOR_KEY
    k = str(key).strip().lower()
    if k in _CAPS:
        return _CAPS[k]
    return KNOWN_CAPABILITIES.get(k)


def list_registered_vendor_keys() -> list[str]:
    return sorted(_DRIVERS.keys())


def clear_vendor_registry() -> None:
    """Test helper."""
    _DRIVERS.clear()
    _CAPS.clear()


def vendor_supports_url_userinfo(
    *,
    vendor_key: str | None = None,
    info_refresh_strategy: str | None = None,
) -> bool:
    """
    Decide whether HTTP subscription-userinfo is allowed.

    DB ``info_refresh_strategy`` wins when set:
      - provider_bot → no HTTP
      - subscription_url / automatic → HTTP if capabilities allow
    """
    strategy = (info_refresh_strategy or "automatic").strip().lower()
    if strategy == "provider_bot":
        return False
    caps = get_vendor_capabilities(vendor_key or DEFAULT_VENDOR_KEY)
    if caps is None:
        return strategy in ("automatic", "subscription_url")
    if strategy == "subscription_url":
        return bool(caps.url_userinfo)
    # automatic
    return bool(caps.url_userinfo)
