from __future__ import annotations

from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image


def valid_receipt(name: str = "receipt.png") -> SimpleUploadedFile:
    stream = BytesIO()
    Image.new("RGB", (2, 2), color=(18, 94, 62)).save(stream, format="PNG")
    return SimpleUploadedFile(name, stream.getvalue(), content_type="image/png")


def checkout_submission_key(client, **query) -> str:
    response = client.get(reverse("reports:my_subscription"), query)
    if response.status_code != 200:
        raise AssertionError(
            f"Could not issue checkout submission key: HTTP {response.status_code}"
        )
    return response.context["checkout_submission_key"]
