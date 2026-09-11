# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Dict, Any, List, Iterable, Optional, Tuple, Set
from datetime import timedelta
import hashlib

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import FieldDoesNotExist
from django.http import HttpRequest
from django.utils import timezone
from django.apps import apps
from django.db.models import Count, Q
from django.urls import NoReverseMatch, reverse

# هذه الوحدة تعمل في **كل طلب**، فسقوطها يُسقط كل صفحة. ولذلك تختار في مواضع
# كثيرة أن تُكمل بقيمة بديلة بدل أن ترمي — وهو الاختيار الصحيح. أما ما كان
# خاطئاً فهو أن يمرّ ذلك بلا أثر: قسمٌ فارغ في الشريط لا يُميَّز عن قسمٍ تعثّر
# بناؤه، لا في السجل ولا في Sentry. ``soft_*`` تُبقي الإكمال وتُلغي الصمت.
from core.observability import report_degraded as _degraded, soft, soft_call, soft_fail

from .models import (
    Ticket,
    Department,
    Report,
    School,
    SchoolYearArchive,
    school_has_archive_addon,
)
from . import capabilities as caps
from .permissions import effective_user_role_label, get_school_manager_school_ids
from .gender_labels import school_gender_labels, school_gender_template_context

# حالات التذاكر
OPEN_STATES = {"open", "new"}
INPROGRESS_STATES = {"in_progress", "pending"}
UNRESOLVED_STATES = OPEN_STATES | INPROGRESS_STATES
CLOSED_STATES = {"done", "rejected", "cancelled"}


# -----------------------------
# أدوات مساعدة عامة
# -----------------------------
@soft("nav.count", default=0)
def _safe_count(qs) -> int:
    return qs.count()


@soft("nav.get_model", default=None)
def _get_model(app_label: str, model_name: str):
    return apps.get_model(app_label, model_name)


def _get_membership_model():
    return _get_model("reports", "DepartmentMembership")


@soft("nav.model_fields", default=frozenset())
def _model_fields(model) -> Set[str]:
    return {f.name for f in model._meta.get_fields()}


@soft("nav.cache_ttl", default=10)
def _nav_cache_ttl_seconds() -> int:
    """Short TTL to smooth load spikes without keeping stale nav data for long."""
    default_ttl = 20 if not bool(getattr(settings, "DEBUG", False)) else 5
    return max(0, int(getattr(settings, "NAV_CONTEXT_CACHE_TTL_SECONDS", default_ttl) or 0))


@soft("nav.active_school_id", default=0)
def _active_school_id_from_request(request: Optional[HttpRequest]) -> int:
    sid = request.session.get("active_school_id") if request is not None else None
    return int(sid) if sid else 0


# ── الكاش لا يُسقط الطلب، لكنه لا يُبتلع صامتاً ──────────────────────────────
# Redis يعمل بـ ``IGNORE_EXCEPTIONS: True`` فيُعيد ``None`` بدل أن يرمي، لكن
# ``cache.set`` قد يرمي على أخطاء أخرى (تسلسل، انقطاع). وتعثّر الكاش هنا يعني
# أن كل طلب يعيد بناء الشريط من القاعدة — وهو تدهور أداءٍ صامت يستحق أن يُقاس.
def _cache_get(key: str):
    return soft_call("nav.cache_get", lambda: cache.get(key), default=None, key=key)


def _cache_set(key: str, value, ttl: int) -> None:
    soft_call("nav.cache_set", lambda: cache.set(key, value, ttl), default=None, key=key)


# -----------------------------
# كشف أقسام المسؤول Officer
# -----------------------------
def _officer_role_values(membership_model) -> Iterable:
    values = set()
    if membership_model is None:
        return {"officer", 1, "1"}
    v = getattr(membership_model, "OFFICER", None)
    if v is not None:
        values.add(v)
    RoleType = getattr(membership_model, "RoleType", None)
    if RoleType is not None:
        v = getattr(RoleType, "OFFICER", None)
        if v is not None:
            values.add(v)
    # fallback
    values.update({"officer", 1, "1"})
    return values


def _teacher_role_values(membership_model) -> Iterable:
    values = set()
    if membership_model is None:
        return {"teacher", 0, "0"}
    v = getattr(membership_model, "TEACHER", None)
    if v is not None:
        values.add(v)
    RoleType = getattr(membership_model, "RoleType", None)
    if RoleType is not None:
        v = getattr(RoleType, "TEACHER", None)
        if v is not None:
            values.add(v)
    # fallback
    values.update({"teacher", 0, "0"})
    return values


def _user_department_codes(user, active_school: Optional[School] = None) -> List[str]:
    """رموزُ أقسام المستخدم — استعلامٌ واحد لكل طلب لا واحدٌ لكل نداء.

    ``_targeted_for_user_q`` تناديها، وتلك تُنادى مرتين في الطلب الواحد: مرة
    لمسار الإشعار البارز ومرة لعدّاد غير المقروء. فكان الجدول يُقرأ مرتين
    لنتيجةٍ واحدة لا تتغيّر داخل الطلب.

    والذاكرة على كائن المستخدم لا في الكاش: عمرها عمرُ الطلب بالضبط، فلا
    تحتاج إبطالاً ولا تُخطئ عبر الطلبات — وهو النمط نفسه الذي تستعمله
    ``permissions._school_membership_cache``.
    """
    cache_key = int(getattr(active_school, "pk", 0) or 0)
    memo = getattr(user, "_nav_department_codes_cache", None)
    if isinstance(memo, dict) and cache_key in memo:
        return memo[cache_key]

    Membership = _get_membership_model()
    if Membership is None:
        return []
    try:
        qs = Membership.objects.filter(teacher=user, department__is_active=True)
        with soft_fail(
            "nav.department_school_filter", school=getattr(active_school, "pk", None)
        ):
            if active_school is not None and "school" in _model_fields(Department):
                qs = qs.filter(department__school=active_school)
        codes = [c for c in qs.values_list("department__slug", flat=True) if c]
    except Exception:
        _degraded("nav.department_codes", user_id=getattr(user, "pk", None))
        return []

    if not isinstance(memo, dict):
        memo = {}
        with soft_fail("nav.department_codes_memo"):
            user._nav_department_codes_cache = memo
    memo[cache_key] = codes
    return codes


# -----------------------------
# نماذج الإشعارات (ديناميكيًا)
# -----------------------------
def _notification_models():
    """يُعيد موديل الإشعار + موديل سجل الاستلام/القراءة إن وُجد."""
    N = (
        _get_model("reports", "Notification")
        or _get_model("reports", "Announcement")
        or _get_model("reports", "AdminMessage")
    )
    # ✅ دعم اسم NotificationRecipient (المعمول به في مشروعك)
    R = (
        _get_model("reports", "NotificationRecipient")
        or _get_model("reports", "NotificationRead")
        or _get_model("reports", "NotificationReceipt")
        or _get_model("reports", "NotificationSeen")
    )
    return N, R


def _notification_sender_str(obj) -> str:
    f = _model_fields(obj.__class__)
    for cand in ("sender", "created_by", "author", "user", "teacher", "owner"):
        if cand in f:
            with soft_fail("nav.notification_sender", field=cand):
                v = getattr(obj, cand, None)
                if v:
                    return str(
                        getattr(v, "name", None)
                        or getattr(v, "phone", None)
                        or getattr(v, "username", None)
                        or v
                    )
    return "الإدارة"


_DISMISS_COOKIE_PREFIX = "notif_dismissed_"


def _has_dismissal_cookies(request: Optional[HttpRequest]) -> bool:
    """هل أخفى المستخدم أي إشعار أصلاً؟

    **السؤال يسبق الاستعلام.** كانت دالتا الاستبعاد أدناه تجلبان حتى ثمانين
    معرّفاً من القاعدة ثم تسألان الكوكيز عنها — أي استعلامان كاملان في **كل
    طلب**، بينما الغالبية العظمى من المستخدمين لم يُخفوا شيئاً قط فتعود
    القائمة فارغة دائماً.

    وقراءة الكوكيز مجانية: قاموسٌ في الذاكرة. فإن لم يكن فيه مفتاح إخفاء واحد،
    فلا شيء يمكن استبعاده — ولا حاجة لسؤال القاعدة.
    """
    if not request:
        return False
    cookies = getattr(request, "COOKIES", None) or {}
    return any(str(key).startswith(_DISMISS_COOKIE_PREFIX) for key in cookies)


