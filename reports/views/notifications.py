# reports/views/notifications.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from uuid import uuid4

from django.core.files.base import ContentFile
from django.views.decorators.http import require_GET

from core.observability import report_degraded as _degraded, soft_call, soft_fail
from ..circular_evidence import (
    acknowledgement_digest, document_matches_snapshot, document_snapshot, evidence_digest,
)
from ..handwritten_signature import (
    InvalidHandwrittenSignature, normalize_signature, stored_signature_digest,
)

from ..coverage import pending_documenters
from ..services_notification_idempotency import (
    NotificationSubmissionConflict,
    notification_submission_fingerprint,
    reserve_notification_submission,
    validate_submission_fingerprint,
)

from ._helpers import *
from ._helpers import (
    _is_staff, _is_staff_or_officer, _is_manager_in_school,
    _school_manager_label,
    _get_active_school, _canonical_sender_name, _canonical_role_label,
    effective_user_role_label, _safe_next_url,
)


def _communication_kind(notification) -> str:
    kind = str(getattr(notification, "kind", "") or "").strip()
    if kind in {"notification", "newsletter", "circular"}:
        return kind
    return "circular" if bool(getattr(notification, "requires_signature", False)) else "notification"


def _is_circular(notification) -> bool:
    return _communication_kind(notification) == "circular"


def _is_newsletter(notification) -> bool:
    return _communication_kind(notification) == "newsletter"


def _sent_list_url(notification) -> str:
    if _is_circular(notification):
        return "reports:circulars_sent"
    if _is_newsletter(notification):
        return f"{reverse('reports:notifications_sent')}?kind=newsletter"
    return "reports:notifications_sent"


def _recipient_detail_url_name(notification) -> str:
    return "reports:my_circular_detail" if _is_circular(notification) else "reports:my_notification_detail"


def _communication_label(notification) -> str:
    return {"circular": "التعميم", "newsletter": "النشرة"}.get(
        _communication_kind(notification), "الإشعار"
    )


