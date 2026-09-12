from __future__ import annotations

import logging
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import FileSystemStorage
from io import BytesIO
from typing import BinaryIO

from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # Pillow غير متوفر؟ نتخطى الضغط بهدوء
    Image = None
    UnidentifiedImageError = OSError

# نحاول استيراد django-storages (S3) لاستخدام Cloudflare R2 عند تفعيله
try:
    from storages.backends.s3boto3 import S3Boto3Storage
except ImportError:
    S3Boto3Storage = None


def _rewind(file_obj: BinaryIO) -> None:
    try:
        file_obj.seek(0)
    except (AttributeError, OSError, ValueError):
        logger.debug("Could not rewind media file before storage processing", exc_info=True)


def _compress_image_file(file_obj: BinaryIO, max_size: int = 1600, jpeg_quality: int = 90):
    """يضغط الصورة (إن كانت صورة) قبل حفظها.

    - يقلّل الأبعاد بحيث لا يتجاوز أي بُعد max_size (مثلاً 1600px)
    - يحفظ JPEG بجودة عالية (افتراضيًا 90) مع optimize=True
    - لا يلمس الملفات غير الصورية أو GIF/WEBP (لتجنّب كسر الصور المتحركة)
    """
    if Image is None:
        # في حال لم يكن Pillow مثبتًا لأي سبب
        return file_obj

    # نضمن أن مؤشر الملف في البداية
    _rewind(file_obj)

    try:
        img = Image.open(file_obj)
    except (UnidentifiedImageError, OSError, ValueError):
        # ليس ملف صورة معروف → نعيده كما هو
        _rewind(file_obj)
        return file_obj

    img_format = (img.format or "JPEG").upper()

    # نتجنّب العبث بـ GIF/WEBP لاحتمال أن تكون متحركة
    if img_format in {"GIF", "WEBP"}:
        _rewind(file_obj)
        return file_obj

    # تصغير الأبعاد إذا كانت كبيرة جدًا
    try:
        width, height = img.size
        if max(width, height) > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)
    except (AttributeError, OSError, ValueError):
        # في حال فشل أي شيء، نُكمل بدون تغيير الأبعاد
        logger.debug("Could not inspect or resize uploaded image", exc_info=True)

    # معالجة الأنماط اللونية قبل الحفظ كـ JPEG
    if img_format in {"JPEG", "JPG"} and img.mode in {"RGBA", "P"}:
        img = img.convert("RGB")

    buffer = BytesIO()

    # إعدادات الحفظ حسب النوع
    save_kwargs = {}
    if img_format in {"JPEG", "JPG"}:
        save_kwargs["quality"] = jpeg_quality
        save_kwargs["optimize"] = True
        save_format = "JPEG"
    elif img_format == "PNG":
        save_kwargs["optimize"] = True
        save_format = "PNG"
    else:
        save_format = img_format

    try:
        img.save(buffer, format=save_format, **save_kwargs)
    except OSError:
        # في بعض الحالات لا يدعم optimizer → نحفظ بدون خيارات خاصة
        buffer = BytesIO()
        img.save(buffer, format=save_format)

    buffer.seek(0)
    compressed = ContentFile(buffer.read())
    # الاحتفاظ بنفس الاسم ليسهّل التوافق
    compressed.name = getattr(file_obj, "name", None) or "image"
    return compressed


def _use_r2_storage() -> bool:
    """Detect if S3-compatible storage is configured in settings (Cloudflare R2)."""
    return bool(
        S3Boto3Storage is not None
        and getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
        and getattr(settings, "AWS_S3_ENDPOINT_URL", None)
    )


if _use_r2_storage():
    class PublicRawMediaStorage(S3Boto3Storage):
        """Compatibility storage name for R2-backed sensitive media.

        Access is private and signed by default through AWS_QUERYSTRING_AUTH.
        The historical class name is retained because migrations import it.
        """

        def _save(self, name, content):  # type: ignore[override]
            content = _compress_image_file(content)
            return super()._save(name, content)
else:
    class PublicRawMediaStorage(FileSystemStorage):
        """Compatibility storage name for local development media."""

        def _save(self, name, content):  # type: ignore[override]
            content = _compress_image_file(content)
            return super()._save(name, content)


# ----------------- Cloudflare R2 Storage -----------------
if S3Boto3Storage is None:
    class R2MediaStorage(FileSystemStorage):
        """Placeholder when django-storages isn't installed."""

        def __init__(self, *args, **kwargs):
            raise ImproperlyConfigured(
                "R2 storage requires 'django-storages' and 'boto3'. "
                "Install them and ensure 'storages' is available."
            )
else:
    class R2MediaStorage(S3Boto3Storage):
        """Storage backend for Cloudflare R2 (S3-compatible).

        Compresses image files before upload to reduce size.
        """

        def _save(self, name, content):  # type: ignore[override]
            content = _compress_image_file(content)
            return super()._save(name, content)


# When R2 is enabled, reuse the same behavior for attachment storage.
if _use_r2_storage() and S3Boto3Storage is not None:
    class PublicRawMediaStorage(R2MediaStorage):
        """R2 media storage; private signed URLs are the production default."""