def _exclude_notif_dismissed_cookies_notif_qs(qs, request: Optional[HttpRequest]):
    """استبعاد الإشعارات التي أخفاها المستخدم عبر الكوكي على مستوى Notification."""
    if not _has_dismissal_cookies(request):
        return qs

    def _apply():
        ids = list(qs.values_list("id", flat=True)[:80])
        skip = [i for i in ids if request.COOKIES.get(f"{_DISMISS_COOKIE_PREFIX}{i}")]
        return qs.exclude(id__in=skip) if skip else qs

    return soft_call("nav.dismissed_cookies_notif", _apply, default=qs)


def _exclude_notif_dismissed_cookies_recipient_qs(qs, request: Optional[HttpRequest], notif_fk: str):
    """استبعاد سجلات الاستلام التي أخفاها المستخدم عبر الكوكي (يفترض وجود FK اسمه notif_fk)."""
    if not _has_dismissal_cookies(request):
        return qs

    def _apply():
        ids = list(qs.values_list(f"{notif_fk}_id", flat=True)[:80])
        skip = [i for i in ids if request.COOKIES.get(f"{_DISMISS_COOKIE_PREFIX}{i}")]
        return qs.exclude(**{f"{notif_fk}_id__in": skip}) if skip else qs

    return soft_call("nav.dismissed_cookies_recipient", _apply, default=qs, fk=notif_fk)


def _published_notifications_qs(N):
    """فلترة نشر/نشاط/فترات زمنية على مستوى Notification."""
    qs = N.objects.all()
    now = timezone.now()
    f = _model_fields(N)
    # تعثّرٌ هنا يُعيد استعلاماً **أوسع** من المقصود — إشعارات غير منشورة أو
    # منتهية تظهر للمستخدم. فهو ليس تدهوراً تجميلياً بل تسرّب محتوى، ويجب أن
    # يُرى في السجل لا أن يُبتلع.
    with soft_fail("nav.published_notifications_filter"):
        if "is_active" in f:
            qs = qs.filter(is_active=True)
        if "status" in f and hasattr(N, "Status"):
            published_value = getattr(N.Status, "PUBLISHED", None)
            if published_value is not None:
                qs = qs.filter(status=published_value)
        # حقول أوقات شائعة
        if "starts_at" in f:
            qs = qs.filter(Q(starts_at__lte=now) | Q(starts_at__isnull=True))
        if "ends_at" in f:
            qs = qs.filter(Q(ends_at__gte=now) | Q(ends_at__isnull=True))
        if "publish_at" in f:
            qs = qs.filter(Q(publish_at__lte=now) | Q(publish_at__isnull=True))
        if "expires_at" in f:
            qs = qs.filter(Q(expires_at__gte=now) | Q(expires_at__isnull=True))
    return qs


def _user_lookup_for_relation(N, relation_name: str) -> Optional[str]:
    """المسار الذي يُطابَق به المستخدم عبر علاقة ``relation_name``.

    العلاقة قد تشير إلى المستخدم مباشرةً (``recipients=user``) أو إلى جدول وسيط
    يحمل مفتاح المستخدم (``recipients__teacher=user``). واختيار الصيغة الخطأ لا
    يُنتج نتيجةً ناقصة بل ``ValueError`` عند تنفيذ الاستعلام — وهو بالضبط ما كان
    يحدث هنا: ``Notification.recipients`` هو العكسيُّ لـ``NotificationRecipient``
    لا علاقةَ متعدّدٍ بالمعلّمين، فكان ``Q(recipients=user)`` يرمي في كل نداء،
    ويُبتلع صامتاً، فيموت مسار الاحتياط كلّه بلا أثر.

    فيُسأل الميتا عن النموذج المرتبط بدل التخمين من الاسم.
    """
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    try:
        field = N._meta.get_field(relation_name)
    except FieldDoesNotExist:
        return None

    related_model = getattr(field, "related_model", None)
    if related_model is None:
        return None
    if related_model is user_model:
        return relation_name

    # جدول وسيط: نبحث فيه عن المفتاح المشير إلى المستخدم.
    for candidate in ("teacher", "user", "recipient", "member"):
        try:
            through_field = related_model._meta.get_field(candidate)
        except FieldDoesNotExist:
            # حقلٌ غير موجود هو معنى وجود المرشّح التالي — لا خطأ يُسجَّل.
            continue
        if getattr(through_field, "related_model", None) is user_model:
            return f"{relation_name}__{candidate}"
    return None


def _targeted_for_user_q(N, user) -> Q:
    """
    استهداف المستخدم مباشرة من موديل Notification (Fallback فقط).
    مشروعك يعتمد NotificationRecipient لذا هذا المسار يُستخدم فقط إذا لم يتوفر R.
    """
    f = _model_fields(N)
    q = Q()
    if "teacher" in f:
        q |= Q(teacher=user)
    if "user" in f:
        q |= Q(user=user)
    for relation_name in ("recipients", "teachers", "users", "audience_teachers"):
        if relation_name in f:
            lookup = _user_lookup_for_relation(N, relation_name)
            if lookup:
                q |= Q(**{lookup: user})
    user_codes = _user_department_codes(user)
    if user_codes:
        if "department" in f:
            q |= Q(department__slug__in=user_codes) | Q(department__code__in=user_codes)
        if "departments" in f:
            q |= Q(departments__slug__in=user_codes) | Q(departments__code__in=user_codes)
    if "is_broadcast" in f:
        q |= Q(is_broadcast=True)
    return q


def _order_newest(qs, N_or_R):
    f = _model_fields(N_or_R)
    order_fields = []
    for cand in ("created_at", "created_on", "publish_at", "starts_at", "id"):
        if cand in f:
            order_fields.append(f"-{cand}")
    if order_fields:
        return soft_call(
            "nav.order_newest", lambda: qs.order_by(*order_fields), default=qs
        )
    return qs


def _notification_title_body_dict(obj) -> Tuple[str, str]:
    f = _model_fields(obj.__class__)
    title = ""
    for cand in ("title", "subject", "heading", "name"):
        if cand in f:
            with soft_fail("nav.notification_title", field=cand):
                title = getattr(obj, cand) or ""
                break
    body = ""
    for cand in ("body", "message", "content", "text", "details"):
        if cand in f:
            with soft_fail("nav.notification_body", field=cand):
                body = getattr(obj, cand) or ""
                break
    return (str(title).strip() or "إشعار"), str(body or "")


def _build_hero_payload_from_notification(n) -> Dict[str, Any]:
    title, body = _notification_title_body_dict(n)
    data: Dict[str, Any] = {
        "id": getattr(n, "pk", None),
        "title": title,
        "body": body,
        "sender_name": _notification_sender_str(n),
    }
    f = _model_fields(n.__class__)
    for cand in ("action_url", "url", "link"):
        if cand in f:
            with soft_fail("nav.notification_action_url", field=cand):
                data["action_url"] = getattr(n, cand) or ""
                break
    return data