@login_required(login_url="reports:login")
@access_required(_is_staff_or_officer)
@ratelimit(key="user", rate="10/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def notifications_create(request: HttpRequest, mode: str = "notification") -> HttpResponse:
    if NotificationCreateForm is None:
        messages.error(request, "نموذج إنشاء الإشعار غير متوفر.")
        return redirect("reports:home")

    mode = (mode or "notification").strip().lower()
    if mode not in {"notification", "circular"}:
        mode = "notification"
    is_circular = mode == "circular"
    requested_kind = (
        "circular"
        if is_circular
        else str(request.POST.get("communication_type") or "notification").strip()
    )
    is_newsletter = requested_kind == "newsletter"

    # نربط الإشعارات بمدرسة معيّنة للمدير/الضابط عبر المدرسة النشطة
    active_school = soft_call(
        "notifications.active_school",
        lambda: _get_active_school(request),
        default=None,
        user_id=getattr(request.user, "pk", None),
    )

    is_superuser = bool(getattr(request.user, "is_superuser", False))

    # حماية: مدير المدرسة/الضابط يحتاج مدرسة نشطة.
    if (not is_superuser) and active_school is None:
        messages.error(request, "يرجى اختيار المدرسة أولاً قبل إرسال الإشعارات.")
        return redirect("reports:home")

    # التعميمات: مدير المدرسة ومدير النظام.
    if is_circular:
        if not is_superuser:
            if active_school is None or not _is_manager_in_school(request.user, active_school):
                messages.error(request, f"التعاميم متاحة لـ{_school_manager_label(active_school)} فقط.")
                return redirect("reports:home")

    initial = {}
    if request.method == "GET" and is_circular:
        initial["requires_signature"] = True

    # ── التذكير بالتوثيق: الفعل من حيث رُئي ────────────────────────────────
    # لوحة المدير تعرض من لم يوثّق، ثم تُرسله إلى هنا. وكان الزرّ يفتح نموذجاً
    # فارغاً فيعيد المدير اختيار الخمسة يدوياً من أربعةٍ وأربعين — أي أن اللوحة
    # عرفت الجواب ثم نسيته في الخطوة التالية.
    #
    # **ولماذا لا تُمرَّر المعرّفات في الرابط.** لأن الرابط يُكتب: من مرّر
    # ‎?teachers=1,2,3‎ استهدف من شاء. فيُمرَّر *سببٌ* لا قائمة، ويُعاد الحساب
    # هنا على المدرسة النشطة وحدها — فلا يبلغ المستلمَ إلا من كان أهلاً له.
    reminder_context = None
    if request.method == "GET" and request.GET.get("remind") == "coverage" and active_school is not None:
        pending = list(pending_documenters(active_school)[:200])
        if pending:
            initial["teachers"] = [teacher.pk for teacher in pending]
            initial["title"] = "تذكير بتوثيق الأعمال"
            initial["message"] = (
                "نلفت عنايتكم إلى توثيق أعمالكم في منصة توثيق. "
                "لم يُسجَّل لكم تقرير حتى تاريخه، ونأمل استكمال ذلك."
            )
            reminder_context = {"count": len(pending)}

    form = NotificationCreateForm(
        request.POST or None,
        request.FILES or None,
        user=request.user,
        active_school=active_school,
        initial=initial,
        mode=mode,
        require_submission_key=not is_circular,
    )
    if request.method == "POST":
        if form.is_valid():
            attachment = form.cleaned_data.get("attachment")
            storage_school = active_school or form.cleaned_data.get("target_school")
            capacity_error = archive_storage_capacity_error(
                storage_school,
                [attachment] if attachment and (is_circular or is_newsletter) else [],
            )
            if capacity_error:
                form.add_error("attachment", capacity_error)
                messages.error(request, capacity_error)
                return render(
                    request,
                    "reports/circulars_create.html" if is_circular else "reports/notifications_create.html",
                    {
                        "form": form,
                        "mode": mode,
                        "title": "إنشاء تعميم" if is_circular else "إنشاء تواصل",
                    },
                )
            try:
                existing_notification = None
                reservation_created = True
                with transaction.atomic():
                    if is_circular:
                        form.save(
                            creator=request.user,
                            default_school=active_school,
                            force_requires_signature=True,
                        )
                    else:
                        payload_fingerprint, submission_school = (
                            notification_submission_fingerprint(
                                cleaned_data=form.cleaned_data,
                                sender=request.user,
                                default_school=active_school,
                                mode=mode,
                            )
                        )
                        submission, reservation_created = reserve_notification_submission(
                            sender=request.user,
                            submission_key=form.cleaned_data["submission_key"],
                            school=submission_school,
                            payload_fingerprint=payload_fingerprint,
                        )
                        if reservation_created:
                            notification = form.save(
                                creator=request.user,
                                default_school=active_school,
                                dispatch_realtime_on_commit=True,
                            )
                            submission.notification = notification
                            submission.save(update_fields=["notification"])
                        else:
                            validate_submission_fingerprint(submission, payload_fingerprint)
                            existing_notification = submission.notification

                sent_label = "التعميم" if is_circular else ("النشرة" if is_newsletter else "الإشعار")
                if not is_circular and not reservation_created:
                    messages.success(request, f"تم إرسال {sent_label} مسبقًا، ولم يُكرر الإرسال.")
                    if existing_notification is not None:
                        return redirect(_sent_list_url(existing_notification))
                    return redirect("reports:notifications_sent")
                messages.success(request, f"تم إرسال {sent_label} إلى المستلمين المحددين.")
                if is_circular:
                    return redirect("reports:circulars_sent")
                if is_newsletter:
                    return redirect(f"{reverse('reports:notifications_sent')}?kind=newsletter")
                return redirect("reports:notifications_sent")
            except NotificationSubmissionConflict:
                conflict_message = (
                    "تعذّر إعادة استخدام طلب الإرسال. أعد فتح نموذج الإرسال وحاول مرة أخرى."
                )
                form.add_error(None, conflict_message)
                messages.error(request, conflict_message)
            except Exception:
                logger.exception("notifications_create failed")
                messages.error(request, "تعذّر الإرسال. جرّب لاحقًا.")
        else:
            messages.error(request, "الرجاء تصحيح الأخطاء.")

    return render(
        request,
        "reports/circulars_create.html" if is_circular else "reports/notifications_create.html",
        {
            "form": form,
            "mode": mode,
            "title": "إنشاء تعميم" if is_circular else "إنشاء تواصل",
            "reminder_context": reminder_context,
        },
    )

@login_required(login_url="reports:login")
@access_required(_is_staff_or_officer)
@require_http_methods(["POST"])
def notification_delete(request: HttpRequest, pk: int) -> HttpResponse:
    if Notification is None:
        messages.error(request, "نموذج الإشعار غير متاح.")
        return redirect("reports:notifications_sent")

    active_school = _get_active_school(request)
    is_superuser = bool(getattr(request.user, "is_superuser", False))
    if (not is_superuser) and active_school is None:
        messages.error(request, "يرجى اختيار المدرسة أولاً.")
        return redirect("reports:home")

    n = get_object_or_404(Notification, pk=pk)
    sent_list_url = _sent_list_url(n)

    # التعميمات: سماح لمدير المدرسة/مدير النظام
    if _is_circular(n):
        if not is_superuser and not _is_manager_in_school(request.user, active_school):
            messages.error(request, "لا تملك صلاحية التعامل مع التعاميم.")
            return redirect(sent_list_url)
    is_owner = getattr(n, "created_by_id", None) == request.user.id
    is_manager = _is_manager_in_school(request.user, active_school)
    if not (is_manager or is_owner):
        messages.error(request, "لا تملك صلاحية حذف هذا الإشعار.")
        return redirect(sent_list_url)

    # عزل حسب المدرسة النشطة (غير السوبر)
    if (not is_superuser) and hasattr(n, "school_id"):
        if getattr(n, "school_id", None) is None:
            messages.error(request, "لا تملك صلاحية حذف إشعار عام.")
            return redirect(sent_list_url)
        if getattr(n, "school_id", None) != getattr(active_school, "id", None):
            messages.error(request, "لا تملك صلاحية حذف إشعار من مدرسة أخرى.")
            return redirect(sent_list_url)
    try:
        n.delete()
        messages.success(request, "تم حذف الإشعار.")
    except Exception:
        logger.exception("notification_delete failed")
        messages.error(request, "تعذّر حذف الإشعار.")
    return redirect(sent_list_url)

def _resync_notification_clients(user) -> None:
    """يُبطل كاش العدّادات ويطلب من المتصفحات إعادة المزامنة بعد تحديث جماعي.

    ``update`` الجماعي لا يُطلق الإشارات، فالعدّاد المخزَّن والعميل المفتوح
    يبقيان على الرقم القديم. وتعثّر أيٍّ من الخطوتين لا يُبطل العملية — الصفوف
    حُدِّثت فعلاً — لكنه يترك المستخدم أمام عدّادٍ لا يتحرّك، فيُسجَّل.
    """
    teacher_id = int(getattr(user, "id", 0) or 0)
    if not teacher_id:
        return

    with soft_fail("notifications.invalidate_counter_cache", user_id=teacher_id):
        from ..cache_utils import invalidate_user_notifications

        invalidate_user_notifications(teacher_id)

    with soft_fail("notifications.push_force_resync", user_id=teacher_id):
        from ..realtime_notifications import push_force_resync

        push_force_resync(teacher_id=teacher_id)


def _recipient_is_read(rec) -> tuple[bool, str | None]:
    for flag in ("is_read", "read", "seen", "opened"):
        if hasattr(rec, flag):
            with soft_fail("notifications.recipient_read_flag", field=flag):
                return (bool(getattr(rec, flag)), None)
    for dt in ("read_at", "seen_at", "opened_at"):
        if hasattr(rec, dt):
            with soft_fail("notifications.recipient_read_at", field=dt):
                val = getattr(rec, dt)
                return (bool(val), getattr(val, "strftime", lambda fmt: None)("%Y-%m-%d %H:%M") if val else None)
    if hasattr(rec, "status"):
        with soft_fail("notifications.recipient_status"):
            st = str(rec.status or "").lower()
            if st in {"read", "seen", "opened", "done"}:
                return (True, None)
    return (False, None)

def _digits_only(val: str) -> str:
    return "".join(ch for ch in str(val or "") if ch.isdigit())


def _phone_key(val: str) -> str:
    """Normalize phone for comparison.

    We compare by the last 9 digits to support common Saudi formats:
    - 05xxxxxxxx
    - 5xxxxxxxx
    - 9665xxxxxxxx
    """
    d = _digits_only(val)
    if len(d) >= 9:
        return d[-9:]
    return d


def _mask_phone(val: str) -> str:
    d = _digits_only(val)
    if not d:
        return ""
    if len(d) <= 4:
        return "*" * len(d)
    return ("*" * (len(d) - 4)) + d[-4:]

@login_required(login_url="reports:login")
@access_required(_is_staff_or_officer)
@require_http_methods(["GET"])
def notification_detail(request: HttpRequest, pk: int) -> HttpResponse:
    if Notification is None:
        messages.error(request, "نموذج الإشعار غير متاح.")
        return redirect("reports:notifications_sent")

    active_school = _get_active_school(request)
    is_superuser = bool(getattr(request.user, "is_superuser", False))
    if (not is_superuser) and active_school is None:
        messages.error(request, "يرجى اختيار المدرسة أولاً.")
        return redirect("reports:home")

    n = get_object_or_404(Notification, pk=pk)
    sent_list_url = _sent_list_url(n)

    # التعميمات: سماح لمدير المدرسة/مدير النظام
    if _is_circular(n):
        if (not is_superuser) and (not _is_manager_in_school(request.user, active_school)):
            messages.error(request, "لا تملك صلاحية عرض التعاميم.")
            return redirect(sent_list_url)

    # عزل حسب المدرسة النشطة (غير السوبر)
    if (not is_superuser) and hasattr(n, "school_id"):
        if getattr(n, "school_id", None) is None:
            messages.error(request, "لا تملك صلاحية عرض إشعار عام.")
            return redirect(sent_list_url)
        if getattr(n, "school_id", None) != getattr(active_school, "id", None):
            messages.error(request, "لا تملك صلاحية عرض إشعار من مدرسة أخرى.")
            return redirect(sent_list_url)

    if not _is_manager_in_school(request.user, active_school):
        if getattr(n, "created_by_id", None) != request.user.id:
            messages.error(request, "لا تملك صلاحية عرض هذا الإشعار.")
            return redirect(sent_list_url)

    body = (
        getattr(n, "message", None) or getattr(n, "body", None) or
        getattr(n, "content", None) or getattr(n, "text", None) or
        getattr(n, "details", None) or ""
    )

    recipients = []
    sig_total = 0
    sig_signed = 0
    sig_read = 0
    recipient_read = 0
    if NotificationRecipient is not None:
        # اكتشف اسم FK للإشعار
        notif_fk = None
        for f in NotificationRecipient._meta.get_fields():
            if getattr(getattr(f, "remote_field", None), "model", None) is Notification:
                notif_fk = f.name
                break

        # اسم حقل الشخص
        user_fk = None
        for cand in ("teacher", "user", "recipient"):
            if hasattr(NotificationRecipient, cand):
                user_fk = cand
                break

        if notif_fk:
            qs = NotificationRecipient.objects.filter(**{f"{notif_fk}": n})
            if user_fk:
                qs = qs.select_related(f"{user_fk}", "added_by")
            qs = qs.order_by("id")

            # Batch-prefetch SchoolMembership to avoid N+1 in effective_user_role_label
            recipients_list = list(qs)
            if user_fk and active_school:
                _teachers = [getattr(r, user_fk) for r in recipients_list if getattr(r, user_fk, None)]
                if _teachers:
                    from ..permissions import prefetch_memberships_for_school
                    prefetch_memberships_for_school(_teachers, active_school)

            for r in recipients_list:
                t = getattr(r, user_fk) if user_fk else None
                if not t:
                    continue
                name = getattr(t, "name", None) or getattr(t, "phone", None) or getattr(t, "username", None) or f"مستخدم #{getattr(t, 'pk', '')}"
                role_label = effective_user_role_label(t, active_school=active_school)
                is_read, read_at_str = _recipient_is_read(r)
                if is_read:
                    recipient_read += 1

                signed = bool(getattr(r, "is_signed", False))
                signed_at_str = None
                try:
                    v = getattr(r, "signed_at", None)
                    signed_at_str = v.strftime("%Y-%m-%d %H:%M") if v else None
                except Exception:
                    signed_at_str = None

                if bool(getattr(n, "requires_signature", False)):
                    sig_total += 1
                    if signed:
                        sig_signed += 1
                    if is_read:
                        sig_read += 1

                recipients.append({
                    "name": str(name),
                    "role": role_label,
                    "read": bool(is_read),
                    "read_at": read_at_str,
                    "signed": signed,
                    "signed_at": signed_at_str,
                    "added_later": (
                        getattr(r, "delivery_source", "")
                        == NotificationRecipient.DeliverySource.MANUAL_ADDITION
                    ),
                    "added_at": getattr(r, "created_at", None),
                    "added_by": getattr(getattr(r, "added_by", None), "name", ""),
                })

    eligible_new_recipients = []
    can_add_recipients = bool(
        getattr(n, "requires_signature", False)
        and active_school is not None
        and getattr(n, "school_id", None) == getattr(active_school, "pk", None)
        and _is_manager_in_school(request.user, active_school)
    )
    if can_add_recipients:
        existing_teacher_ids = NotificationRecipient.objects.filter(
            notification=n
        ).values_list("teacher_id", flat=True)
        eligible_new_recipients = list(
            Teacher.objects.filter(
                is_active=True,
                school_memberships__school=active_school,
                school_memberships__is_active=True,
                school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
            )
            .exclude(pk__in=existing_teacher_ids)
            .distinct()
            .order_by("name", "pk")
        )
        if eligible_new_recipients:
            from ..permissions import prefetch_memberships_for_school

            prefetch_memberships_for_school(eligible_new_recipients, active_school)
            for teacher in eligible_new_recipients:
                teacher.recipient_role_label = effective_user_role_label(
                    teacher, active_school=active_school
                )

    # اسم اليوم بالعربية لتاريخ الإرسال (بتوقيت محلي)
    created_day_name = ""
    try:
        if getattr(n, "created_at", None):
            _days = {1: "الاثنين", 2: "الثلاثاء", 3: "الأربعاء", 4: "الخميس",
                     5: "الجمعة", 6: "السبت", 7: "الأحد"}
            created_day_name = _days.get(timezone.localtime(n.created_at).isoweekday(), "")
    except Exception:
        created_day_name = ""

    ctx = {
        "n": n,
        "now": timezone.now(),
        "body": body,
        "recipients": recipients,
        "created_day_name": created_day_name,
        "signature_stats": {
            "total": int(sig_total),
            "signed": int(sig_signed),
            "unsigned": int(max(sig_total - sig_signed, 0)),
            "read": int(sig_read),
            "unread": int(max(sig_total - sig_read, 0)),
            "signed_percentage": int(round((sig_signed / sig_total) * 100)) if sig_total else 0,
            "read_percentage": int(round((sig_read / sig_total) * 100)) if sig_total else 0,
        },
        "recipient_stats": {
            "total": len(recipients),
            "read": int(recipient_read),
            "unread": int(max(len(recipients) - recipient_read, 0)),
            "read_percentage": (
                int(round((recipient_read / len(recipients)) * 100)) if recipients else 0
            ),
        },
        "can_add_recipients": can_add_recipients,
        "eligible_new_recipients": eligible_new_recipients,
        "communication_kind": _communication_kind(n),
        "communication_label": _communication_label(n),
        "is_newsletter": _is_newsletter(n),
    }
    template_name = "reports/circular_detail.html" if _is_circular(n) else "reports/notification_detail.html"
    return render(request, template_name, ctx)


@login_required(login_url="reports:login")
@role_required({"manager"})
@ratelimit(key="user", rate="20/h", method="POST", block=True)
@require_http_methods(["POST"])
def circular_recipients_add(request: HttpRequest, pk: int) -> HttpResponse:
    """Append active school staff to a circular without rewriting its original audience."""
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "اختر المدرسة أولاً.")
        return redirect("reports:select_school")

    circular = get_object_or_404(
        Notification.objects.select_related("school"),
        pk=pk,
        school=active_school,
        requires_signature=True,
    )
    submitted_values = request.POST.getlist("teacher_ids")
    try:
        submitted_ids = {int(value) for value in submitted_values if str(value).strip()}
    except (TypeError, ValueError):
        submitted_ids = set()

    if not submitted_ids:
        messages.error(request, "اختر مستلمًا واحدًا على الأقل لإلحاقه بالتعميم.")
        return redirect("reports:notification_detail", pk=circular.pk)
    if len(submitted_ids) > 200:
        messages.error(request, "يمكن إلحاق 200 مستلم كحد أقصى في العملية الواحدة.")
        return redirect("reports:notification_detail", pk=circular.pk)

    eligible_ids = set(
        Teacher.objects.filter(
            pk__in=submitted_ids,
            is_active=True,
            school_memberships__school=active_school,
            school_memberships__is_active=True,
            school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
        )
        .distinct()
        .values_list("pk", flat=True)
    )
    if eligible_ids != submitted_ids:
        messages.error(request, "تعذر الإلحاق: تتضمن القائمة حسابًا غير نشط أو خارج المدرسة.")
        return redirect("reports:notification_detail", pk=circular.pk)

    with transaction.atomic():
        # Serialise append operations for the same document.  Without this
        # lock, two manager tabs can both report the same recipient as newly
        # added even though the uniqueness constraint persists only one row.
        circular = Notification.objects.select_for_update().get(pk=circular.pk)
        existing_ids = set(
            NotificationRecipient.objects.filter(
                notification=circular,
                teacher_id__in=submitted_ids,
            ).values_list("teacher_id", flat=True)
        )
        new_ids = sorted(submitted_ids - existing_ids)
        if new_ids:
            NotificationRecipient.objects.bulk_create(
                [
                    NotificationRecipient(
                        notification=circular,
                        teacher_id=teacher_id,
                        delivery_source=NotificationRecipient.DeliverySource.MANUAL_ADDITION,
                        added_by=request.user,
                    )
                    for teacher_id in new_ids
                ]
            )
            AuditLog.objects.create(
                school=active_school,
                teacher=request.user,
                actor_name=(request.user.name or "")[:150],
                actor_role=effective_user_role_label(request.user, active_school)[:64],
                action=AuditLog.Action.UPDATE,
                model_name="Notification",
                object_id=circular.pk,
                object_repr=str(circular)[:255],
                changes={
                    "action": "append_circular_recipients",
                    "teacher_ids": new_ids,
                    "added_count": len(new_ids),
                },
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
            )

    if not new_ids:
        messages.info(request, "المستلمون المحددون موجودون في سجل التعميم بالفعل.")
        return redirect("reports:notification_detail", pk=circular.pk)

    with soft_fail("circulars.append_recipients.delivery", notification_id=circular.pk):
        from ..cache_utils import invalidate_user_notifications
        from ..realtime_notifications import push_new_notification_to_teachers

        for teacher_id in new_ids:
            invalidate_user_notifications(teacher_id)
        push_new_notification_to_teachers(
            notification=circular,
            teacher_ids=new_ids,
        )

    messages.success(
        request,
        f"تم إلحاق {len(new_ids)} من المستلمين بالتعميم وتوثيق العملية في سجل المدرسة.",
    )
    return redirect("reports:notification_detail", pk=circular.pk)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def notification_sign(request: HttpRequest, pk: int) -> HttpResponse:
    """Record a recipient's drawn signature against an immutable document."""
    if NotificationRecipient is None:
        messages.error(request, "نظام الإشعارات غير متاح حالياً.")
        return redirect(_safe_next_url(request.POST.get("next")) or "reports:my_notifications")

    rec = get_object_or_404(
        NotificationRecipient.objects.select_related(
            "notification",
            "notification__created_by",
        ),
        pk=pk,
        teacher=request.user,
    )

    n = getattr(rec, "notification", None)
    if n is None:
        messages.error(request, "تعذّر العثور على الوثيقة.")
        return redirect("reports:my_circulars")

    label = _communication_label(n)
    detail_url_name = _recipient_detail_url_name(n)

    if not bool(getattr(n, "requires_signature", False)):
        messages.error(request, "هذه الوثيقة لا تتطلب إقرارًا.")
        return redirect("reports:my_notification_detail", pk=rec.pk)

    if bool(getattr(rec, "is_signed", False)):
        messages.info(request, f"تم تسجيل إقرارك مسبقًا على {label}.")
        return redirect(detail_url_name, pk=rec.pk)

    if n.expires_at and timezone.now() > n.expires_at:
        messages.error(request, f"انتهت صلاحية {label}، ولم يعد الإقرار متاحًا.")
        return redirect(detail_url_name, pk=rec.pk)

    # ✅ منع التوقيع بعد انتهاء آخر موعد للتوقيع (إن حُدّد)
    deadline = getattr(n, "signature_deadline_at", None)
    if deadline and timezone.now() > deadline:
        messages.error(request, f"انتهى آخر موعد للإقرار على {label}.")
        return redirect(detail_url_name, pk=rec.pk)

    window = timedelta(minutes=15)

    ack = request.POST.get("ack") in {"1", "on", "true", "yes"}

    # Serialize attempts per recipient; a failed counter write stops the request.
    try:
        with transaction.atomic():
            attempt_rec = NotificationRecipient.objects.select_for_update().get(
                pk=rec.pk, teacher=request.user,
            )
            if attempt_rec.is_signed:
                messages.info(request, f"تم تسجيل إقرارك مسبقًا على {label}.")
                return redirect(detail_url_name, pk=rec.pk)
            now = timezone.now()
            last_attempt = attempt_rec.signature_last_attempt_at
            attempts = attempt_rec.signature_attempt_count or 0
            if last_attempt and now - last_attempt > window:
                attempts = 0
            # Keep an operational count, without the phone-guessing lockout:
            # there is no phone secret to brute-force in the drawing flow.
            attempt_rec.signature_attempt_count = min(attempts + 1, 65535)
            attempt_rec.signature_last_attempt_at = now
            attempt_rec.save(update_fields=["signature_attempt_count", "signature_last_attempt_at"])
    except Exception:
        logger.exception("notification signature attempt counter failed")
        messages.error(request, "تعذّر التحقق من المحاولة حاليًا. جرّب لاحقًا.")
        return redirect(detail_url_name, pk=rec.pk)

    if not ack:
        messages.error(request, "يلزم الموافقة على نص الإقرار قبل اعتماده.")
        return redirect(detail_url_name, pk=rec.pk)

    try:
        signature_png = normalize_signature(request.POST.get("signature_data") or "")
    except InvalidHandwrittenSignature as exc:
        messages.error(request, str(exc))
        return redirect(detail_url_name, pk=rec.pk)

    # Capture exactly which document and declaration were acknowledged. Legacy
    # documents are captured at first sign; their issue-time contents cannot be
    # reconstructed, so the receipt explicitly identifies that limitation.
    saved_image_name = ""
    saved_image_storage = None
    try:
        with transaction.atomic():
            locked_notification = Notification.objects.select_for_update().get(pk=n.pk)
            rec = NotificationRecipient.objects.select_for_update().get(pk=rec.pk, teacher=request.user)
            if rec.is_signed:
                messages.info(request, f"تم تسجيل إقرارك مسبقًا على {label}.")
                return redirect(detail_url_name, pk=rec.pk)
            if locked_notification.expires_at and timezone.now() > locked_notification.expires_at:
                messages.error(request, f"انتهت صلاحية {label}، ولم يعد الإقرار متاحًا.")
                return redirect(detail_url_name, pk=rec.pk)
            if locked_notification.signature_deadline_at and timezone.now() > locked_notification.signature_deadline_at:
                messages.error(request, f"انتهى آخر موعد للإقرار على {label}.")
                return redirect(detail_url_name, pk=rec.pk)
            if not locked_notification.issued_digest:
                snapshot = document_snapshot(locked_notification, basis="at_first_sign")
                locked_notification.issued_snapshot = snapshot
                locked_notification.issued_digest = evidence_digest(snapshot)
                Notification.objects.filter(pk=n.pk, issued_digest="").update(
                    issued_snapshot=snapshot, issued_digest=locked_notification.issued_digest,
                )
            if not document_matches_snapshot(locked_notification):
                messages.error(request, "تغيّر محتوى الوثيقة أو مرفقها؛ أبلغ الإدارة قبل اعتماد الإقرار.")
                return redirect(detail_url_name, pk=rec.pk)
            rec.signed_document_digest = locked_notification.issued_digest
            rec.signed_ack_text = locked_notification.issued_snapshot.get("ack_text", "")
            rec.signature_method = "drawn_ack"
            rec.signature_image.save(f"{rec.pk}/{uuid4().hex}.png", ContentFile(signature_png), save=False)
            saved_image_name = rec.signature_image.name
            saved_image_storage = rec.signature_image.storage
            rec.signature_image_sha256 = stored_signature_digest(rec.signature_image)
            rec.signed_at = timezone.now()
            rec.signature_evidence_digest = acknowledgement_digest(rec, rec.signed_at)
            rec.is_signed = True
            rec.is_read = True
            if rec.read_at is None:
                rec.read_at = rec.signed_at
            rec.save(update_fields=[
                "is_signed", "signed_at", "is_read", "read_at", "signed_document_digest",
                "signed_ack_text", "signature_method", "signature_evidence_digest",
                "signature_image", "signature_image_sha256",
            ])
    except Exception:
        if saved_image_name and saved_image_storage:
            try:
                saved_image_storage.delete(saved_image_name)
            except Exception:
                logger.exception("failed to remove uncommitted signature image")
        logger.exception("notification_sign failed")
        messages.error(request, "تعذّر تسجيل الإقرار. جرّب لاحقًا.")
        return redirect(detail_url_name, pk=rec.pk)

    messages.success(request, f"تم تسجيل إقرارك على {label}.")
    return redirect(detail_url_name, pk=rec.pk)


