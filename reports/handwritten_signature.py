"""Validation and normalization for a recipient's drawn acknowledgement."""

from __future__ import annotations

import base64
import binascii
import hashlib
from io import BytesIO

from PIL import Image, UnidentifiedImageError


SIGNATURE_SIZE = (900, 300)
MAX_PNG_BYTES = 256 * 1024
DATA_PREFIX = "data:image/png;base64,"


class InvalidHandwrittenSignature(ValueError):
    pass


def normalize_signature(data_url: str) -> bytes:
    """Accept a transparent drawing, reject empty/oversized data and strip metadata."""
    if not data_url or not data_url.startswith(DATA_PREFIX):
        raise InvalidHandwrittenSignature("ارسم توقيعك في المساحة المخصصة قبل الاعتماد.")
    encoded = data_url[len(DATA_PREFIX):]
    if len(encoded) > (MAX_PNG_BYTES * 4 // 3 + 8):
        raise InvalidHandwrittenSignature("حجم التوقيع أكبر من المسموح. امسحه وأعد رسمه.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_PNG_BYTES:
            raise InvalidHandwrittenSignature("حجم التوقيع أكبر من المسموح. امسحه وأعد رسمه.")
        with Image.open(BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != SIGNATURE_SIZE:
                raise InvalidHandwrittenSignature("صيغة التوقيع غير صالحة. امسحه وأعد رسمه.")
            image.load()
            alpha = image.convert("RGBA").getchannel("A")
    except (binascii.Error, OSError, UnidentifiedImageError, ValueError) as exc:
        if isinstance(exc, InvalidHandwrittenSignature):
            raise
        raise InvalidHandwrittenSignature("تعذّر قراءة التوقيع. امسحه وأعد رسمه.") from exc

    ink = alpha.point(lambda value: 255 if value >= 32 else 0)
    bounds = ink.getbbox()
    if not bounds:
        raise InvalidHandwrittenSignature("ارسم توقيعك في المساحة المخصصة قبل الاعتماد.")
    left, top, right, bottom = bounds
    pixel_count = ink.histogram()[255]
    if right - left < 35 or bottom - top < 8 or pixel_count < 140 or pixel_count > 900 * 300 // 3:
        raise InvalidHandwrittenSignature("التوقيع غير واضح. امسحه وأعد رسمه بخط واضح.")

    # Keep the ink's antialiasing while making the stored image deterministic
    # and removing any uploaded metadata or unexpected color channels.
    normalized = Image.new("RGBA", SIGNATURE_SIZE, (7, 67, 44, 0))
    normalized.putalpha(alpha)
    output = BytesIO()
    normalized.save(output, format="PNG", optimize=True)
    result = output.getvalue()
    if len(result) > MAX_PNG_BYTES:
        raise InvalidHandwrittenSignature("حجم التوقيع أكبر من المسموح. امسحه وأعد رسمه.")
    return result


def stored_signature_digest(field) -> str:
    with field.storage.open(field.name, "rb") as source:
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()