def _pick_hero_notification(user, request: Optional[HttpRequest] = None) -> Optional[Dict[str, Any]]:
    """
    يُعيد حمولة نافذة هيرو المنبثقة:
    - أولاً عبر NotificationRecipient (غير مقروء → أحدث)،
    - وإلا فالباك عبر Notification موجه للمستخدم (إن وُجد).
    """
    N, R = _notification_models()
    if not N:
        return None

    uid = int(getattr(user, "id", 0) or 0)
    sid = _active_school_id_from_request(request)
    ttl = _nav_cache_ttl_seconds()
    cache_key = f"nav:hero:v1:u{uid}:s{sid}"
    if uid and ttl > 0:
        cached_val = _cache_get(cache_key)
        if cached_val is not None:
            return cached_val

    # المسار المفضل: عبر سجلات الاستلام (Recipient)
    if R:
        fR = _model_fields(R)

        # اكتشاف أسماء الحقول
        notif_fk = None
        for cand in ("notification", "notif", "message"):
            if cand in fR:
                notif_fk = cand
                break
        user_fk = None
        for cand in ("teacher", "user", "recipient"):
            if cand in fR:
                user_fk = cand
                break

        if notif_fk and user_fk:
            try:
                now = timezone.now()
                qs = R.objects.select_related(notif_fk)

                # فلترة تخصّص المستلم
                qs = qs.filter(**{user_fk: user})

                # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL).
                # تعثّرٌ هنا يُسقط حاجز العزل بين المدارس، فلا يجوز أن يمرّ بلا
                # أثر مهما بدا نادراً.
                with soft_fail("nav.hero_school_isolation", user_id=uid, school_id=sid):
                    if request is not None:
                        sid = request.session.get("active_school_id")
                    else:
                        sid = None
                    fN = _model_fields(N)
                    if sid and "school" in fN:
                        qs = qs.filter(
                            Q(**{f"{notif_fk}__school_id": sid}) |
                            Q(**{f"{notif_fk}__school_id__isnull": True})
                        )

                # غير مقروء
                if "is_read" in fR:
                    qs = qs.filter(is_read=False)
                elif "read_at" in fR:
                    qs = qs.filter(Q(read_at__isnull=True))

                # استبعاد المنتهي/غير المنشور عبر FK إلى Notification
                fN = _model_fields(N)
                if "expires_at" in fN:
                    qs = qs.filter(**{f"{notif_fk}__expires_at__gt": now}) | qs.filter(
                        **{f"{notif_fk}__expires_at__isnull": True}
                    )
                if "is_active" in fN:
                    qs = qs.filter(**{f"{notif_fk}__is_active": True})
                if "publish_at" in fN:
                    qs = qs.filter(**{f"{notif_fk}__publish_at__lte": now}) | qs.filter(
                        **{f"{notif_fk}__publish_at__isnull": True}
                    )
                if "starts_at" in fN:
                    qs = qs.filter(**{f"{notif_fk}__starts_at__lte": now}) | qs.filter(
                        **{f"{notif_fk}__starts_at__isnull": True}
                    )
                if "ends_at" in fN:
                    qs = qs.filter(**{f"{notif_fk}__ends_at__gte": now}) | qs.filter(
                        **{f"{notif_fk}__ends_at__isnull": True}
                    )

                # استبعاد الكوكي (Dismiss)
                qs = _exclude_notif_dismissed_cookies_recipient_qs(qs, request, notif_fk)

                # ترتيب بالأحدث الممكن
                qs = _order_newest(qs, R)

                rec = qs.first()
                if rec:
                    n = soft_call(
                        "nav.hero_recipient_notification",
                        lambda: getattr(rec, notif_fk),
                        default=None,
                        user_id=uid,
                    )
                    if n:
                        payload = _build_hero_payload_from_notification(n)
                        if uid and ttl > 0:
                            _cache_set(cache_key, payload, ttl)
                        return payload
            except Exception:
                _degraded("nav.hero_via_recipient", user_id=uid, school_id=sid)

    # فالباك: مباشرة من Notification (يعمل فقط إن كان هناك استهداف عبر حقول الـ Notification نفسها)
    try:
        now = timezone.now()
        base_qs = _published_notifications_qs(N).filter(_targeted_for_user_q(N, user)).distinct()

        # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL)
        with soft_fail("nav.hero_fallback_school_isolation", user_id=uid):
            sid = request.session.get("active_school_id") if request is not None else None
            fN = _model_fields(N)
            if sid and "school" in fN:
                base_qs = base_qs.filter(Q(school_id=sid) | Q(school__isnull=True))
        base_qs = _exclude_notif_dismissed_cookies_notif_qs(base_qs, request)
        base_qs = _order_newest(base_qs, N)

        obj = base_qs.only("id")[:1].first()
        if obj:
            payload = _build_hero_payload_from_notification(obj)
            if uid and ttl > 0:
                _cache_set(cache_key, payload, ttl)
            return payload

        # فرصة ثانية: نطاق آخر 3 أيام
        try:
            fN = _model_fields(N)
            recent_qs = _published_notifications_qs(N).filter(_targeted_for_user_q(N, user)).distinct()
            three_days_ago = now - timedelta(days=3)
            if "created_at" in fN:
                recent_qs = recent_qs.filter(created_at__gte=three_days_ago)
            elif "created_on" in fN:
                recent_qs = recent_qs.filter(created_on__gte=three_days_ago)
            elif "publish_at" in fN:
                recent_qs = recent_qs.filter(publish_at__gte=three_days_ago)
            recent_qs = _exclude_notif_dismissed_cookies_notif_qs(recent_qs, request)
            recent_qs = _order_newest(recent_qs, N)
            obj = recent_qs.only("id")[:1].first()
            if obj:
                payload = _build_hero_payload_from_notification(obj)
                if uid and ttl > 0:
                    _cache_set(cache_key, payload, ttl)
                return payload
        except Exception:
            _degraded("nav.hero_recent_window", user_id=uid)
    except Exception:
        _degraded("nav.hero_fallback", user_id=uid)

    if uid and ttl > 0:
        _cache_set(cache_key, None, ttl)

    return None


def _unread_count(user, request: Optional[HttpRequest] = None) -> int:
    """عدد الإشعارات غير المقروءة للمستخدم."""
    N, R = _notification_models()
    if not N:
        return 0

    uid = int(getattr(user, "id", 0) or 0)
    sid = _active_school_id_from_request(request)
    ttl = _nav_cache_ttl_seconds()
    cache_key = f"nav:unread:v3:u{uid}:s{sid}"
    if uid and ttl > 0:
        cached_val = _cache_get(cache_key)
        if cached_val is not None:
            return int(cached_val)

    # المسار المفضل: NotificationRecipient
    if R:
        try:
            fR = _model_fields(R)
            user_fk = None
            for cand in ("teacher", "user", "recipient"):
                if cand in fR:
                    user_fk = cand
                    break
            if not user_fk:
                return 0

            notif_fk = None
            for cand in ("notification", "notif", "message"):
                if cand in fR:
                    notif_fk = cand
                    break

            qs = R.objects.filter(**{user_fk: user})

            # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL)
            with soft_fail("nav.unread_school_isolation", user_id=uid, school_id=sid):
                sid = request.session.get("active_school_id") if request is not None else None
                fN = _model_fields(N)
                if sid and notif_fk and "school" in fN:
                    qs = qs.filter(
                        Q(**{f"{notif_fk}__school_id": sid}) |
                        Q(**{f"{notif_fk}__school_id__isnull": True})
                    )

            # استبعاد المنتهي عبر FK إن أمكن
            if notif_fk:
                fN = _model_fields(N)
                now = timezone.now()

                # الإشعار أو النشرة يحتاج انتباه المستخدم ما دام غير مقروء،
                # وتبقى النشرة الموقعة في العداد حتى بعد فتحها إن لم يكتمل
                # توقيعها. أما التعاميم فلها عداد مستقل في شريط التنقل.
                with soft_fail("nav.unread_exclude_circulars", user_id=uid):
                    unread_q = Q(is_read=False) if "is_read" in fR else Q(read_at__isnull=True)
                    if "kind" in fN:
                        newsletter_signature_q = (
                            Q(
                                **{
                                    f"{notif_fk}__kind": "newsletter",
                                    f"{notif_fk}__requires_signature": True,
                                    "is_signed": False,
                                }
                            )
                            if "is_signed" in fR
                            else Q(pk__in=[])
                        )
                        qs = qs.filter(
                            Q(**{f"{notif_fk}__kind__in": ["notification", "newsletter"]})
                            & (unread_q | newsletter_signature_q)
                        )
                    else:
                        qs = qs.filter(unread_q).filter(
                            **{f"{notif_fk}__requires_signature": False}
                        )

                if "expires_at" in fN:
                    qs = qs.filter(**{f"{notif_fk}__expires_at__gt": now}) | qs.filter(
                        **{f"{notif_fk}__expires_at__isnull": True}
                    )
                if "is_active" in fN:
                    qs = qs.filter(**{f"{notif_fk}__is_active": True})

            val = _safe_count(qs)
            if uid and ttl > 0:
                _cache_set(cache_key, int(val), ttl)
            return val
        except Exception:
            _degraded("nav.unread_via_recipient", user_id=uid, school_id=sid)
            return 0

    # فالباك: بلا سجل استلام → نعجز عن قياس غير المقروء بدقة
    try:
        qs = _published_notifications_qs(N).filter(_targeted_for_user_q(N, user)).distinct()

        # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL)
        with soft_fail("nav.unread_fallback_school_isolation", user_id=uid):
            sid = request.session.get("active_school_id") if request is not None else None
            fN = _model_fields(N)
            if sid and "school" in fN:
                qs = qs.filter(Q(school_id=sid) | Q(school__isnull=True))
        val = _safe_count(qs)
        if uid and ttl > 0:
            _cache_set(cache_key, int(val), ttl)
        return val
    except Exception:
        _degraded("nav.unread_fallback", user_id=uid)
        return 0