@login_required(login_url="reports:login")
@never_cache
@require_http_methods(["GET"])
def circular_receipt(request: HttpRequest, pk: int) -> HttpResponse:
    """Private, printable acknowledgement receipt for its recipient."""
    rec = get_object_or_404(
        NotificationRecipient.objects.select_related("notification", "teacher"),
        pk=pk,
        teacher=request.user,
        is_signed=True,
    )
    notification = rec.notification
    snapshot = notification.issued_snapshot or {}
    evidence_available = bool(
        rec.signature_evidence_digest and rec.signed_document_digest and snapshot
    )
    evidence_valid = False
    if evidence_available and rec.signed_at:
        try:
            evidence_valid = (
                document_matches_snapshot(notification)
                and rec.signed_document_digest == notification.issued_digest
                and acknowledgement_digest(rec, rec.signed_at) == rec.signature_evidence_digest
                and (
                    not rec.signature_image_sha256
                    or (
                        bool(rec.signature_image)
                        and stored_signature_digest(rec.signature_image) == rec.signature_image_sha256
                    )
                )
            )
        except Exception:
            logger.exception("circular receipt verification failed for recipient %s", rec.pk)
    return render(request, "reports/circular_receipt.html", {
        "r": rec,
        "n": notification,
        "snapshot": snapshot,
        "evidence_available": evidence_available,
        "evidence_valid": evidence_valid,
        "communication_label": _communication_label(notification),
    })


