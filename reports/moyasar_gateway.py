from __future__ import annotations

import base64
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class MoyasarGatewayError(Exception):
    pass


def is_enabled() -> bool:
    return bool(getattr(settings, "MOYASAR_ENABLED", False))


def _secret_key() -> str:
    key = str(getattr(settings, "MOYASAR_SECRET_KEY", "") or "").strip()
    if not key:
        raise ImproperlyConfigured(
            "MOYASAR_SECRET_KEY is required when Moyasar payments are enabled."
        )
    return key


def _amount_in_halalas(value: Decimal | int | str) -> int:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    halalas = int(amount * 100)
    if halalas < 100:
        raise MoyasarGatewayError("Moyasar invoice amount must be at least 1 SAR.")
    return halalas


def _request(
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    method: str = "GET",
) -> dict[str, Any]:
    base_url = str(getattr(settings, "MOYASAR_API_BASE_URL", "") or "").rstrip("/")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    credentials = base64.b64encode(f"{_secret_key()}:".encode("utf-8")).decode("ascii")
    request = Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
            "User-Agent": "Tawtheeq-Moyasar/1.0",
        },
    )
    timeout = int(getattr(settings, "MOYASAR_REQUEST_TIMEOUT", 15) or 15)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise MoyasarGatewayError(
            f"Moyasar API returned HTTP {exc.code}: {detail}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise MoyasarGatewayError("Could not connect to Moyasar API.") from exc

    try:
        result = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MoyasarGatewayError("Moyasar API returned an invalid JSON response.") from exc
    if not isinstance(result, dict):
        raise MoyasarGatewayError("Moyasar API returned an unexpected response.")
    return result


def create_invoice(
    *,
    amount: Decimal,
    description: str,
    callback_url: str,
    success_url: str,
    back_url: str,
    metadata: dict[str, str],
    expired_at: datetime | str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "amount": _amount_in_halalas(amount),
        "currency": "SAR",
        "description": str(description)[:255],
        "callback_url": callback_url,
        "success_url": success_url,
        "back_url": back_url,
        "metadata": {str(key)[:40]: str(value)[:500] for key, value in metadata.items()},
    }
    if expired_at:
        payload["expired_at"] = expired_at.isoformat() if isinstance(expired_at, datetime) else str(expired_at)
    response = _request(
        "/invoices",
        method="POST",
        payload=payload,
    )
    if not response.get("id") or not response.get("url"):
        raise MoyasarGatewayError("Moyasar invoice response is missing invoice details.")
    return response


def fetch_invoice(invoice_id: str) -> dict[str, Any]:
    if not invoice_id:
        raise MoyasarGatewayError("Moyasar invoice id is required.")
    return _request(f"/invoices/{invoice_id}")


def cancel_invoice(invoice_id: str) -> dict[str, Any]:
    if not invoice_id:
        raise MoyasarGatewayError("Moyasar invoice id is required.")
    return _request(f"/invoices/{invoice_id}/cancel", method="PUT")
