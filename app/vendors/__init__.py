"""Provider adapters.

Phase 9 (light multi-provider readiness):
- Boundary: ``VendorBotDriver`` + ``VendorCapabilities`` in ``base``;
  live instances in ``registry``.
- Concrete adapters: NetPlus + FineArts.
- Usable usernames are **global** across vendors (shop inventory rule).
"""
from app.vendors.base import VendorBotDriver, VendorCapabilities
from app.vendors.registry import (
    DEFAULT_VENDOR_KEY,
    get_vendor_capabilities,
    get_vendor_driver,
    register_vendor_driver,
    vendor_supports_url_userinfo,
)

__all__ = [
    "DEFAULT_VENDOR_KEY",
    "VendorBotDriver",
    "VendorCapabilities",
    "get_vendor_capabilities",
    "get_vendor_driver",
    "register_vendor_driver",
    "vendor_supports_url_userinfo",
]