def _reverse_any(names: Iterable[str]) -> Optional[str]:
    """أول مسار قابل للحل من قائمة مرشّحين.

    ``NoReverseMatch`` هنا **متوقَّع لا استثنائي**: القائمة مرشّحون، وفشل أحدهم
    هو معنى وجود التالي. فيُصطاد بنوعه وحده — و``except Exception`` كان يبتلع
    معه أخطاء استيراد وإعداد حقيقية.
    """
    for n in names:
        try:
            return reverse(n)
        except NoReverseMatch:
            continue
    return None


def _school_role_labels(active_school: Optional[School]) -> Dict[str, str]:
    """Compatibility wrapper around the canonical gender-aware labels."""
    labels = school_gender_labels(active_school)
    return {
        "manager": str(labels["manager"]),
        "teacher": str(labels["teacher"]),
        "teachers": str(labels["teachers"]),
        "teachers_obj": str(labels["teachers_object"]),
        "head": str(labels["head"]),
        "head_of_department": str(labels["head_of_department"]),
        "admin_staff": str(labels["admin_staff"]),
        "lab_tech": str(labels["lab_tech"]),
    }


def _pending_signatures_count(user, request: Optional[HttpRequest] = None) -> int:
    """عدد التعاميم التي تتطلب توقيع ولم يتم توقيعها بعد للمستخدم."""
    N, R = _notification_models()
    if N is None or R is None:
        return 0

    fR = _model_fields(R)
    if "is_signed" not in fR:
        return 0

    # تحديد اسم FK للإشعار داخل سجل الاستلام
    notif_fk = None
    for cand in ("notification", "notif", "announcement", "message"):
        if cand in fR:
            notif_fk = cand
            break
    if not notif_fk:
        return 0

    # تحديد اسم المستخدم داخل السجل
    user_fk = None
    for cand in ("teacher", "user"):
        if cand in fR:
            user_fk = cand
            break
    if not user_fk:
        return 0

    uid = int(getattr(user, "id", 0) or 0)
    sid = _active_school_id_from_request(request)
    ttl = _nav_cache_ttl_seconds()
    cache_key = f"nav:pending-sign:v1:u{uid}:s{sid}"
    if uid and ttl > 0:
        cached_val = _cache_get(cache_key)
        if cached_val is not None:
            return int(cached_val)

    now = timezone.now()
    try:
        qs = R.objects.filter(**{user_fk: user, "is_signed": False, f"{notif_fk}__requires_signature": True})

        # فلترة نشر/انتهاء على مستوى Notification إن وُجدت
        fN = _model_fields(N)
        if "kind" in fN:
            qs = qs.filter(**{f"{notif_fk}__kind": "circular"})
        with soft_fail("nav.pending_signatures_active_filter", user_id=uid):
            if "is_active" in fN:
                qs = qs.filter(**{f"{notif_fk}__is_active": True})
        with soft_fail("nav.pending_signatures_expiry_filter", user_id=uid):
            if "expires_at" in fN:
                qs = qs.filter(
                    Q(**{f"{notif_fk}__expires_at__gte": now}) | Q(**{f"{notif_fk}__expires_at__isnull": True})
                )

        # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL)
        with soft_fail("nav.pending_signatures_school_isolation", user_id=uid, school_id=sid):
            sid = request.session.get("active_school_id") if request is not None else None
            if sid and "school" in fN:
                qs = qs.filter(Q(**{f"{notif_fk}__school_id": sid}) | Q(**{f"{notif_fk}__school__isnull": True}))

        qs = _exclude_notif_dismissed_cookies_recipient_qs(qs, request, notif_fk=notif_fk)
        val = _safe_count(qs)
        if uid and ttl > 0:
            _cache_set(cache_key, int(val), ttl)
        return val
    except Exception:
        _degraded("nav.pending_signatures", user_id=uid, school_id=sid)
        return 0


# -----------------------------
# أعلام الصلاحيات المُنطَقة
# -----------------------------
# **علمٌ لكل صلاحية تفتح باباً في القائمة.** كانت القائمة تعرف شرطاً واحداً هو
# «مدير مدرسة»، فكل ما يُمنح للوكيل والموظف الإداري يعمل في العرض ولا يجد
# رابطاً: الصلاحية نافذة، والشاشة قائمة، والطريق إليها كتابةُ المسار يدوياً.
#
# والاتجاه المعاكس محكومٌ بالأعلام نفسها: رابطٌ يظهر لمن لا يملك صلاحيته يردّه
# العرضُ برسالة منع — وفعلٌ مرئي ممنوع أسوأ ما يقابله مستخدم.
#
# **الحساب مرة واحدة لكل طلب.** ``scope_capabilities`` و``delegated_capabilities``
# تُخزَّنان على كائن المستخدم، فبناء كل الأعلام من اتحادهما استعلامان لا استعلام
# لكل علم — والبديل (نداء ``capability_source`` لكل رمز) يضاعف الكلفة بلا فائدة.
# الرموز تُقرأ من مرجع الصلاحيات لا تُكتب نصاً: رمزٌ مكتوب بيده هنا يصمت عند
# أي تغيير في المرجع، فيختفي رابطٌ بلا خطأ واحد.
_NAV_CAPABILITY_FLAGS: tuple[tuple[str, str], ...] = (
    ("CAN_VIEW_SCHOOL_DASHBOARD", caps.VIEW_SCHOOL_DASHBOARD),
    ("CAN_REVIEW_APPROVALS", caps.REVIEW_REPORTS),
    ("CAN_RECOMMEND_APPROVAL", caps.RECOMMEND_APPROVAL),
    ("CAN_VIEW_ACHIEVEMENTS", caps.VIEW_ACHIEVEMENTS),
    ("CAN_HANDLE_REQUESTS", caps.HANDLE_REQUESTS),
    ("CAN_DRAFT_CIRCULARS", caps.DRAFT_CIRCULARS),
    ("CAN_VIEW_SCHOOL_AUDIT", caps.VIEW_AUDIT_LOG),
    ("CAN_ASSIGN_TASKS", caps.ASSIGN_TASKS),
    ("CAN_MANAGE_MEETINGS", caps.MANAGE_MEETINGS),
    ("CAN_TRACK_PLANS", caps.TRACK_PLANS),
    ("CAN_ARCHIVE_DOCUMENTS", caps.ARCHIVE_DOCUMENTS),
    ("CAN_MANAGE_LAB", caps.MANAGE_LAB),
)

_NAV_SCOPE_DEPENDENT_CODES = {
    caps.VIEW_SCHOOL_DASHBOARD,
    caps.REVIEW_REPORTS,
    caps.VIEW_ACHIEVEMENTS,
    caps.HANDLE_REQUESTS,
    caps.VIEW_AUDIT_LOG,
    caps.ASSIGN_TASKS,
    caps.TRACK_PLANS,
    caps.MANAGE_LAB,
}


def _empty_capability_flags() -> Dict[str, bool]:
    return {name: False for name, _code in _NAV_CAPABILITY_FLAGS}


