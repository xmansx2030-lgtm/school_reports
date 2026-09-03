# reports/views/notifications.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from core.observability import report_degraded as _degraded, soft_call, soft_fail

from ..coverage import pending_documenters

from ._helpers import *
from ._helpers import (
    _is_staff, _is_staff_or_officer, _is_manager_in_school,
    _role_display_map, _school_manager_label,
    _get_active_school, _canonical_sender_name, _canonical_role_label,
    effective_user_role_label, _safe_next_url,
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
    )
    if request.method == "POST":
        if form.is_valid():
            attachment = form.cleaned_data.get("attachment")
            storage_school = active_school or form.cleaned_data.get("target_school")
            capacity_error = archive_storage_capacity_error(
                storage_school,
                [attachment] if attachment and is_circular else [],
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
                        "title": "إنشاء تعميم" if is_circular else "إنشاء إشعار",
                    },
                )
            try:
                with transaction.atomic():
                    form.save(
                        creator=request.user,
                        default_school=active_school,
                        force_requires_signature=True if is_circular else False,
                    )
                messages.success(request, "تم إرسال التعميم." if is_circular else "تم إرسال الإشعار.")
                return redirect("reports:circulars_sent" if is_circular else "reports:notifications_sent")
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
            "title": "إنشاء تعميم" if is_circular else "إنشاء إشعار",
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
    sent_list_url = "reports:circulars_sent" if bool(getattr(n, "requires_signature", False)) else "reports:notifications_sent"

    # التعميمات: سماح لمدير المدرسة/مدير النظام
    if bool(getattr(n, "requires_signature", False)):
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

