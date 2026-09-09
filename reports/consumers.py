from __future__ import annotations

from typing import Any, Dict, Optional
import asyncio
import contextlib

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.core.cache import cache
from django.db import close_old_connections
from django.db.models import Count, Q
from django.utils import timezone

import logging
import time

from .models import NotificationRecipient
from core import opmetrics
from core.observability import report_degraded as _degraded, soft_call, soft_fail


logger = logging.getLogger(__name__)

# ── Per-user connection limits ──────────────────────────────────────
# At 50K+ schools × 25 teachers, uncontrolled reconnect storms will
# overwhelm Redis pub/sub and the DB.  These limits keep a single user
# from consuming unbounded resources.
MAX_WS_CONNECTIONS_PER_USER = 3       # max concurrent sockets per user
WS_CONNECT_RATE_WINDOW_SECONDS = 60   # sliding window for rate-limit
WS_CONNECT_RATE_MAX = 10              # max new connections per window
COUNTS_CACHE_TTL_SECONDS = 10         # cache _compute_counts per user
WS_HEARTBEAT_TIMEOUT_SECONDS = 75
WS_IDLE_SWEEP_SECONDS = 15


def _gauge_key(name: str) -> str:
    return f"ws:gauge:{name}"


def _safe_cache_delta(key: str, delta: int, *, floor: int = 0) -> int:
    try:
        cache.add(key, 0, timeout=3600)
        if delta >= 0:
            return int(cache.incr(key, delta))
        current = int(cache.get(key) or 0)
        updated = max(floor, current + delta)
        cache.set(key, updated, timeout=3600)
        return updated
    except Exception:
        return 0