def _capability_flags(
    user,
    active_school: Optional[School],
    *,
    is_school_manager: bool,
) -> Dict[str, bool]:
    """علمٌ لكل صلاحية، للمستخدم في مدرسته النشطة.

    مدير المدرسة ومالك النظام يمرّان بكل الأعلام: الأول يملك كل شيء في مدرسته
    بحكم دوره، والثاني يمر دائماً — وهي القاعدة نفسها التي تنفّذها
    ``has_capability``، مكتوبةً هنا مرة واحدة لا في أحد عشر شرطاً.
    """
    flags = _empty_capability_flags()
    if not getattr(user, "is_authenticated", False):
        return flags

    if getattr(user, "is_superuser", False) or is_school_manager:
        # المدير بلا مدرسة نشطة لا يُفتح له شيء: كل هذه الشاشات تسأل عن مدرسة
        # أولاً، ورابطٌ يقود إلى «فضلاً اختر مدرسة» ليس رابطاً.
        allow_all = bool(active_school is not None or getattr(user, "is_superuser", False))
        return {name: allow_all for name, _code in _NAV_CAPABILITY_FLAGS}

    if active_school is None:
        return flags

    # تعثّرٌ هنا يُطفئ **كل** أعلام الصلاحيات، فيختفي من الشريط كلُّ ما يملكه
    # الوكيل والموظف الإداري ويبدو النظام وكأنه سحب صلاحياتهم. أخطر من أن يمرّ
    # بلا سطر سجلّ.
    from .permissions import delegated_capabilities, scope_capabilities

    granted = soft_call(
        "nav.capability_flags",
        lambda: set(scope_capabilities(user, active_school))
        | set(delegated_capabilities(user, active_school)),
        default=None,
        user_id=getattr(user, "pk", None),
        school_id=getattr(active_school, "pk", None),
    )
    if granted is None:
        return flags

    for name, code in _NAV_CAPABILITY_FLAGS:
        flags[name] = code in granted
    return flags


# ما يُجمع في مجموعة «الإشراف» للوكيل والموظف الإداري. الشريط المسطّح لا يحتمل
# ثمانية مداخل جديدة — وهي العلّة نفسها التي جُمّع من أجلها شريط المدير حين
# امتلأ حتى ركب على اسم المدرسة.
_SUPERVISION_FLAGS: tuple[str, ...] = (
    "CAN_VIEW_SCHOOL_DASHBOARD",
    "CAN_ASSIGN_TASKS",
    "CAN_MANAGE_MEETINGS",
    "CAN_TRACK_PLANS",
    "CAN_VIEW_ACHIEVEMENTS",
    "CAN_HANDLE_REQUESTS",
    "CAN_DRAFT_CIRCULARS",
    "CAN_ARCHIVE_DOCUMENTS",
    "CAN_MANAGE_LAB",
)


def _shows_supervision_group(flags: Dict[str, bool], *, is_school_manager: bool) -> bool:
    """هل يستحق هذا المستخدم مجموعة «الإشراف» في شريطه؟

    المدير خارجها: مجموعاته الخمس تغطّي هذه الوجهات كلها، وإضافتها له تكرار.
    ومن لا يملك منها شيئاً خارجها كذلك — فمجموعةٌ فارغة زرٌّ يفتح لا شيء.
    """
    if is_school_manager:
        return False
    return any(bool(flags.get(name)) for name in _SUPERVISION_FLAGS)
# =============================================================================
# كونتكست التنقل
# =============================================================================
# **لماذا فُكِّكت هذه الدالة.** كانت ``nav_context`` واحدةً من 425 سطراً تعمل في
# كل طلب لكل مستخدم — أسخن مسار في المنصة وأقلّه قابليةً للقراءة. وطولها لم يكن
# مسألة ذوق: بناءُ الشريط يمرّ بأحد عشر قراراً مستقلاً (المدرسة النشطة، عضويات
# الأقسام، نطاق الإدارة، عدّادات التذاكر، الأرشيف، المدير التنفيذي، أدوار
# المدرسة، الصلاحيات، الإشعارات، مبدّل المدارس)، وكانت كلها في نطاقٍ واحد
# تتشارك عشرات المتغيّرات المحلية. فأيّ تعديل في أحدها يقرأ الأربعين سطراً التي
# قبله ليطمئن، ولا شيء يمنعه من إضافة استعلامٍ لكل صفحة في النظام.
#
# فصار لكل قرارٍ دالةٌ تُسمّيه، مدخلاتُها صريحة ومخرجاتُها قاموسٌ من مفاتيح
# القالب. و``nav_context`` تنسّق بينها فقط.
#
# **العقد محروس** في ``reports/tests/test_nav_context_contract.py``: مجموعةُ
# المفاتيح كاملةً لكل دور، **وسقفُ الاستعلامات**. والثاني هو المهم — انحدار
# الأداء هنا صامت: لا خطأ ولا بطء ملحوظ محلياً، فقط استعلامٌ إضافي مضروبٌ في كل
# صفحة وكل مستخدم.


def _anonymous_nav_context() -> Dict[str, Any]:
    """شكلُ الكونتكست للزائر — **الشكل نفسه** بقيم صفرية.

    القالب واحد للحالتين، ومفتاحٌ يوجد للمسجَّل دون الزائر يُقرأ فارغاً بلا خطأ
    — فيختفي عنصرٌ في حالةٍ ويظهر في أخرى بلا سبب مفهوم. ولذلك تُبنى المجموعة
    كاملةً هنا لا مختصرةً.
    """
    return {
        "NAV_MY_OPEN_TICKETS": 0,
        "NAV_ASSIGNED_TO_ME": 0,
        "IS_OFFICER": False,
        "OFFICER_DEPARTMENT": None,
        "OFFICER_DEPARTMENTS": [],
        "SHOW_OFFICER_REPORTS_LINK": False,
        "SHOW_DEPARTMENT_REPORTS_LINK": False,
        "SHOW_SCHOOL_REPORTS_LINK": False,
        "SHOW_ARCHIVE_LINK": False,
        "ARCHIVE_ADDON_ACTIVE": False,
        "HAS_SAVED_ARCHIVE": False,
        "IS_SCHOOL_MANAGER": False,
        "IS_SCHOOL_DEPUTY": False,
        "IS_ADMIN_STAFF": False,
        "HAS_TEACHER_ROLE": False,
        "SHOW_PERSONAL_ACHIEVEMENT": False,
        "IS_LAB_TECHNICIAN": False,
        "SHOW_LAB_NAV": False,
        "SHOW_ASSIGNED_TO_ME": False,
        "SHOW_SUPERVISION_GROUP": False,
        "IS_EXECUTIVE_DIRECTOR": False,
        "IS_GROUP_ONLY_DIRECTOR": False,
        "GROUP_NAME": None,
        **_empty_capability_flags(),
        "DEPARTMENT_REPORTS_URLNAME": None,
        "NAV_OFFICER_REPORTS": 0,
        "SHOW_ADMIN_DASHBOARD_LINK": False,
        "NAV_NOTIFICATIONS_UNREAD": 0,
        "NAV_SIGNATURES_PENDING": 0,
        "NAV_NOTIFICATION_HERO": None,
        "CAN_SEND_NOTIFICATIONS": False,
        "SEND_NOTIFICATION_URL": None,
        "SCHOOL_ID": None,
        "SCHOOL_NAME": None,
        "SCHOOL_LOGO_URL": None,
        "USER_SCHOOLS": [],
        "USER_ROLE_LABEL": None,
        **school_gender_template_context(None),
    }


def _nav_cache_key(request: HttpRequest, user) -> Optional[str]:
    """مفتاح كاش الشريط، أو ``None`` إن تعذّر بناؤه.

    يدخل في المفتاح كل ما يغيّر الناتج: المستخدم، والمدرسة النشطة، ودوره،
    وأعلامُ حسابه، ورقمُ إصدار دوره (يُزاد عند تغيير الصلاحيات)، وبصمةُ كوكيز
    الإخفاء (فإخفاء إشعار يُبطل النسخة فوراً).

    ``v7`` يُزاد مع كل تغيير في **شكل** الناتج لا في قيمته: قيمةٌ مخزَّنة بالشكل
    القديم تصل قالباً يقرأ مفاتيح لا وجود لها فيها، فتُقرأ فارغةً بلا خطأ —
    وتختفي روابط لثوانٍ بعد كل نشر.
    """
    sid_raw = soft_call(
        "nav.context_session_school",
        lambda: request.session.get("active_school_id"),
        default=None,
    )
    sid_for_key = str(int(sid_raw)) if sid_raw else "none"

    def _dismissed_signature() -> str:
        dismissed_keys = [k for k in (request.COOKIES or {}).keys() if k.startswith("notif_dismissed_")]
        dismissed_keys.sort()
        return hashlib.sha256("|".join(dismissed_keys).encode("utf-8")).hexdigest()[:12]

    dismissed_sig = soft_call(
        "nav.context_cookie_signature", _dismissed_signature, default="nocookies"
    )

    def _build() -> str:
        uid = int(getattr(user, "id", 0) or 0)
        role_version = int(cache.get(f"navctx:role-version:u{uid}", 1) or 1)
        user_flags = "".join(
            [
                "s" if getattr(user, "is_superuser", False) else "-",
                "f" if getattr(user, "is_staff", False) else "-",
            ]
        )
        role_id = int(getattr(user, "role_id", 0) or 0)
        return (
            f"navctx:v7:u{uid}:s{sid_for_key}:r{role_id}:"
            f"f{user_flags}:v{role_version}:c{dismissed_sig}"
        )

    return soft_call(
        "nav.context_cache_key",
        _build,
        default=None,
        user_id=getattr(user, "pk", None),
    )


