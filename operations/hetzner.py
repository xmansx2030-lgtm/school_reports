"""Small, fixed-scope Hetzner Cloud API adapter for the operations centre."""

from __future__ import annotations

from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone


class HetznerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class HetznerClient:
    BASE_URL = "https://api.hetzner.cloud/v1"
    TIMEOUT = (4, 12)

    def __init__(self, *, write: bool = False):
        name = "OPERATIONS_HETZNER_WRITE_TOKEN" if write else "OPERATIONS_HETZNER_READ_TOKEN"
        self.token = str(getattr(settings, name, "") or "").strip()
        if not self.token:
            raise HetznerError("not_configured", "اتصال Hetzner غير مهيأ لهذا الإجراء.")
        self.write = write

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = requests.request(
                method,
                self.BASE_URL + path,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                timeout=self.TIMEOUT,
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise HetznerError("unavailable", "تعذر الاتصال بخدمة Hetzner الآن.") from exc
        if not 200 <= response.status_code < 300:
            # Provider responses can contain account data; never pass them through or log them.
            if response.status_code in {401, 403}:
                raise HetznerError("unauthorized", "مفتاح Hetzner غير صالح أو صلاحياته غير كافية.")
            if response.status_code == 429:
                raise HetznerError("rate_limited", "بلغ اتصال Hetzner حد الطلبات المؤقت.")
            raise HetznerError("provider_error", f"تعذر إكمال الطلب لدى Hetzner ({response.status_code}).")
        try:
            return response.json()
        except ValueError as exc:
            raise HetznerError("invalid_response", "استجابة Hetzner غير صالحة.") from exc

    def overview(self, server_id: str) -> dict:
        server = self.server_state(server_id)
        now = timezone.now()
        partial_errors = []
        try:
            metrics = self._request(
                "GET",
                f"/servers/{server_id}/metrics",
                params={
                    "type": "cpu,disk,network",
                    "start": (now - timedelta(hours=3)).isoformat(),
                    "end": now.isoformat(),
                    "step": 300,
                },
            ).get("metrics", {})
        except HetznerError:
            metrics = {}
            partial_errors.append("metrics")
        try:
            backups = self._request(
                "GET", "/images", params={"bound_to": server_id, "type": "backup", "per_page": 10}
            ).get("images", [])
        except HetznerError:
            backups = []
            partial_errors.append("backups")
        try:
            recent_actions = self._request(
                "GET", f"/servers/{server_id}/actions", params={"per_page": 10, "sort": "id:desc"}
            ).get("actions", [])
        except HetznerError:
            recent_actions = []
            partial_errors.append("actions")
        return {
            "source": "hetzner",
            "fetched_at": now.isoformat(),
            "id": server["id"],
            "name": server.get("name"),
            "status": server.get("status"),
            "server_type": (server.get("server_type") or {}).get("name"),
            "location": (server.get("location") or {}).get("name"),
            "backup_window": server.get("backup_window"),
            "protection": server.get("protection") or {},
            "public_ipv4": ((server.get("public_net") or {}).get("ipv4") or {}).get("ip"),
            "metrics": metrics,
            "backups": [
                {
                    "id": row.get("id"),
                    "description": row.get("description"),
                    "created": row.get("created"),
                    "status": row.get("status"),
                    "image_size": row.get("image_size"),
                }
                for row in backups
                if row.get("type") == "backup"
            ],
            "recent_actions": [
                {
                    "id": row.get("id"),
                    "command": row.get("command"),
                    "status": row.get("status"),
                    "started": row.get("started"),
                    "finished": row.get("finished"),
                }
                for row in recent_actions
            ],
            "partial_errors": partial_errors,
        }

    def server_state(self, server_id: str) -> dict:
        server = self._request("GET", f"/servers/{server_id}").get("server")
        if not isinstance(server, dict) or str(server.get("id")) != str(server_id):
            raise HetznerError("invalid_response", "تعذرت مطابقة هوية الخادم لدى Hetzner.")
        return server

    def action(self, server_id: str, action: str) -> dict:
        if not self.write:
            raise HetznerError("read_only", "الاتصال الحالي للقراءة فقط.")
        if action not in {"poweron", "reboot", "shutdown", "snapshot"}:
            raise HetznerError("invalid_action", "إجراء الخادم غير مسموح.")
        if action == "snapshot":
            payload = self._request(
                "POST",
                f"/servers/{server_id}/actions/create_image",
                json={"type": "snapshot", "description": f"operations-{timezone.now():%Y%m%dT%H%M%SZ}"},
            )
        else:
            payload = self._request("POST", f"/servers/{server_id}/actions/{action}")
        result = payload.get("action")
        if not isinstance(result, dict) or not result.get("id"):
            raise HetznerError("invalid_response", "لم تؤكد Hetzner رقم الإجراء.")
        return result

    def action_status(self, action_id: int) -> dict:
        return self._request("GET", f"/actions/{action_id}").get("action") or {}