class NotificationCountsConsumer(AsyncJsonWebsocketConsumer):
    """Push unread/signature counters via WebSocket.

    Contract:
    - On connect: send a full `counts` payload.
    - On server-side events: send `delta` payloads (no polling).
    - Client may send {"type": "resync"} to force recompute (non-periodic).
    """

    user_id: int
    active_school_id: Optional[int]
    trace_id: str
    _last_resync_ts: float
    group_name: str
    _idle_watchdog_task: Optional[asyncio.Task]
    _disconnect_log_reason: Optional[str]

    async def connect(self):
        await database_sync_to_async(close_old_connections)()
        user = self.scope.get("user")
        self.trace_id = self._scope_header("x-request-id") or "-"
        self._last_resync_ts = 0.0
        self.group_name = ""  # ensure always set before disconnect
        self._idle_watchdog_task = None
        self._disconnect_log_reason = None
        self.connected_at = time.monotonic()
        self.last_client_activity_ts = self.connected_at
        path = self.scope.get("path")
        if not user or not getattr(user, "is_authenticated", False):
            opmetrics.increment("ws.notifications.denied.unauthenticated")
            logger.debug("WS notifications reject unauthenticated trace_id=%s path=%s", self.trace_id, path)
            self._remember_close_reason("unauthenticated")
            await self.close(code=4401)
            return

        self.user_id = int(getattr(user, "id", 0) or 0)

        # ── Rate-limit new connections per user ─────────────────────
        rate_key = f"ws:conn_rate:{self.user_id}"
        try:
            if not cache.add(rate_key, 0, timeout=WS_CONNECT_RATE_WINDOW_SECONDS):
                conn_count = cache.incr(rate_key)
            else:
                conn_count = 1
            if conn_count > WS_CONNECT_RATE_MAX:
                opmetrics.increment("ws.notifications.denied.rate_limited")
                logger.warning(
                    "WS notifications rate-limited trace_id=%s user_id=%s connects_in_window=%s",
                    self.trace_id, self.user_id, conn_count,
                )
                self._remember_close_reason("rate_limited")
                await self.close(code=4429)
                return
        except Exception:
            pass  # cache down — allow the connection

        # ── Cap concurrent connections per user ─────────────────────
        cap_key = f"ws:conn_cap:{self.user_id}"
        try:
            cache.add(cap_key, 0, timeout=3600)
            active = cache.incr(cap_key)
            if active > MAX_WS_CONNECTIONS_PER_USER:
                cache.decr(cap_key)
                opmetrics.increment("ws.notifications.denied.max_connections")
                logger.info(
                    "WS notifications max-connections trace_id=%s user_id=%s active=%s",
                    self.trace_id, self.user_id, active,
                )
                self._remember_close_reason("max_connections")
                await self.close(code=4429)
                return
        except Exception:
            pass  # cache down — allow the connection

        sid = None
        try:
            sess = self.scope.get("session")
            if sess is not None:
                sid = sess.get("active_school_id")
        except Exception:
            sid = None
        try:
            self.active_school_id = int(sid) if sid else None
        except Exception:
            self.active_school_id = None
        self.group_name = f"notif.u{self.user_id}"
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        except Exception as exc:
            logger.exception(
                "WS notifications group_add failed (user_id=%s group=%s path=%s): %s",
                self.user_id,
                self.group_name,
                self.scope.get("path"),
                exc,
            )
            # Accept first so we can send a proper close frame, otherwise the
            # browser receives a raw TCP drop and logs 1006 instead of 1011.
            with soft_fail("ws.accept_before_close"):
                await self.accept()
            self._remember_close_reason("group_add_failed")
            await self.close(code=1011)
            return
        try:
            await self.accept()
        except Exception as exc:
            logger.exception(
                "WS notifications accept failed (user_id=%s path=%s): %s",
                self.user_id,
                self.scope.get("path"),
                exc,
            )
            return
        opmetrics.increment("ws.notifications.connect.accepted")
        _safe_cache_delta(_gauge_key("active"), 1)
        _safe_cache_delta(_gauge_key(f"user:{self.user_id}"), 1)
        self._idle_watchdog_task = asyncio.create_task(self._idle_watchdog())
        payload = await self._compute_counts_cached()
        await self.send_json({"type": "counts", **payload})
        opmetrics.increment("ws.notifications.messages.sent")

    async def disconnect(self, code):
        await database_sync_to_async(close_old_connections)()
        watchdog = getattr(self, "_idle_watchdog_task", None)
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog
        group = getattr(self, "group_name", "")
        if group:
            # مجموعةٌ لا يُنسحب منها تُبقي قناةً ميتة تتلقّى البثّ إلى الأبد.
            with soft_fail("ws.group_discard", group=group):
                await self.channel_layer.group_discard(group, self.channel_name)
        # Release connection-cap slot
        # عدّادٌ لا يُنقَص عند الفصل يستنفد سقفَ اتصالات المستخدم تدريجياً
        # حتى يعجز عن الاتصال أصلاً.
        with soft_fail("ws.release_connection_slot", user_id=getattr(self, "user_id", None)):
            uid = getattr(self, "user_id", None)
            if uid is not None:
                cap_key = f"ws:conn_cap:{uid}"
                cache.decr(cap_key)
        uid = getattr(self, "user_id", None)
        if uid is not None:
            _safe_cache_delta(_gauge_key("active"), -1)
            _safe_cache_delta(_gauge_key(f"user:{uid}"), -1)
        try:
            user = getattr(self, "user_id", None)
            duration_ms = round((time.monotonic() - float(getattr(self, "connected_at", time.monotonic()))) * 1000, 1)
            # code=None means the TCP connection dropped with no WebSocket close
            # frame (e.g. page navigation, OS killing idle tab) — treat it as 1006.
            norm_code = (
                int(code)
                if isinstance(code, int)
                else (int(code) if isinstance(code, str) and code.isdigit() else 1006)
            )
            reason = self._disconnect_reason(norm_code)
            if norm_code == 1001:
                opmetrics.increment("ws.notifications.close.navigation_1001")
                return
            if norm_code == 1000:
                return
            elif norm_code == 1006:
                opmetrics.increment("ws.notifications.close.abnormal_1006")
                bucket = timezone.now().strftime("%Y%m%d%H")
                key = f"ws_disconnect_1006:{bucket}"
                try:
                    cache.add(key, 0, timeout=7200)
                    count = cache.incr(key)
                except Exception:
                    count = None
                # Log only 1st, 10th, 100th per hour to reduce noise
                if count in {1, 10, 100} or count is None:
                    is_repeated = count not in {None, 1}
                    log_fn = logger.warning if is_repeated else logger.debug
                    log_fn(
                        "WS notifications abnormal_close user_id=%s code=%s normalized_code=%s reason=%s hour_count=%s duration_ms=%s",
                        user,
                        code,
                        norm_code,
                        reason,
                        count,
                        duration_ms,
                    )
            elif norm_code == 4401:
                opmetrics.increment("ws.notifications.close.unauthorized_4401")
                logger.debug("WS notifications denied user_id=%s code=%s reason=%s duration_ms=%s", user, norm_code, reason, duration_ms)
            else:
                opmetrics.increment("ws.notifications.close.other")
                log_fn = logger.warning if self._is_clearly_abnormal_close(norm_code) else logger.debug
                log_fn(
                    "WS notifications close user_id=%s code=%s normalized_code=%s reason=%s duration_ms=%s",
                    user,
                    code,
                    norm_code,
                    reason,
                    duration_ms,
                )
            opmetrics.timing("ws.notifications.connection.duration", duration_ms)
        except Exception:
            _degraded("ws.record_disconnect_metrics")

    async def receive_json(self, content: Dict[str, Any], **kwargs):
        await database_sync_to_async(close_old_connections)()
        self.last_client_activity_ts = time.monotonic()
        opmetrics.increment("ws.notifications.messages.received")
        msg_type = (content or {}).get("type")
        if msg_type in {"keepalive", "ping"}:
            with soft_fail("ws.send_pong", user_id=getattr(self, "user_id", None)):
                await self.send_json({"type": "pong"})
                opmetrics.increment("ws.notifications.messages.sent")
            return
        if msg_type == "resync":
            # Throttle resyncs: max once per 5 seconds to avoid DB hammering
            now = time.monotonic()
            if now - self._last_resync_ts < 5.0:
                return
            self._last_resync_ts = now
            payload = await self._compute_counts_cached()
            await self.send_json({"type": "counts", **payload})
            opmetrics.increment("ws.notifications.messages.sent")
            return

        if msg_type == "set_active_school":
            previous_school_id = getattr(self, "active_school_id", None)
            next_school_id = await self._resolve_allowed_school_id(content.get("active_school_id"))
            self.active_school_id = next_school_id
            if previous_school_id != next_school_id:
                logger.debug("WS notifications active school updated user_id=%s school_id=%s", getattr(self, "user_id", None), next_school_id)
            payload = await self._compute_counts_cached()
            await self.send_json({"type": "counts", **payload})
            opmetrics.increment("ws.notifications.messages.sent")
            return

    async def notif_delta(self, event: Dict[str, Any]):
        """Group event handler."""
        out = {
            "type": "delta",
            "delta_unread": int(event.get("delta_unread") or 0),
            "delta_signatures_pending": int(event.get("delta_signatures_pending") or 0),
            "delta_count": int(event.get("delta_count") or 0),
            "notification_school_id": event.get("notification_school_id"),
            "force_resync": bool(event.get("force_resync") or False),
        }
        await self.send_json(out)
        opmetrics.increment("ws.notifications.messages.sent")

    async def _compute_counts_cached(self) -> Dict[str, int]:
        """Return cached notification counts to avoid hammering the DB on
        reconnect storms.  At 50K+ schools the same user may reconnect
        dozens of times per minute (mobile browser 1006)."""
        uid = getattr(self, "user_id", 0)
        sid = getattr(self, "active_school_id", None) or 0
        ck = f"ws:counts:{uid}:{sid}"
        cached = soft_call("ws.counts_cache_get", lambda: cache.get(ck), default=None)
        if cached is not None:
            return cached
        result = await self._compute_counts()
        soft_call(
            "ws.counts_cache_set",
            lambda: cache.set(ck, result, COUNTS_CACHE_TTL_SECONDS),
            default=None,
        )
        return result

    @database_sync_to_async
    def _compute_counts(self) -> Dict[str, int]:
        close_old_connections()
        now = timezone.now()

        qs = NotificationRecipient.objects.filter(teacher_id=self.user_id)

        # Active-school isolation (include global notifications school=NULL)
        if self.active_school_id is not None:
            qs = qs.filter(Q(notification__school_id=self.active_school_id) | Q(notification__school__isnull=True))

        # Exclude expired
        qs = qs.filter(Q(notification__expires_at__gt=now) | Q(notification__expires_at__isnull=True))

        unread_q = Q(notification__kind__in=["notification", "newsletter"]) & (
            Q(is_read=False)
            | Q(
                notification__kind="newsletter",
                notification__requires_signature=True,
                is_signed=False,
            )
        )
        pending_sig_q = Q(
            notification__kind="circular",
            notification__requires_signature=True,
            is_signed=False,
        )
        attention_q = unread_q | pending_sig_q

        agg = qs.aggregate(
            count=Count("id", filter=attention_q),
            unread=Count("id", filter=unread_q),
            signatures_pending=Count("id", filter=pending_sig_q),
        )

        return {
            "count": int(agg.get("count") or 0),
            "unread": int(agg.get("unread") or 0),
            "signatures_pending": int(agg.get("signatures_pending") or 0),
        }

    async def _idle_watchdog(self) -> None:
        while True:
            await asyncio.sleep(WS_IDLE_SWEEP_SECONDS)
            last_seen = float(getattr(self, "last_client_activity_ts", 0.0) or 0.0)
            if not last_seen:
                last_seen = time.monotonic()
            if time.monotonic() - last_seen <= WS_HEARTBEAT_TIMEOUT_SECONDS:
                continue
            opmetrics.increment("ws.notifications.close.idle_timeout")
            logger.info(
                "WS notifications idle timeout trace_id=%s user_id=%s timeout_seconds=%s",
                getattr(self, "trace_id", "-"),
                getattr(self, "user_id", None),
                WS_HEARTBEAT_TIMEOUT_SECONDS,
            )
            with contextlib.suppress(Exception):
                self._remember_close_reason("idle_timeout")
                await self.close(code=4408)
            return

    def _remember_close_reason(self, reason: Optional[str]) -> None:
        self._disconnect_log_reason = reason or None

    def _disconnect_reason(self, norm_code: int) -> str:
        explicit_reason = getattr(self, "_disconnect_log_reason", None)
        if explicit_reason:
            return explicit_reason
        return {
            1000: "normal_closure",
            1006: "connection_dropped",
            1011: "server_error",
            4401: "unauthenticated",
            4408: "idle_timeout",
            4429: "policy_limit",
        }.get(norm_code, "unknown")

    def _is_clearly_abnormal_close(self, norm_code: int) -> bool:
        return norm_code in {
            1002,
            1003,
            1005,
            1006,
            1007,
            1008,
            1009,
            1010,
            1011,
            1012,
            1013,
            1014,
            1015,
        }

    def _scope_header(self, name: str) -> str:
        needle = (name or "").strip().lower().encode()
        for item in (self.scope.get("headers") or []):
            try:
                k, v = item
            except (TypeError, ValueError):
                # ترويسةٌ مشوّهة في النطاق: تُتخطّى، ولا تُخفي غيرها.
                continue
            if k == needle:
                return v.decode("utf-8", errors="ignore")
        return "-"

    def _scope_session_key(self) -> str:
        try:
            sess = self.scope.get("session")
            if sess is None:
                return "none"
            return getattr(sess, "session_key", None) or "none"
        except Exception:
            return "none"

    @database_sync_to_async
    def _resolve_allowed_school_id(self, raw_school_id: Any) -> Optional[int]:
        close_old_connections()
        try:
            requested_school_id = int(raw_school_id) if raw_school_id else None
        except Exception:
            requested_school_id = None
        if requested_school_id is None:
            return None
        try:
            session = self.scope.get("session")
            session_school_id = int(session.get("active_school_id")) if session and session.get("active_school_id") else None
        except Exception:
            session_school_id = None
        if session_school_id is not None:
            return requested_school_id if requested_school_id == session_school_id else session_school_id
        return None