def _resolve_active_school(request: HttpRequest) -> Optional[School]:
    """المدرسة النشطة، مُعاد استخدامها من الـ middleware إن أمكن.

    ``ActiveSchoolGuardMiddleware`` حمّلها وخوّلها بالفعل، فقراءتها من الطلب
    توفّر استعلاماً في كل صفحة. والسقوط إلى الجلسة لمسارات لا يمرّ عليها الحارس.
    """
    active_school = getattr(request, "active_school", None)
    if active_school is not None:
        return active_school

    def _load():
        sid = request.session.get("active_school_id")
        return School.objects.filter(pk=sid, is_active=True).first() if sid else None

    return soft_call("nav.context_active_school", _load, default=None)


def _ticket_counters(user, active_school: Optional[School]) -> Dict[str, int]:
    """عدّادا التذاكر — في استعلامٍ واحد لا اثنين."""

    def _aggregate():
        ticket_base = Ticket.objects.filter(status__in=UNRESOLVED_STATES)
        if active_school is not None:
            ticket_base = ticket_base.filter(school=active_school)
        return ticket_base.aggregate(
            my_open=Count("id", filter=Q(creator=user)),
            assigned_open=Count("id", filter=Q(assignee=user)),
        )

    return soft_call(
        "nav.ticket_counters",
        _aggregate,
        default={"my_open": 0, "assigned_open": 0},
        user_id=getattr(user, "pk", None),
        school_id=getattr(active_school, "pk", None),
    )


def _manager_scope(user, active_school: Optional[School]) -> Tuple[bool, bool]:
    """(يدير مدرسةً ما، يدير المدرسة النشطة).

    الأول يفتح رابط لوحة الإدارة، والثاني يقرّر شكل الشريط كله.
    """
    try:
        manager_school_ids = get_school_manager_school_ids(user)
    except Exception:
        # مديرٌ يُقرأ «ليس مديراً» يفقد شريطه كاملاً. أسوأ تدهور ممكن هنا.
        _degraded("nav.manager_schools", user_id=getattr(user, "pk", None))
        return (False, False)

    any_school_manager = bool(manager_school_ids)
    if active_school is not None:
        is_here = int(getattr(active_school, "id", 0) or 0) in manager_school_ids
    else:
        is_here = any_school_manager
    return (any_school_manager, is_here)


def _department_roles(
    user, active_school: Optional[School]
) -> Tuple[List[Department], List[Department]]:
    """(أقسامٌ يرأسها، أقسامٌ هو عضو فيها) — من استعلامٍ واحد لا اثنين.

    الفصل بين الدورين يتم في بايثون على الصفوف نفسها: استعلامان منفصلان لكل
    دور كانا يقرآن الجدول مرتين لنتيجةٍ واحدة.
    """
    officer_depts: List[Department] = []
    member_depts: List[Department] = []

    Membership = _get_membership_model()
    if Membership is None:
        return (officer_depts, member_depts)

    try:
        membs = Membership.objects.select_related("department").filter(
            teacher=user,
            department__is_active=True,
        )
        if active_school is not None and "school" in _model_fields(Department):
            membs = membs.filter(department__school=active_school)

        officer_values = set(_officer_role_values(Membership))
        teacher_values = set(_teacher_role_values(Membership))
        seen_officer: set[int] = set()
        seen_member: set[int] = set()
        for m in membs:
            d = getattr(m, "department", None)
            if d is None or getattr(d, "pk", None) is None:
                continue
            role_type = getattr(m, "role_type", None)
            did = int(d.pk)
            if role_type in officer_values and did not in seen_officer:
                seen_officer.add(did)
                officer_depts.append(d)
            if role_type in teacher_values and did not in seen_member:
                seen_member.add(did)
                member_depts.append(d)
    except Exception:
        _degraded("nav.department_memberships", user_id=getattr(user, "pk", None))
        return ([], [])

    return (officer_depts, member_depts)


def _officer_reports_count(
    user,
    active_school: Optional[School],
    *,
    officer_depts: List[Department],
    is_officer: bool,
) -> int:
    """عدد تقارير آخر سبعة أيام ضمن نطاق رئيس القسم."""
    try:
        start_date = timezone.localdate() - timedelta(days=7)
        base_qs = Report.objects.filter(report_date__gte=start_date)
        if active_school is not None:
            base_qs = base_qs.filter(school=active_school)

        if getattr(user, "is_superuser", False):
            return base_qs.count()

        if not is_officer:
            return 0

        dept_ids = [int(getattr(d, "pk", 0) or 0) for d in officer_depts if getattr(d, "pk", None)]
        if not dept_ids:
            return 0

        rt_ids = soft_call(
            "nav.officer_report_types",
            lambda: {
                int(x)
                for x in Department.objects.filter(pk__in=dept_ids).values_list(
                    "reporttypes__id", flat=True
                )
                if x
            },
            default=set(),
        )
        if not rt_ids:
            return 0
        return base_qs.filter(category_id__in=list(rt_ids)).count()
    except Exception:
        _degraded("nav.officer_reports_count", user_id=getattr(user, "pk", None))
        return 0


def _archive_visibility(
    active_school: Optional[School], *, is_school_manager: bool
) -> Dict[str, Any]:
    """متى يظهر رابط الأرشيف، ولماذا.

    ثلاثة أسباب مستقلة: أن يكون المستخدم مديراً، أو أن تكون الإضافة مفعّلة، أو
    أن يوجد أرشيفٌ محفوظ بالفعل — فالأخير يعني بياناتٍ لا يجوز أن يُحجب عنها
    صاحبُها لأن الاشتراك انتهى.
    """
    if active_school is None:
        return {
            "SHOW_ARCHIVE_LINK": False,
            "ARCHIVE_ADDON_ACTIVE": False,
            "HAS_SAVED_ARCHIVE": False,
        }

    has_saved_archive = soft_call(
        "nav.saved_archive_exists",
        lambda: SchoolYearArchive.objects.filter(
            school=active_school,
            status__in=[
                SchoolYearArchive.Status.READY,
                SchoolYearArchive.Status.PARTIAL,
            ],
        ).exists(),
        default=False,
        school_id=getattr(active_school, "pk", None),
    )
    archive_addon_active = bool(school_has_archive_addon(active_school))

    return {
        "SHOW_ARCHIVE_LINK": bool(
            is_school_manager or archive_addon_active or has_saved_archive
        ),
        "ARCHIVE_ADDON_ACTIVE": archive_addon_active,
        "HAS_SAVED_ARCHIVE": has_saved_archive,
    }


