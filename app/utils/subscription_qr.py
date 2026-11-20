"""Generate subscription-link QR images on demand (not stored)."""
from __future__ import annotations

from io import BytesIO


def subscription_qr_png(url: str) -> bytes:
    """Encode the subscription URL as a PNG QR code."""
    import qrcode

    qr = qrcode.QRCode(version=None, box_size=8, border=2)
    qr.add_data(url.strip())
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
