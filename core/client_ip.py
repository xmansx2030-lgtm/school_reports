"""Safe client IP resolution for deployments behind a trusted reverse proxy."""
from __future__ import annotations

from ipaddress import ip_address, ip_network

from django.conf import settings


def _valid_ip(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(ip_address(raw))
    except ValueError:
        return ""


def _is_trusted_proxy(value: str) -> bool:
    try:
        peer = ip_address(value)
    except ValueError:
        return False

    for raw_network in getattr(settings, "TRUSTED_PROXY_CIDRS", ()):
        try:
            if peer in ip_network(str(raw_network).strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request) -> str | None:
    """Return a validated client address for logs and persisted audit records.

    Proxy-supplied addresses are accepted only when the direct peer belongs to
    ``TRUSTED_PROXY_CIDRS``. In particular, an arbitrary
    ``X-Forwarded-For`` value is never treated as provenance.
    """
    remote_addr = _valid_ip(request.META.get("REMOTE_ADDR"))
    forwarded_addr = _valid_ip(request.META.get("HTTP_X_REAL_IP"))

    if remote_addr and forwarded_addr and _is_trusted_proxy(remote_addr):
        return forwarded_addr
    return remote_addr or None


def client_ip_for_ratelimit(request) -> str:
    """Return a stable rate-limit key when no valid address is available."""
    return client_ip(request) or "0.0.0.0"