def _executive_director_context(user) -> Dict[str, Any]:
    """المدير التنفيذي لمجموعة المدارس — دورٌ إشرافي لا يفتح مسار تحرير.

    ``IS_GROUP_ONLY_DIRECTOR`` يميّز من **لا عضوية مدرسة له** — وهي حالته
    المُصمَّمة — عمّن جمع الصفتين: شريط الأول بلا شاشات العمل الشخصي (تقاريره
    وطلباته وملف إنجازه)، فكلها تسأل عن مدرسة نشطة أولاً وتردّه.
    """
    try:
        from .models import SchoolMembership as _SchoolMembership
        from .permissions import executive_director_groups as _director_groups
        from .permissions import is_executive_director as _is_executive_director

        if not bool(_is_executive_director(user)):
            return {
                "IS_EXECUTIVE_DIRECTOR": False,
                "IS_GROUP_ONLY_DIRECTOR": False,
                "GROUP_NAME": None,
            }

        group_only = not _SchoolMembership.objects.filter(
            teacher=user, is_active=True
        ).exists()
        # اسم المجموعة يحلّ محل اسم المدرسة في الترويسة: بلا مدرسة نشطة كانت
        # الترويسة تقول «منصة توثيق» — اسمُ المنتَج لا اسمُ ما يقوده.
        first_group = _director_groups(user).first()
        return {
            "IS_EXECUTIVE_DIRECTOR": True,
            "IS_GROUP_ONLY_DIRECTOR": group_only,
            "GROUP_NAME": getattr(first_group, "name", None),
        }
    except Exception:
        # يُقرأ «ليس تنفيذياً» فيفقد لوحة المجموعة كاملة.
        _degraded("nav.executive_director", user_id=getattr(user, "pk", None))
        return {
            "IS_EXECUTIVE_DIRECTOR": False,
            "IS_GROUP_ONLY_DIRECTOR": False,
            "GROUP_NAME": None,
        }


def _school_role_flags(user, active_school: Optional[School]) -> Dict[str, bool]:
    """أدوار المستخدم داخل مدرسته النشطة.

    القائمة كانت تعرف «مديراً» أو «معلّماً» ولا شيء بينهما — والوكيل والموظف
    الإداري والمحضّر بلا شريط. والمحضّر يُحسم بمسمّاه الوظيفي: المختبر عملُه لا
    صلاحيةٌ تُمنح له.
    """
    empty = {
        "IS_SCHOOL_DEPUTY": False,
        "IS_ADMIN_STAFF": False,
        "HAS_TEACHER_ROLE": False,
        "IS_LAB_TECHNICIAN": False,
        "SHOW_LAB_NAV": False,
    }
    if active_school is None:
        return empty

    try:
        from .models import SchoolMembership as _SchoolMembership
        from .permissions import can_view_lab as _can_view_lab
        from .permissions import is_admin_staff as _is_admin_staff
        from .permissions import is_lab_technician as _is_lab_technician
        from .permissions import is_school_deputy as _is_school_deputy

        return {
            "IS_SCHOOL_DEPUTY": bool(_is_school_deputy(user, active_school)),
            "IS_ADMIN_STAFF": bool(_is_admin_staff(user, active_school)),
            "HAS_TEACHER_ROLE": _SchoolMembership.objects.filter(
                school=active_school,
                teacher=user,
                role_type=_SchoolMembership.RoleType.TEACHER,
                is_active=True,
            ).exists(),
            "IS_LAB_TECHNICIAN": bool(_is_lab_technician(user, active_school)),
            "SHOW_LAB_NAV": bool(_can_view_lab(user, active_school)),
        }
    except Exception:
        # الوكيل والموظف والمحضّر يُقرأون «معلّمين» — يفقدون شريطهم بالكامل.
        _degraded(
            "nav.school_roles",
            user_id=getattr(user, "pk", None),
            school_id=getattr(active_school, "pk", None),
        )
        return empty


def _notification_counters(user, request: HttpRequest) -> Dict[str, Any]:
    """عدّادا الإشعارات والتواقيع، والإشعار البارز.

    الدوال الثلاث تُمسك تعثّرها داخلياً وتُبلّغ عنه؛ فالغلاف هنا شبكةُ أمانٍ
    أخيرة لا موضعَ التقاطٍ أول — ولذلك يحمل كلٌّ اسماً مستقلاً يميّزه في السجل.
    """
    uid = getattr(user, "pk", None)
    return {
        "NAV_NOTIFICATIONS_UNREAD": soft_call(
            "nav.unread_count_outer",
            lambda: _unread_count(user, request=request),
            default=0,
            user_id=uid,
        ),
        "NAV_SIGNATURES_PENDING": soft_call(
            "nav.pending_signatures_outer",
            lambda: _pending_signatures_count(user, request=request),
            default=0,
            user_id=uid,
        ),
        "NAV_NOTIFICATION_HERO": soft_call(
            "nav.hero_outer",
            lambda: _pick_hero_notification(user, request=request),
            default=None,
            user_id=uid,
        ),
    }


def _school_switcher(user, active_school: Optional[School]) -> Dict[str, Any]:
    """المدرسة المعروضة في الترويسة، وقائمة ما يمكن التبديل إليه.

    الدور مدرسي لا حسابي، فتظهر كل العضويات النشطة حتى لمن يدير مدرسة ويدرّس
    في أخرى؛ والمدرسة المختارة هي ما يقرّر شكل الشريط.
    """
    empty = {
        "SCHOOL_ID": None,
        "SCHOOL_NAME": None,
        # شعارات المدارس حُذفت من النظام؛ المفتاح باقٍ لأن القوالب تقرؤه.
        "SCHOOL_LOGO_URL": None,
        "USER_SCHOOLS": [],
    }
    try:
        user_schools: List[School] = []
        if not getattr(user, "is_superuser", False):
            user_schools = list(
                School.objects.filter(
                    memberships__teacher=user,
                    memberships__is_active=True,
                    is_active=True,
                )
                .distinct()
                .order_by("name")
            )
        return {
            "SCHOOL_ID": getattr(active_school, "pk", None),
            "SCHOOL_NAME": getattr(active_school, "name", None),
            "SCHOOL_LOGO_URL": None,
            "USER_SCHOOLS": user_schools,
        }
    except Exception:
        # بلا قائمة مدارس يعجز صاحب العضويات المتعددة عن التبديل بينها.
        _degraded("nav.user_schools", user_id=getattr(user, "pk", None))
        return empty