@login_required(login_url="reports:login")
@never_cache
@require_GET
def circular_signature_image(request: HttpRequest, pk: int) -> HttpResponse:
    """Serve a private signature only to its recipient or report viewer."""
    rec = get_object_or_404(
        NotificationRecipient.objects.select_related("notification"),
        pk=pk, is_signed=True,
    )
    if not rec.signature_image or not rec.signature_image_sha256:
        raise Http404
    if rec.teacher_id != request.user.pk:
        notification = rec.notification
        active_school = _get_active_school(request)
        if not _is_staff_or_officer(request.user):
            raise Http404
        if not request.user.is_superuser and (
            active_school is None or notification.school_id != active_school.pk
        ):
            raise Http404
        if not (
            _is_manager_in_school(request.user, active_school)
            or notification.created_by_id == request.user.pk
        ):
            raise Http404
    try:
        with rec.signature_image.storage.open(rec.signature_image.name, "rb") as source:
            image = source.read(256 * 1024 + 1)
    except Exception:
        logger.exception("could not read signature image for recipient %s", rec.pk)
        raise Http404 from None
    if len(image) > 256 * 1024 or not image:
        raise Http404
    from hashlib import sha256
    if sha256(image).hexdigest() != rec.signature_image_sha256:
        logger.error("signature image integrity check failed for recipient %s", rec.pk)
        raise Http404
    response = HttpResponse(image, content_type="image/png")
    response["Content-Disposition"] = "inline"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required(login_url="reports:login")