def _arabic_role_label(role_slug: str, active_school: Optional[School] = None) -> str:
    return _role_display_map(active_school).get((role_slug or "").lower(), role_slug or "")


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
    sent_list_url = "reports:circulars_sent" if bool(getattr(n, "requires_signature", False)) else "reports:notifications_sent"

    # التعميمات: سماح لمدير المدرسة/مدير النظام
    if bool(getattr(n, "requires_signature", False)):
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
        "can_add_recipients": can_add_recipients,
        "eligible_new_recipients": eligible_new_recipients,
    }
    template_name = "reports/circular_detail.html" if bool(getattr(n, "requires_signature", False)) else "reports/notification_detail.html"
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
    """Teacher signs a circular (NotificationRecipient.pk) using phone re-entry + acknowledgement."""
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
        messages.error(request, "تعذّر العثور على التعميم.")
        return redirect("reports:my_circulars")

    if not bool(getattr(n, "requires_signature", False)):
        messages.error(request, "هذا الإشعار لا يتطلب توقيعاً.")
        return redirect("reports:my_notification_detail", pk=rec.pk)

    if bool(getattr(rec, "is_signed", False)):
        messages.info(request, "تم تسجيل توقيعك مسبقاً على هذا التعميم.")
        return redirect("reports:my_circular_detail", pk=rec.pk)

    # ✅ منع التوقيع بعد انتهاء آخر موعد للتوقيع (إن حُدّد)
    deadline = getattr(n, "signature_deadline_at", None)
    if deadline and timezone.now() > deadline:
        messages.error(request, "انتهى آخر موعد للتوقيع على هذا التعميم، ولم يعد بالإمكان اعتماد التوقيع.")
        return redirect("reports:my_circular_detail", pk=rec.pk)

    now = timezone.now()
    max_attempts = 5
    window = timedelta(minutes=15)

    try:
        attempts = int(getattr(rec, "signature_attempt_count", 0) or 0)
    except Exception:
        attempts = 0
    last_attempt = getattr(rec, "signature_last_attempt_at", None)

    # Reset attempts after window
    if last_attempt and (now - last_attempt) > window:
        attempts = 0

    if last_attempt and (now - last_attempt) <= window and attempts >= max_attempts:
        minutes_left = int(max(1, (window - (now - last_attempt)).total_seconds() // 60))
        messages.error(request, f"تم تجاوز عدد المحاولات. حاول مرة أخرى بعد {minutes_left} دقيقة.")
        return redirect("reports:my_circular_detail", pk=rec.pk)

    entered_phone = (request.POST.get("phone") or "").strip()
    ack = request.POST.get("ack") in {"1", "on", "true", "yes"}

    # Register an attempt (best-effort).
    # عدّاد المحاولات هو ما يحدّ من تخمين رقم الجوال في التوقيع — فتعثّر حفظه
    # يُبطل الحدّ ويجب أن يُرى، لا أن يُبتلع.
    with soft_fail("notifications.signature_attempt_counter", recipient_id=rec.pk):
        rec.signature_attempt_count = attempts + 1
        rec.signature_last_attempt_at = now
        rec.save(update_fields=["signature_attempt_count", "signature_last_attempt_at"])

    if not ack:
        messages.error(request, "يلزم الموافقة على الإقرار قبل اعتماد التوقيع.")
        return redirect("reports:my_circular_detail", pk=rec.pk)

    if not entered_phone:
        messages.error(request, "يرجى إدخال رقم الجوال المسجل للتوقيع.")
        return redirect("reports:my_circular_detail", pk=rec.pk)

    if _phone_key(entered_phone) != _phone_key(getattr(request.user, "phone", "")):
        messages.error(request, "رقم الجوال غير مطابق للرقم المسجل. تأكد وحاول مرة أخرى.")
        return redirect("reports:my_circular_detail", pk=rec.pk)

    # Sign + mark read
    try:
        update_fields: list[str] = []
        if hasattr(rec, "is_signed"):
            rec.is_signed = True
            update_fields.append("is_signed")
        if hasattr(rec, "signed_at"):
            rec.signed_at = now
            update_fields.append("signed_at")
        if hasattr(rec, "is_read") and not bool(getattr(rec, "is_read", False)):
            rec.is_read = True
            update_fields.append("is_read")
        if hasattr(rec, "read_at") and getattr(rec, "read_at", None) is None:
            rec.read_at = now
            update_fields.append("read_at")
        if update_fields:
            try:
                rec.save(update_fields=update_fields)
            except Exception:
                rec.save()
    except Exception:
        logger.exception("notification_sign failed")
        messages.error(request, "تعذّر تسجيل التوقيع. جرّب لاحقًا.")
        return redirect("reports:my_circular_detail", pk=rec.pk)

    messages.success(request, "تم تسجيل توقيعك على التعميم.")
    return redirect("reports:my_circular_detail", pk=rec.pk)


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
    sent_list_url = "reports:circulars_sent" if bool(getattr(n, "requires_signature", False)) else "reports:notifications_sent"

    # هذا التقرير خاص بالتعاميم فقط
    if not bool(getattr(n, "requires_signature", False)):
        messages.error(request, "هذا التقرير متاح للتعاميم فقط.")
        return redirect(sent_list_url)

    # Permission: manager in school or creator
    if not _is_manager_in_school(request.user, active_school):
        if getattr(n, "created_by_id", None) != request.user.id:
            messages.error(request, "لا تملك صلاحية عرض تقرير هذا التعميم.")
            return redirect(sent_list_url)

    # School isolation
    if (not is_superuser) and hasattr(n, "school_id"):
        if getattr(n, "school_id", None) != getattr(active_school, "id", None):
            messages.error(request, "لا تملك صلاحية عرض تعميم من مدرسة أخرى.")
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
            "name": getattr(t, "name", "") or str(t),
            "role": effective_user_role_label(t, active_school=active_school),
            "phone": _mask_phone(getattr(t, "phone", "")),
            "read": bool(getattr(r, "is_read", False)),
            "read_at": getattr(r, "read_at", None),
            "signed": is_signed,
            "signed_at": getattr(r, "signed_at", None),
        })

    ctx = {
        "n": n,
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
            cache_key = f"unreadcnt:v1:u{uid}:s{sid_for_key}"
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

    # unread = unread notifications only (exclude circulars)
    unread_q = Q(is_read=False) & Q(notification__requires_signature=False)

    # signatures_pending = unsigned circulars
    pending_sig_q = Q(notification__requires_signature=True, is_signed=False)

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

    # فصل: هذه الصفحة للإشعارات فقط (بدون التعاميم)
    qs = qs.filter(notification__requires_signature=False)

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
    """قائمة التعاميم للمستخدم (التي تتطلب توقيعاً)."""
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

    # فصل: هذه الصفحة للتعاميم فقط
    qs = qs.filter(notification__requires_signature=True)

    # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL)
    if active_school is not None:
        qs = qs.filter(Q(notification__school=active_school) | Q(notification__school__isnull=True))
    else:
        qs = qs.filter(notification__school__isnull=True)

    # إخفاء المنتهية
    now = timezone.now()
    qs = qs.exclude(notification__expires_at__lt=now)

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

    return render(request, "reports/my_circulars.html", {"page_obj": page})


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

    is_circular = bool(getattr(n, "requires_signature", False))

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
        signing_closed = bool(_deadline and timezone.now() > _deadline)
    except Exception:
        signing_closed = False

    return render(
        request,
        "reports/my_circular_detail.html" if is_circular else "reports/my_notification_detail.html",
        {
            "r": r,
            "n": n,
            "body": body,
            "sender_name": sender_name,
            "sender_role_label": sender_role_label,
            "signing_closed": signing_closed,
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

    # فصل التعاميم عن الإشعارات
    qs = qs.filter(requires_signature=True) if is_circular else qs.filter(requires_signature=False)

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
    qs = qs.filter(notification__requires_signature=False)

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
    qs = qs.filter(notification__requires_signature=True)

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