def nav_context(request: HttpRequest) -> Dict[str, Any]:
    """كل ما يحتاجه الشريط الجانبي والترويسة، لهذا المستخدم في مدرسته النشطة.

    تنسيقٌ فقط: كل قرارٍ في دالته، وهذه تجمع نواتجها. راجع تعليل التفكيك أعلى
    الملف، والعقد المحروس في ``test_nav_context_contract.py``.
    """
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return _anonymous_nav_context()

    # ── كاش قصير العمر يمتصّ ذروة التنقّل بين الصفحات ──
    ttl = soft_call(
        "nav.context_ttl",
        lambda: int(getattr(settings, "NAV_CONTEXT_CACHE_TTL_SECONDS", 20) or 0),
        default=20,
    )
    cache_key = _nav_cache_key(request, user) if ttl > 0 else None
    if cache_key:
        cached = _cache_get(cache_key)
        if isinstance(cached, dict):
            return cached

    active_school = _resolve_active_school(request)

    # ── الأدوار والنطاق ──
    any_school_manager, is_school_manager = _manager_scope(user, active_school)
    officer_depts, member_depts = _department_roles(user, active_school)
    is_officer = bool(officer_depts)
    show_officer_link = bool(getattr(user, "is_superuser", False) or is_officer)

    role_flags = _school_role_flags(user, active_school)
    director_context = _executive_director_context(user)
    capability_flags = _capability_flags(
        user, active_school, is_school_manager=is_school_manager
    )
    if role_flags["IS_LAB_TECHNICIAN"] and active_school is not None:
        from .permissions import supervised_department_ids

        lab_supervised = soft_call(
            "nav.lab_supervised_departments",
            lambda: supervised_department_ids(user, active_school),
            default=set(),
            user_id=getattr(user, "pk", None),
            school_id=getattr(active_school, "pk", None),
        )
        if not lab_supervised:
            for flag_name, code in _NAV_CAPABILITY_FLAGS:
                if code in _NAV_SCOPE_DEPENDENT_CODES:
                    capability_flags[flag_name] = False

    # ── وجهات التقارير ──
    # مدير المدرسة يرى «تقارير المدرسة» لا «تقارير قسمي».
    show_dept_reports_link = bool(
        (show_officer_link or bool(member_depts)) and not is_school_manager
    )

    # ── العدّادات ──
    ticket_agg = _ticket_counters(user, active_school)
    assigned_open = ticket_agg["assigned_open"]

    # ── من يرسل الإشعارات، وإلى أي مسار ──
    can_send_notifications = bool(
        getattr(user, "is_superuser", False)
        or (active_school is not None and (is_officer or is_school_manager))
    )
    send_notification_url = (
        _reverse_any(
            [
                "reports:notifications_create",
                "reports:send_notification",
                "reports:notification_create",
                "reports:announcement_create",
                "reports:admin_message_create",
                "reports:notifications_send",
            ]
        )
        if can_send_notifications
        else None
    )

    out: Dict[str, Any] = {
        "NAV_MY_OPEN_TICKETS": ticket_agg["my_open"],
        "NAV_ASSIGNED_TO_ME": assigned_open,
        "IS_OFFICER": is_officer,
        "OFFICER_DEPARTMENT": officer_depts[0] if officer_depts else None,
        "OFFICER_DEPARTMENTS": officer_depts,
        "SHOW_OFFICER_REPORTS_LINK": show_officer_link,
        "SHOW_DEPARTMENT_REPORTS_LINK": show_dept_reports_link,
        "SHOW_SCHOOL_REPORTS_LINK": bool(is_school_manager and active_school is not None),
        "DEPARTMENT_REPORTS_URLNAME": (
            "reports:officer_reports" if show_officer_link else "reports:department_reports"
        ),
        "NAV_OFFICER_REPORTS": _officer_reports_count(
            user, active_school, officer_depts=officer_depts, is_officer=is_officer
        ),
        # رابط لوحة الإدارة لكل من يحمل is_staff أو يدير مدرسةً ما.
        "SHOW_ADMIN_DASHBOARD_LINK": bool(getattr(user, "is_staff", False)) or any_school_manager,
        "IS_SCHOOL_MANAGER": is_school_manager,
        "SHOW_PERSONAL_ACHIEVEMENT": bool(
            role_flags["HAS_TEACHER_ROLE"] or role_flags["IS_LAB_TECHNICIAN"]
        ),
        # «مهامي المعيّنة» كان مشروطاً بكوْن المستخدم رئيس قسم، والعدّاد يُحسب
        # للجميع: طلبٌ يُحال إلى موظف إداري يُحتسب في القائمة ولا رابط يوصله
        # إليه. والشرط الصحيح وجودُ ما يُعرض لا حملُ دور بعينه.
        "SHOW_ASSIGNED_TO_ME": bool(
            is_officer
            or assigned_open
            or capability_flags["CAN_HANDLE_REQUESTS"]
        ),
        "SHOW_SUPERVISION_GROUP": _shows_supervision_group(
            capability_flags, is_school_manager=is_school_manager
        ),
        "CAN_SEND_NOTIFICATIONS": can_send_notifications,
        "SEND_NOTIFICATION_URL": send_notification_url,
        "USER_ROLE_LABEL": effective_user_role_label(user, active_school=active_school),
        **role_flags,
        **director_context,
        **capability_flags,
        **_archive_visibility(active_school, is_school_manager=is_school_manager),
        **_notification_counters(user, request),
        **_school_switcher(user, active_school),
        **school_gender_template_context(active_school),
    }

    if cache_key:
        _cache_set(cache_key, out, ttl)

    return out



def nav_counters(request: HttpRequest) -> Dict[str, int]:
    ctx = nav_context(request)
    return {
        "NAV_MY_OPEN_TICKETS": int(ctx.get("NAV_MY_OPEN_TICKETS", 0)),
        "NAV_ASSIGNED_TO_ME": int(ctx.get("NAV_ASSIGNED_TO_ME", 0)),
    }


def nav_badges(request: HttpRequest) -> Dict[str, Any]:
    return nav_context(request)


__all__ = ["nav_context", "nav_counters", "nav_badges"]


def csp(request: HttpRequest) -> Dict[str, Any]:
    """Expose CSP nonce to templates.

    The nonce is attached to the request by ContentSecurityPolicyMiddleware.
    """
    try:
        nonce = getattr(request, "csp_nonce", "") or ""
        if not nonce:
            # Fallback safety: ensure templates always receive a nonce value
            # even if middleware ordering or an upstream wrapper skipped setting it.
            import secrets

            nonce = secrets.token_urlsafe(16)
            request.csp_nonce = nonce
        return {"CSP_NONCE": nonce}
    except Exception:
        return {"CSP_NONCE": ""}


__all__.append("csp")


def seo(request: HttpRequest) -> Dict[str, Any]:
    """Expose canonical URLs and the public business identity."""
    site_url = str(getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    if not site_url:
        site_url = request.build_absolute_uri("/").rstrip("/")
    business = {
        "legal_name": str(getattr(settings, "BUSINESS_LEGAL_NAME", "") or "").strip(),
        "commercial_registration": str(
            getattr(settings, "BUSINESS_COMMERCIAL_REGISTRATION", "") or ""
        ).strip(),
        "freelance_document_number": str(
            getattr(settings, "BUSINESS_FREELANCE_DOCUMENT_NUMBER", "") or ""
        ).strip(),
        "freelance_activity": str(
            getattr(settings, "BUSINESS_FREELANCE_ACTIVITY", "") or ""
        ).strip(),
        "freelance_document_expiry": str(
            getattr(settings, "BUSINESS_FREELANCE_DOCUMENT_EXPIRY", "") or ""
        ).strip(),
        "freelance_document_url": str(
            getattr(settings, "BUSINESS_FREELANCE_DOCUMENT_URL", "") or ""
        ).strip(),
        "tax_number": str(getattr(settings, "BUSINESS_TAX_NUMBER", "") or "").strip(),
        "licenses": str(getattr(settings, "BUSINESS_LICENSES", "") or "").strip(),
        "verification_url": str(
            getattr(settings, "BUSINESS_VERIFICATION_URL", "") or ""
        ).strip(),
        "address": str(getattr(settings, "BUSINESS_ADDRESS", "") or "").strip(),
        "support_email": str(
            getattr(settings, "BUSINESS_SUPPORT_EMAIL", "") or ""
        ).strip(),
        "support_phone": str(
            getattr(settings, "BUSINESS_SUPPORT_PHONE", "") or ""
        ).strip(),
    }
    business["disclosure_complete"] = bool(
        business["legal_name"]
        and (
            business["commercial_registration"]
            or business["freelance_document_number"]
        )
        and business["address"]
        and business["support_email"]
        and business["support_phone"]
    )
    return {
        "SITE_URL": site_url,
        "BUSINESS": business,
        "PWA_INSTALL_ENABLED": bool(
            getattr(settings, "PWA_INSTALL_ENABLED", False)
        ),
    }


__all__.append("seo")


# -----------------------------
# بوابات الدفع المفعّلة
# -----------------------------
def payment_gateways(request: HttpRequest) -> Dict[str, Any]:
    """أعلام بوابات الدفع، متاحةً لكل قالب لا لصفحتين اثنتين.

    **لماذا معالج سياق لا متغيّر في كل عرض؟** لأن ذكر البوابة ليس شأن صفحة
    الدفع وحدها: سياسة الخصوصية تُفصح عن معالِج الدفع بوصفه معالجاً للبيانات،
    وسجلّ المدفوعات يعرض حالته، ولوحة المنصة كذلك. وحين كانت الأعلام تُمرَّر
    من عرضين فقط، بقي اسمٌ تجاري ظاهراً في صفحة عامة بلا شرط. فمصدرُ الحقيقة
    واحد، ومن أراد ذكر بوابة سأل عنه.
    """
    from .moyasar_gateway import is_enabled as _moyasar_enabled
    from .tamara_gateway import is_enabled as _tamara_enabled

    # بوّابةٌ تُقرأ «معطّلة» بسبب تعثّر تعني اختفاء وسيلة الدفع من صفحة
    # الاشتراك — عطلٌ تجاري مباشر لا يجوز أن يمرّ صامتاً.
    moyasar_on = bool(soft_call("gateways.moyasar_enabled", _moyasar_enabled, default=False))
    tamara_on = bool(soft_call("gateways.tamara_enabled", _tamara_enabled, default=False))

    names: List[str] = []
    if moyasar_on:
        names.append("ميسر")
    if tamara_on:
        names.append("تمارا")

    return {
        "moyasar_enabled": moyasar_on,
        "tamara_enabled": tamara_on,
        # جاهزة للعرض نصّاً: «ميسر» أو فراغ عند تعطيلها.
        "active_payment_gateway_names": " و".join(names),
        "any_payment_gateway_enabled": bool(names),
    }


__all__.append("payment_gateways")