@access_required(_is_staff_or_officer)
@require_http_methods(["GET"])
def notification_signatures_print(request: HttpRequest, pk: int) -> HttpResponse:
    if Notification is None or NotificationRecipient is None:
        messages.error(request, "نظام الإشعارات غير متاح.")
        return redirect("reports:notifications_sent")

    active_school = _get_active_school(request)
    is_superuser = bool(getattr(request.user, "is_superuser", False))
    if (not is_superuser) and active_school is None:
        messages.error(request, "يرجى اختيار المدرسة أولاً.")
        return redirect("reports:home")

    n = get_object_or_404(Notification, pk=pk)
    sent_list_url = _sent_list_url(n)

    # التقرير متاح للتعميم أو النشرة متى كان التوقيع مطلوبًا.
    if not bool(getattr(n, "requires_signature", False)):
        messages.error(request, "تقرير التواقيع متاح للوثائق التي تطلب توقيعًا فقط.")
        return redirect(sent_list_url)

    # Permission: manager in school or creator
    if not _is_manager_in_school(request.user, active_school):
        if getattr(n, "created_by_id", None) != request.user.id:
            messages.error(request, "لا تملك صلاحية عرض تقرير هذه الوثيقة.")
            return redirect(sent_list_url)

    # School isolation
    if (not is_superuser) and hasattr(n, "school_id"):
        if getattr(n, "school_id", None) != getattr(active_school, "id", None):
            messages.error(request, "لا تملك صلاحية عرض وثيقة من مدرسة أخرى.")
            return redirect(sent_list_url)

    qs = (
        NotificationRecipient.objects
        .filter(notification=n)
        .select_related("teacher")
        .order_by("teacher__name", "id")
    )

    # Batch-prefetch memberships to avoid N+1 in effective_user_role_label
    recipients_list = list(qs)
    if active_school:
        _teachers = [r.teacher for r in recipients_list if getattr(r, "teacher", None)]
        if _teachers:
            from ..permissions import prefetch_memberships_for_school
            prefetch_memberships_for_school(_teachers, active_school)

    rows = []
    signed = 0
    total = 0
    for r in recipients_list:
        t = getattr(r, "teacher", None)
        if not t:
            continue
        total += 1
        is_signed = bool(getattr(r, "is_signed", False))
        if is_signed:
            signed += 1
        rows.append({
            "recipient_id": r.pk,
            "name": getattr(t, "name", "") or str(t),
            "role": effective_user_role_label(t, active_school=active_school),
            "phone": _mask_phone(getattr(t, "phone", "")),
            "read": bool(getattr(r, "is_read", False)),
            "read_at": getattr(r, "read_at", None),
            "signed": is_signed,
            "signed_at": getattr(r, "signed_at", None),
            "evidence_ref": r.signature_evidence_digest[:12] if r.signature_evidence_digest else "",
            "has_drawn_signature": bool(r.signature_image and r.signature_image_sha256),
        })

    document_verified = False
    if n.issued_digest:
        try:
            document_verified = document_matches_snapshot(n)
        except Exception:
            logger.exception("notification signature report document verification failed for %s", n.pk)

    ctx = {
        "n": n,
        "issued_snapshot": n.issued_snapshot or {},
        "document_verified": document_verified,
        "communication_label": _communication_label(n),
        "communication_kind": _communication_kind(n),
        "rows": rows,
        "stats": {
            "total": total,
            "signed": signed,
            "unsigned": max(total - signed, 0),
            "signed_percentage": int(round((signed / total) * 100)) if total else 0,
        },
    }
    return render(request, "reports/notification_signatures_print.html", ctx)


@login_required(login_url="reports:login")
@access_required(_is_staff_or_officer)
@require_http_methods(["GET"])
def notification_signatures_csv(request: HttpRequest, pk: int) -> HttpResponse:
    if Notification is None or NotificationRecipient is None:
        return HttpResponse("unavailable", status=400)

    active_school = _get_active_school(request)
    is_superuser = bool(getattr(request.user, "is_superuser", False))
    if (not is_superuser) and active_school is None:
        return HttpResponse("active_school_required", status=403)

    n = get_object_or_404(Notification, pk=pk)

    if not bool(getattr(n, "requires_signature", False)):
        return HttpResponse("forbidden", status=403)

    if not _is_manager_in_school(request.user, active_school):
        if getattr(n, "created_by_id", None) != request.user.id:
            return HttpResponse("forbidden", status=403)

    if (not is_superuser) and hasattr(n, "school_id"):
        if getattr(n, "school_id", None) != getattr(active_school, "id", None):
            return HttpResponse("forbidden", status=403)

    import csv
    from io import StringIO

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "الاسم",
        "الدور",
        "الجوال (مخفي)",
        "الحالة (مقروء)",
        "وقت القراءة",
        "الحالة (موقّع)",
        "وقت التوقيع",
        "بصمة نسخة الوثيقة SHA-256",
        "بصمة سجل الإقرار SHA-256",
        "بصمة التوقيع المرسوم SHA-256",
        "طريقة الاعتماد",
        "نص الإقرار المعتمد",
    ])

    qs = (
        NotificationRecipient.objects
        .filter(notification=n)
        .select_related("teacher")
        .order_by("teacher__name", "id")
    )

    # Batch-prefetch memberships to avoid N+1
    recipients_list = list(qs)
    if active_school:
        _teachers = [r.teacher for r in recipients_list if getattr(r, "teacher", None)]
        if _teachers:
            from ..permissions import prefetch_memberships_for_school
            prefetch_memberships_for_school(_teachers, active_school)

    for r in recipients_list:
        t = getattr(r, "teacher", None)
        if not t:
            continue
        role_label = effective_user_role_label(t, active_school=active_school)
        writer.writerow([
            getattr(t, "name", "") or str(t),
            role_label,
            _mask_phone(getattr(t, "phone", "")),
            "نعم" if bool(getattr(r, "is_read", False)) else "لا",
            getattr(getattr(r, "read_at", None), "strftime", lambda fmt: "")("%Y-%m-%d %H:%M") if getattr(r, "read_at", None) else "",
            "نعم" if bool(getattr(r, "is_signed", False)) else "لا",
            getattr(getattr(r, "signed_at", None), "strftime", lambda fmt: "")("%Y-%m-%d %H:%M") if getattr(r, "signed_at", None) else "",
            r.signed_document_digest,
            r.signature_evidence_digest,
            r.signature_image_sha256,
            r.signature_method,
            r.signed_ack_text,
        ])

    resp = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
    safe_title = (getattr(n, "title", "") or "notification").strip().replace("\n", " ").replace("\r", " ")
    resp["Content-Disposition"] = f'attachment; filename="signatures_{pk}_{safe_title[:40]}.csv"'
    return resp

@require_http_methods(["GET"])
def unread_notifications_count(request: HttpRequest) -> HttpResponse:
    """إرجاع عدد الإشعارات غير المقروءة بتنسيق JSON لاستخدامه في الـ Polling.

    ملاحظة: لا نُعيد توجيه المستخدمين غير المسجلين لصفحة الدخول لأن هذا المسار يُستدعى بشكل دوري
    من الواجهة (Polling)، وإعادة التوجيه قد تسبب ضغطاً وتداخل مع RateLimit.
    """
    if not getattr(request.user, "is_authenticated", False):
        return JsonResponse({"count": 0, "authenticated": False})

    if NotificationRecipient is None:
        return JsonResponse({"count": 0, "unread": 0, "signatures_pending": 0, "authenticated": True})

    # Short-TTL cache per user + school to cut repeated aggregate queries.
    try:
        ttl = int(getattr(settings, "UNREAD_COUNT_CACHE_TTL_SECONDS", 15) or 0)
    except Exception:
        ttl = 15

    cache_key = None
    if ttl > 0:
        try:
            sid_raw = request.session.get("active_school_id")
            sid_for_key = str(int(sid_raw)) if sid_raw else "none"
        except Exception:
            sid_for_key = "none"
        try:
            uid = int(getattr(request.user, "id", 0) or 0)
            cache_key = f"unreadcnt:v2:u{uid}:s{sid_for_key}"
            cached = cache.get(cache_key)
            if isinstance(cached, dict):
                return JsonResponse(cached)
        except Exception:
            cache_key = None

    active_school = _get_active_school(request)
    now = timezone.now()

    qs = NotificationRecipient.objects.filter(teacher=request.user)

    # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL)
    if active_school is not None:
        qs = qs.filter(Q(notification__school=active_school) | Q(notification__school__isnull=True))
    else:
        qs = qs.filter(notification__school__isnull=True)

    # استبعاد المنتهي
    qs = qs.filter(Q(notification__expires_at__gt=now) | Q(notification__expires_at__isnull=True))

    # Bell attention: unread notifications/newsletters, plus a signed newsletter
    # that was opened but still awaits the recipient's signature.
    unread_q = Q(notification__kind__in=["notification", "newsletter"]) & (
        Q(is_read=False)
        | Q(
            notification__kind="newsletter",
            notification__requires_signature=True,
            is_signed=False,
        )
    )

    # The circular badge remains exclusive to actual circulars.
    pending_sig_q = Q(
        notification__kind="circular",
        notification__requires_signature=True,
        is_signed=False,
    ) & (Q(notification__signature_deadline_at__gte=now)
         | Q(notification__signature_deadline_at__isnull=True))

    # count = items needing attention (backward compatible): unread notifications OR pending circular signatures
    attention_q = unread_q | pending_sig_q

    agg = qs.aggregate(
        count=Count("id", filter=attention_q),
        unread=Count("id", filter=unread_q),
        signatures_pending=Count("id", filter=pending_sig_q),
    )

    payload = {
        "count": int(agg.get("count") or 0),
        "unread": int(agg.get("unread") or 0),
        "signatures_pending": int(agg.get("signatures_pending") or 0),
        "authenticated": True,
    }

    if cache_key and ttl > 0:
        soft_call(
            "notifications.badge_cache_set",
            lambda: cache.set(cache_key, payload, ttl),
            default=None,
        )

    return JsonResponse(payload)

@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def my_notifications(request: HttpRequest) -> HttpResponse:
    if NotificationRecipient is None:
        return render(request, "reports/my_notifications.html", {"page_obj": Paginator([], 12).get_page(1)})

    active_school = _get_active_school(request)

    qs = (
        NotificationRecipient.objects
        .select_related("notification", "notification__created_by")
        .filter(teacher=request.user)
        .order_by("-created_at", "-id")
    )

    # الإشعارات والنشرات في صندوق واحد؛ التعاميم الرسمية لها سجلها المستقل.
    qs = qs.filter(notification__kind__in=["notification", "newsletter"])

    # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL)
    if active_school is not None:
        qs = qs.filter(Q(notification__school=active_school) | Q(notification__school__isnull=True))
    else:
        qs = qs.filter(notification__school__isnull=True)

    # إخفاء المنتهية
    now = timezone.now()
    qs = qs.exclude(notification__expires_at__lt=now)

    page = Paginator(qs, 12).get_page(request.GET.get("page") or 1)

    # ملاحظة: لا نُعلّم الإشعارات كمقروءة تلقائيًا عند فتح القائمة.
    # يبقى الإشعار "غير مقروء" حتى يفتح المستخدم تفاصيله (يُعلَّم في صفحة التفاصيل)،
    # أو يضغط "تمت القراءة" لإشعار، أو "تحديد الكل كمقروء". هذا يجعل فلتر
    # "غير المقروء فقط" والعدّاد ذا معنى حقيقي.

    # اسم المرسل + الدور الصحيح (مُوحّد). تعثّرٌ هنا يترك الكشف بلا أسماء
    # مرسلين — يُقرأ «إشعار مجهول المصدر»، وهو خطأ ظاهر يستحق أثراً.
    with soft_fail("notifications.sender_labels", user_id=request.user.pk):
        items = list(page.object_list)
        for rr in items:
            n = getattr(rr, "notification", None)
            sender = getattr(n, "created_by", None) if n is not None else None
            school_scope = (getattr(n, "school", None) if n is not None else None) or active_school
            rr.sender_name = _canonical_sender_name(sender)
            rr.sender_role_label = _canonical_role_label(sender, school_scope)
        page.object_list = items
    return render(request, "reports/my_notifications.html", {"page_obj": page})


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def my_circulars(request: HttpRequest) -> HttpResponse:
    """قائمة التعاميم للمستخدم، سواء تطلبت إقرارًا أم كانت للاطلاع."""
    if NotificationRecipient is None:
        return render(request, "reports/my_circulars.html", {"page_obj": Paginator([], 12).get_page(1)})

    active_school = _get_active_school(request)

    try:
        qs = (
            NotificationRecipient.objects
            .select_related("notification", "notification__created_by")
            .filter(teacher=request.user)
            .order_by("-created_at", "-id")
        )
    except Exception:
        logger.exception("my_circulars: failed to build base queryset")
        messages.error(request, "تعذر تحميل التعاميم حالياً. سيتم تسجيل المشكلة تلقائياً.")
        return render(request, "reports/my_circulars.html", {"page_obj": Paginator([], 12).get_page(1)})

    # فصل النوع عن سياسة التوقيع: التعميم يبقى تعميماً حتى لو أنشئ من مسودة
    # لا تطلب توقيعاً، والنشرة الموقعة لا تتسرب إلى سجل التعاميم.
    qs = qs.filter(notification__kind="circular")

    # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL)
    if active_school is not None:
        qs = qs.filter(Q(notification__school=active_school) | Q(notification__school__isnull=True))
    else:
        qs = qs.filter(notification__school__isnull=True)

    # إخفاء الوثائق المنتهية، مع إبقاء ما انتهت مهلة إقراره ظاهرًا للمتابعة.
    now = timezone.now()
    qs = qs.exclude(notification__expires_at__lt=now)

    status_filter = request.GET.get("status", "all")
    if status_filter not in {"all", "pending", "closed", "signed", "reading"}:
        status_filter = "all"
    if status_filter == "pending":
        qs = qs.filter(notification__requires_signature=True, is_signed=False).filter(
            Q(notification__signature_deadline_at__gte=now)
            | Q(notification__signature_deadline_at__isnull=True)
        )
    elif status_filter == "closed":
        qs = qs.filter(notification__requires_signature=True, is_signed=False,
                       notification__signature_deadline_at__lt=now)
    elif status_filter == "signed":
        qs = qs.filter(is_signed=True)
    elif status_filter == "reading":
        qs = qs.filter(notification__requires_signature=False)

    try:
        page = Paginator(qs, 12).get_page(request.GET.get("page") or 1)
    except Exception:
        logger.exception("my_circulars: failed to paginate")
        messages.error(request, "تعذر تحميل التعاميم حالياً. سيتم تسجيل المشكلة تلقائياً.")
        return render(request, "reports/my_circulars.html", {"page_obj": Paginator([], 12).get_page(1)})

    # مهم: QuerySet داخل Page قد يبقى كسولاً، وقد يحدث الخطأ أثناء عرض القالب.
    # هنا نجبر التقييم داخل الـ view حتى نلتقط أخطاء قاعدة البيانات (مثل نقص migrations) ونمنع 500.
    try:
        page.object_list = list(page.object_list)
    except Exception:
        logger.exception("my_circulars: failed to evaluate page object_list")
        messages.error(request, "تعذر تحميل التعاميم حالياً. سيتم تسجيل المشكلة تلقائياً.")
        return render(request, "reports/my_circulars.html", {"page_obj": Paginator([], 12).get_page(1)})

    # لا نعدّ عرض المقتطف في القائمة قراءةً للتعميم الرسمي. تُسجّل القراءة
    # فقط عند فتح الوثيقة أو عبر إجراء صريح من المستخدم.

    return render(request, "reports/my_circulars.html", {
        "page_obj": page, "status_filter": status_filter, "now": now,
    })


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def my_notification_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Show a single notification (for the current user) in a dedicated page.

    pk here refers to NotificationRecipient.pk.
    """
    if NotificationRecipient is None:
        messages.error(request, "نموذج الإشعار غير متاح.")
        return redirect("reports:my_notifications")

    try:
        r = get_object_or_404(
            NotificationRecipient.objects.select_related(
                "notification",
                "notification__created_by",
            ),
            pk=pk,
            teacher=request.user,
        )
    except Exception:
        logger.exception("my_notification_detail: failed to load recipient row", extra={"pk": pk})
        messages.error(request, "تعذر فتح التعميم/الإشعار حالياً. سيتم تسجيل المشكلة تلقائياً.")
        return redirect("reports:my_circulars")

    n = getattr(r, "notification", None)
    if n is None:
        messages.error(request, "تعذّر العثور على الإشعار.")
        return redirect("reports:my_notifications")

    is_circular = _is_circular(n)
    is_newsletter = _is_newsletter(n)

    # منع الخلط 100%: إذا كان الرابط من تبويب خاطئ نعيد توجيهه للرابط الصحيح
    with soft_fail("notifications.detail_tab_redirect", recipient_id=r.pk):
        url_name = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
        if is_circular and url_name == "my_notification_detail":
            return redirect("reports:my_circular_detail", pk=r.pk)
        if (not is_circular) and url_name == "my_circular_detail":
            return redirect("reports:my_notification_detail", pk=r.pk)

    body = (
        getattr(n, "message", None)
        or getattr(n, "body", None)
        or getattr(n, "content", None)
        or getattr(n, "text", None)
        or getattr(n, "details", None)
        or ""
    )

    # اسم/دور المرسل (موحّد)
    try:
        sender = getattr(n, "created_by", None)
        school_scope = getattr(n, "school", None) or _get_active_school(request)
        sender_name = _canonical_sender_name(sender)
        sender_role_label = _canonical_role_label(sender, school_scope)
    except Exception:
        _degraded("notifications.detail_sender_label", recipient_id=r.pk)
        sender_name = "الإدارة"
        sender_role_label = ""

    # Mark as read on open.
    # فشلٌ هنا يُبقي الإشعار «غير مقروء» بعد فتحه — فيظلّ العدّاد مرتفعاً بلا
    # سبب مفهوم للمستخدم، وهو أكثر ما يُبلَّغ عنه في هذه الشاشة.
    with soft_fail("notifications.mark_read_on_open", recipient_id=r.pk):
        updated_fields: list[str] = []
        if not bool(getattr(r, "is_read", False)):
            r.is_read = True
            updated_fields.append("is_read")
        if getattr(r, "read_at", None) is None:
            r.read_at = timezone.now()
            updated_fields.append("read_at")
        if updated_fields:
            r.save(update_fields=updated_fields)

    # هل انتهى آخر موعد للتوقيع؟ (لإغلاق نموذج التوقيع في الواجهة)
    signing_closed = False
    try:
        _deadline = getattr(n, "signature_deadline_at", None)
        _expires = getattr(n, "expires_at", None)
        signing_closed = bool(
            (_deadline and timezone.now() > _deadline)
            or (_expires and timezone.now() > _expires)
        )
    except Exception:
        signing_closed = False

    return render(
        request,
        "reports/my_circular_detail.html" if (is_circular or is_newsletter) else "reports/my_notification_detail.html",
        {
            "r": r,
            "n": n,
            "body": body,
            "sender_name": sender_name,
            "sender_role_label": sender_role_label,
            "signing_closed": signing_closed,
            "communication_kind": _communication_kind(n),
            "communication_label": _communication_label(n),
            "is_newsletter": is_newsletter,
        },
    )

@login_required(login_url="reports:login")
@access_required(_is_staff_or_officer)
@require_http_methods(["GET"])
def notifications_sent(request: HttpRequest, mode: str = "notification") -> HttpResponse:
    mode = (mode or "notification").strip().lower()
    if mode not in {"notification", "circular"}:
        mode = "notification"
    is_circular = mode == "circular"

    if is_circular:
        if not request.user.is_superuser and not _is_manager_in_school(request.user, _get_active_school(request)):
            active_school = _get_active_school(request)
            messages.error(request, f"التعاميم متاحة لـ{_school_manager_label(active_school)} فقط.")
            return redirect("reports:home")

    if Notification is None:
        return render(
            request,
            "reports/circulars_sent.html" if is_circular else "reports/notifications_sent.html",
            {
                "page_obj": Paginator([], 20).get_page(1),
                "stats": {},
                "mode": mode,
                "title": "التعاميم المرسلة" if is_circular else "الإشعارات المرسلة",
            },
        )

    active_school = _get_active_school(request)
    if not request.user.is_superuser and active_school is None:
        messages.error(request, "يرجى اختيار المدرسة أولاً.")
        return redirect("reports:home")

    qs = Notification.objects.all().order_by("-created_at", "-id")

    # صفحة "المرسلة" تعرض فقط الإشعارات التي أرسلها مستخدم فعلياً.
    # إشعارات النظام (created_by=NULL) مثل التعليقات الخاصة والتنبيهات الآلية لا تظهر هنا.
    qs = qs.filter(created_by__isnull=False)

    # فصل النوع عن سياسة التوقيع. صفحة التواصل المرسل تجمع الإشعارات والنشرات
    # وتتيح تبويباً واضحاً بينهما، بينما تبقى التعاميم في سجلها الرسمي.
    kind_filter = (request.GET.get("kind") or "all").strip().lower()
    if is_circular:
        qs = qs.filter(kind="circular")
        kind_filter = "circular"
    elif kind_filter in {"notification", "newsletter"}:
        qs = qs.filter(kind=kind_filter)
    else:
        kind_filter = "all"
        qs = qs.filter(kind__in=["notification", "newsletter"])

    # غير السوبر: لا يرى إلا إشعارات المدرسة النشطة (لا إشعارات عامة)
    if not request.user.is_superuser:
        qs = qs.filter(school=active_school)

    # صفحة "المرسلة" تعرض ما أرسله المستخدم الحالي فقط، بما في ذلك مدير النظام.
    # كان مدير النظام يرى كل إشعارات المنصة، فتضيع إشعاراته وسط الصفحات ولا تبدو كأنها أُرسلت.
    qs = qs.filter(created_by=request.user)

    qs = qs.select_related("created_by", "school")
    page = Paginator(qs, 20).get_page(request.GET.get("page") or 1)

    notif_ids = [n.id for n in page.object_list]
    stats: dict[int, dict] = {}

    # حساب read/total على NotificationRecipient.
    #
    # كانت هذه الكتلة تكتشف اسم المفتاح الأجنبي بالمرور على الميتا، ثم تختار
    # حقل القراءة من بين أربعة مرشّحين، ثم حقل التوقيع من بين اثنين — وكلّها
    # فروعٌ لمخطّطات لا وجود لها في هذا المشروع. والثمن أن أي خطأ حقيقي في
    # الاستعلام كان يسقط في فرع «لا حقل مطابق» فتُعرض النسبة صفراً بلا خطأ:
    # مديرٌ يقرأ «لم يقرأ أحد تعميمي» وهو مقروء.
    if notif_ids:
        rows = (
            NotificationRecipient.objects
            .filter(notification_id__in=notif_ids)
            .values("notification_id")
            .annotate(
                total=Count("id"),
                read=Count("id", filter=Q(is_read=True)),
                signed=Count("id", filter=Q(is_signed=True)),
            )
        )
        for row in rows:
            stats[row["notification_id"]] = {
                "total": row.get("total", 0),
                "read": row.get("read", 0),
                "signed": row.get("signed", 0),
            }

    for n in page.object_list:
        bucket = stats.get(n.id, {})
        total = int(bucket.get("total", 0) or 0)
        read = int(bucket.get("read", 0) or 0)
        signed = int(bucket.get("signed", 0) or 0)
        used = signed if bool(getattr(n, "requires_signature", False)) else read
        n.stat_total = total
        n.stat_read = read
        n.stat_signed = signed
        n.stat_used = used
        n.stat_rate = int(round((used / total) * 100)) if total else 0

    # أسماء مستلمين مختصرة
    rec_names_map: dict[int, list[str]] = {i: [] for i in notif_ids}

    def _name_of(person) -> str:
        return (getattr(person, "name", None) or
                getattr(person, "phone", None) or
                getattr(person, "username", None) or
                getattr(person, "national_id", None) or
                str(person))

    for n in page.object_list:
        names_set = set()
        with soft_fail("notifications.sent_recipient_names", notification_id=n.id):
            for t in n.recipients.all()[:12]:
                if t:
                    nm = _name_of(t)
                    if nm not in names_set:
                        names_set.add(nm)
        rec_names_map[n.id] = list(names_set)

    remaining_ids = [nid for nid, arr in rec_names_map.items() if len(arr) < 5]
    if remaining_ids:
        notif_fk_name = "notification"
        if notif_fk_name:
            thr_qs = NotificationRecipient.objects.filter(**{f"{notif_fk_name}_id__in": remaining_ids})
            for r in thr_qs:
                nid = getattr(r, f"{notif_fk_name}_id", None)
                if not nid:
                    continue
                person = (getattr(r, "teacher", None) or
                          getattr(r, "user", None) or
                          getattr(r, "recipient", None))
                if person:
                    nm = _name_of(person)
                    arr = rec_names_map.get(nid, [])
                    if nm and nm not in arr and len(arr) < 12:
                        arr.append(nm)
                        rec_names_map[nid] = arr

    for n in page.object_list:
        n.rec_names = rec_names_map.get(n.id, [])

    return render(
        request,
        "reports/circulars_sent.html" if is_circular else "reports/notifications_sent.html",
        {
            "page_obj": page,
            "stats": stats,
            "mode": mode,
            "title": "التعاميم المرسلة" if is_circular else "الإشعارات المرسلة",
            "kind_filter": kind_filter,
        },
    )

# تعليم الإشعار كمقروء (حسب Recipient pk)
@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def notification_mark_read(request: HttpRequest, pk: int) -> HttpResponse:
    if NotificationRecipient is None:
        return redirect(_safe_next_url(request.POST.get("next")) or "reports:my_notifications")
    item = get_object_or_404(NotificationRecipient, pk=pk, teacher=request.user)
    if not getattr(item, "is_read", False):
        if hasattr(item, "is_read"):
            item.is_read = True
        if hasattr(item, "read_at"):
            item.read_at = timezone.now()
        try:
            if hasattr(item, "is_read") and hasattr(item, "read_at"):
                item.save(update_fields=["is_read", "read_at"])
            else:
                item.save()
        except Exception:
            item.save()
    return redirect(_safe_next_url(request.POST.get("next")) or "reports:my_notifications")

# تحديد الكل كمقروء
@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def notifications_mark_all_read(request: HttpRequest) -> HttpResponse:
    if NotificationRecipient is None:
        return redirect(_safe_next_url(request.POST.get("next")) or "reports:my_notifications")
    qs = NotificationRecipient.objects.filter(teacher=request.user)
    active_school = _get_active_school(request)

    if active_school is not None:
        qs = qs.filter(Q(notification__school=active_school) | Q(notification__school__isnull=True))
    else:
        qs = qs.filter(notification__school__isnull=True)
    qs = qs.filter(Q(notification__expires_at__gt=timezone.now()) | Q(notification__expires_at__isnull=True))

    # فصل: هذا الإجراء خاص بالإشعارات فقط (يستبعد التعاميم)
    qs = qs.filter(notification__kind__in=["notification", "newsletter"])

    # ``update`` واحد بدل حلقةِ حفظٍ لكل صف. والفشل هنا **لا يُبتلع**: المستخدم
    # كان يُخبَر «تم تحديد الجميع كمقروءة» ثم يجد العدّاد كما هو — وهو أسوأ من
    # رسالة فشل صريحة.
    try:
        qs.filter(is_read=False).update(is_read=True, read_at=timezone.now())
    except Exception:
        _degraded("notifications.mark_all_read", user_id=request.user.pk)
        messages.error(request, "تعذّر تحديد الإشعارات كمقروءة. أعد المحاولة.")
        return redirect(_safe_next_url(request.POST.get("next")) or "reports:my_notifications")

    messages.success(request, "تم تحديد جميع الإشعارات كمقروءة.")
    _resync_notification_clients(request.user)

    return redirect(_safe_next_url(request.POST.get("next")) or "reports:my_notifications")


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def circulars_mark_all_read(request: HttpRequest) -> HttpResponse:
    if NotificationRecipient is None:
        return redirect(_safe_next_url(request.POST.get("next")) or "reports:my_circulars")

    qs = NotificationRecipient.objects.filter(teacher=request.user)
    active_school = _get_active_school(request)

    if active_school is not None:
        qs = qs.filter(Q(notification__school=active_school) | Q(notification__school__isnull=True))
    else:
        qs = qs.filter(notification__school__isnull=True)
    qs = qs.filter(Q(notification__expires_at__gt=timezone.now()) | Q(notification__expires_at__isnull=True))
    qs = qs.filter(notification__kind="circular")

    try:
        qs.filter(is_read=False).update(is_read=True, read_at=timezone.now())
    except Exception:
        _degraded("notifications.mark_all_circulars_read", user_id=request.user.pk)
        messages.error(request, "تعذّر تحديد التعاميم كمقروءة. أعد المحاولة.")
        return redirect(_safe_next_url(request.POST.get("next")) or "reports:my_circulars")

    messages.success(request, "تم تحديد جميع التعاميم كمقروءة.")
    _resync_notification_clients(request.user)
    return redirect(_safe_next_url(request.POST.get("next")) or "reports:my_circulars")

# تعليم الإشعار كمقروء (حسب رقم الإشعار نفسه لا الـRecipient)
@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def notification_mark_read_by_notification(request: HttpRequest, pk: int) -> HttpResponse:
    if NotificationRecipient is None:
        return JsonResponse({"ok": False}, status=400)
    try:
        item = NotificationRecipient.objects.filter(
            notification_id=pk, teacher=request.user
        ).first()
        if item:
            if hasattr(item, "is_read") and not item.is_read:
                item.is_read = True
            if hasattr(item, "read_at") and getattr(item, "read_at", None) is None:
                item.read_at = timezone.now()
            try:
                if hasattr(item, "is_read") and hasattr(item, "read_at"):
                    item.save(update_fields=["is_read", "read_at"])
                else:
                    item.save()
            except Exception:
                item.save()
        return JsonResponse({"ok": True})
    except Exception:
        return JsonResponse({"ok": False}, status=400)

# إبقاء المسار القديم للتوافق الخلفي: تحويل إلى صفحة الإنشاء
@login_required(login_url="reports:login")
@access_required(_is_staff)
def send_notification(request: HttpRequest) -> HttpResponse:
    return redirect("reports:notifications_create")
