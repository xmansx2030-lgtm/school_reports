from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.client_ip import client_ip

from ..conversion_ai import generate_conversion_draft
from ..conversion_analytics import build_conversion_rows, default_objective, safe_ai_snapshot
from ..conversion_forms import ConversionDraftForm, ConversionSendForm
from ..models import (
    AuditLog,
    PlatformEmail,
    PlatformEmailConfiguration,
    School,
    SchoolConversionOutreach,
)
from ..resend_email import ResendError, send_platform_email
from ..utils import create_system_notification
from ..view_access import platform_superuser_required


def _audit(request, *, school, action, outreach, changes):
    user = request.user
    AuditLog.objects.create(
        school=school,
        teacher=user,
        actor_name=(getattr(user, "name", "") or str(user))[:150],
        actor_role="مالك المنصة",
        action=action,
        model_name="SchoolConversionOutreach",
        object_id=outreach.pk,
        object_repr=str(outreach)[:255],
        changes=changes,
        ip_address=client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:500],
    )


def _mail_status():
    config = PlatformEmailConfiguration.load()
    last_successful_email = (
        PlatformEmail.objects.filter(
            direction=PlatformEmail.Direction.OUTBOUND,
            status__in=(PlatformEmail.Status.SENT, PlatformEmail.Status.DELIVERED),
        )
        .order_by("-last_event_at", "-sent_at", "-created_at")
        .first()
    )
    return {
        "enabled": bool(config.is_sending_enabled),
        "sender_email": config.sender_email,
        "last_successful_at": (
            last_successful_email.activity_at if last_successful_email else None
        ),
    }


def _row_for_school(school):
    rows = build_conversion_rows([school])
    return rows[0]


@require_GET
@platform_superuser_required
def platform_conversion_dashboard(request):
    schools = School.objects.select_related("subscription__plan").order_by("name")
    query = (request.GET.get("q") or "").strip()
    if query:
        schools = schools.filter(
            Q(name__icontains=query)
            | Q(code__icontains=query)
            | Q(city__icontains=query)
            | Q(email__icontains=query)
        )
    rows = build_conversion_rows(schools)
    segment = (request.GET.get("segment") or "targets").strip()
    if segment == "targets":
        rows = [row for row in rows if row["is_target"]]
    elif segment and segment != "all":
        rows = [row for row in rows if row["segment"] == segment]

    priority = (request.GET.get("priority") or "").strip()
    if priority:
        rows = [row for row in rows if row["priority"] == priority]

    sort = (request.GET.get("sort") or "priority").strip()
    priority_order = {"urgent": 0, "high": 1, "activate": 2, "medium": 3, "low": 4}
    if sort == "utilization":
        rows.sort(key=lambda row: (-row["utilization_score"], row["school"].name))
    elif sort == "intent":
        rows.sort(key=lambda row: (-row["intent_score"], row["school"].name))
    elif sort == "recent":
        rows.sort(
            key=lambda row: row["last_activity"].timestamp() if row["last_activity"] else float("-inf"),
            reverse=True,
        )
    else:
        rows.sort(
            key=lambda row: (
                priority_order.get(row["priority"], 9),
                -row["intent_score"],
                row["school"].name,
            )
        )

    summary = {
        "total": len(rows),
        "high": sum(row["priority"] in {"urgent", "high"} for row in rows),
        "activate": sum(row["priority"] == "activate" for row in rows),
        "expired": sum(row["segment"] in {"trial_expired", "paid_expired"} for row in rows),
    }
    paginator = Paginator(rows, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "reports/platform_conversion_dashboard.html",
        {
            "page_obj": page_obj,
            "summary": summary,
            "query": query,
            "segment": segment,
            "priority": priority,
            "sort": sort,
            "mail_status": _mail_status(),
        },
    )


@require_GET
@platform_superuser_required
def platform_conversion_school(request, pk):
    school = get_object_or_404(School.objects.select_related("subscription__plan"), pk=pk)
    row = _row_for_school(school)
    outreach = school.conversion_outreaches.select_related("created_by", "approved_by").first()
    mail_status = _mail_status()
    draft_form = ConversionDraftForm(
        initial={"objective": default_objective(row), "tone": SchoolConversionOutreach.Tone.EXECUTIVE}
    )
    send_form = ConversionSendForm(instance=outreach) if outreach else None
    # Only the mailbox switch disables the channel. Environment credentials are
    # validated again by send_platform_email at the actual send boundary.
    if send_form is not None and not mail_status["enabled"]:
        send_form.fields["send_email"].initial = False
        send_form.fields["send_email"].disabled = True
    return render(
        request,
        "reports/platform_conversion_school.html",
        {
            "row": row,
            "school": school,
            "outreach": outreach,
            "draft_form": draft_form,
            "send_form": send_form,
            "mail_status": mail_status,
            "history": school.conversion_outreaches.select_related("created_by", "approved_by")[:10],
        },
    )


@require_POST
@platform_superuser_required
def platform_conversion_generate(request, pk):
    school = get_object_or_404(School.objects.select_related("subscription__plan"), pk=pk)
    form = ConversionDraftForm(request.POST)
    if not form.is_valid():
        messages.error(request, "تعذر إنشاء المسودة. راجع الهدف والنبرة ثم حاول مجددًا.")
        return redirect("reports:platform_conversion_school", pk=school.pk)

    row = _row_for_school(school)
    snapshot = safe_ai_snapshot(row)
    draft = generate_conversion_draft(
        snapshot,
        objective=form.cleaned_data["objective"],
        tone=form.cleaned_data["tone"],
        school=school,
        teacher=request.user,
    )
    outreach = SchoolConversionOutreach.objects.create(
        school=school,
        subscription=row["subscription"],
        created_by=request.user,
        objective=form.cleaned_data["objective"],
        tone=form.cleaned_data["tone"],
        context_snapshot=snapshot,
        analysis_summary=draft["analysis_summary"],
        recommended_action=draft["recommended_action"],
        email_subject=draft["email_subject"],
        email_body=draft["email_body"],
        in_app_title=draft["in_app_title"],
        in_app_body=draft["in_app_body"],
        whatsapp_body=draft["whatsapp_body"],
        call_script=draft["call_script"],
        ai_generated=draft["ai_generated"],
        ai_model=draft["ai_model"],
    )
    _audit(
        request,
        school=school,
        action=AuditLog.Action.CREATE,
        outreach=outreach,
        changes={
            "objective": outreach.objective,
            "tone": outreach.tone,
            "ai_generated": outreach.ai_generated,
            "safe_aggregate_snapshot": True,
        },
    )
    if outreach.ai_generated:
        messages.success(request, "جهّز الذكاء الاصطناعي مسودة مخصصة. راجعها وعدّلها قبل الإرسال.")
    else:
        messages.warning(request, "تعذر التوليد الذكي الآن، فتم إعداد مسودة احتياطية قابلة للمراجعة.")
    return redirect(
        f"{reverse('reports:platform_conversion_school', kwargs={'pk': school.pk})}#outreach-review"
    )


@require_POST
@platform_superuser_required
def platform_conversion_send(request, pk, outreach_pk):
    school = get_object_or_404(School, pk=pk)
    with transaction.atomic():
        outreach = get_object_or_404(
            SchoolConversionOutreach.objects.select_for_update(),
            pk=outreach_pk,
            school=school,
        )
        if (
            outreach.status == SchoolConversionOutreach.Status.SENDING
            and outreach.updated_at >= timezone.now() - timedelta(minutes=10)
        ):
            messages.warning(request, "هذه المسودة قيد الإرسال الآن. انتظر النتيجة قبل إعادة المحاولة.")
            return redirect("reports:platform_conversion_school", pk=school.pk)
        form = ConversionSendForm(request.POST, instance=outreach)
        if not form.is_valid():
            for error in form.non_field_errors():
                messages.error(request, str(error))
            if not form.non_field_errors():
                messages.error(request, "لم يتم الإرسال. راجع الحقول المطلوبة والتأكيد النهائي.")
            return redirect(
                f"{reverse('reports:platform_conversion_school', kwargs={'pk': school.pk})}#outreach-review"
            )
        outreach = form.save(commit=False)
        requested = []
        if form.cleaned_data["send_email"]:
            requested.append("email")
            if outreach.email_status != SchoolConversionOutreach.ChannelStatus.SENT:
                outreach.email_status = SchoolConversionOutreach.ChannelStatus.PENDING
        if form.cleaned_data["send_in_app"]:
            requested.append("in_app")
            if outreach.in_app_status != SchoolConversionOutreach.ChannelStatus.SENT:
                outreach.in_app_status = SchoolConversionOutreach.ChannelStatus.PENDING
        outreach.requested_channels = requested
        outreach.status = SchoolConversionOutreach.Status.SENDING
        outreach.approved_by = request.user
        outreach.approved_at = timezone.now()
        outreach.failure_reason = ""
        outreach.save()

    row = _row_for_school(school)
    previous_result = outreach.delivery_result if isinstance(outreach.delivery_result, dict) else {}
    previous_email_rows = previous_result.get("email") if isinstance(previous_result.get("email"), list) else []
    already_emailed = {
        str(item.get("recipient") or "").strip().lower()
        for item in previous_email_rows
        if isinstance(item, dict) and item.get("status") == "sent"
    }
    result = {
        "email": [item for item in previous_email_rows if isinstance(item, dict) and item.get("status") == "sent"],
        "in_app": previous_result.get("in_app"),
    }
    errors = []

    if "email" in outreach.requested_channels and outreach.email_status != SchoolConversionOutreach.ChannelStatus.SENT:
        all_recipients = row["manager_emails"] or ([school.email.strip().lower()] if school.email.strip() else [])
        recipients = [recipient for recipient in all_recipients if recipient not in already_emailed]
        if all_recipients and not recipients:
            outreach.email_status = SchoolConversionOutreach.ChannelStatus.SENT
        elif not recipients:
            errors.append("لا يوجد بريد لمدير المدرسة أو للمدرسة.")
            outreach.email_status = SchoolConversionOutreach.ChannelStatus.FAILED
        else:
            sent_ids = list(outreach.platform_email_ids or [])
            for email_address in recipients:
                try:
                    email = send_platform_email(
                        created_by=request.user,
                        to=[email_address],
                        subject=outreach.email_subject,
                        body=outreach.email_body,
                    )
                    sent_ids.append(email.pk)
                    result["email"].append({"recipient": email_address, "status": "sent", "id": email.pk})
                except (ResendError, ValueError) as exc:
                    errors.append(f"تعذر البريد إلى {email_address}: {str(exc)}")
                    result["email"].append({"recipient": email_address, "status": "failed"})
            outreach.platform_email_ids = sent_ids
            successful_recipients = {
                str(item.get("recipient") or "").strip().lower()
                for item in result["email"]
                if item.get("status") == "sent"
            }
            outreach.email_status = (
                SchoolConversionOutreach.ChannelStatus.SENT
                if all_recipients and set(all_recipients).issubset(successful_recipients)
                else SchoolConversionOutreach.ChannelStatus.FAILED
            )

    if "in_app" in outreach.requested_channels and outreach.in_app_status != SchoolConversionOutreach.ChannelStatus.SENT:
        manager_ids = [manager.pk for manager in row["managers"]]
        if not manager_ids:
            errors.append("لا يوجد مدير نشط لإرسال الإشعار الداخلي.")
            outreach.in_app_status = SchoolConversionOutreach.ChannelStatus.FAILED
        else:
            try:
                notification = create_system_notification(
                    outreach.in_app_title,
                    outreach.in_app_body,
                    school=school,
                    teacher_ids=manager_ids,
                    is_important=False,
                )
                notification.created_by = request.user
                notification.save(update_fields=("created_by",))
                outreach.notification = notification
                outreach.in_app_status = SchoolConversionOutreach.ChannelStatus.SENT
                result["in_app"] = {"status": "sent", "notification_id": notification.pk}
            except Exception as exc:  # task backends vary; preserve partial channel outcome
                errors.append(f"تعذر الإشعار الداخلي: {exc.__class__.__name__}")
                outreach.in_app_status = SchoolConversionOutreach.ChannelStatus.FAILED
                result["in_app"] = {"status": "failed"}

    requested_statuses = []
    if "email" in outreach.requested_channels:
        requested_statuses.append(outreach.email_status)
    if "in_app" in outreach.requested_channels:
        requested_statuses.append(outreach.in_app_status)
    if requested_statuses and all(status == SchoolConversionOutreach.ChannelStatus.SENT for status in requested_statuses):
        outreach.status = SchoolConversionOutreach.Status.SENT
        outreach.sent_at = timezone.now()
    elif any(status == SchoolConversionOutreach.ChannelStatus.SENT for status in requested_statuses):
        outreach.status = SchoolConversionOutreach.Status.PARTIAL
    else:
        outreach.status = SchoolConversionOutreach.Status.FAILED
    outreach.delivery_result = result
    outreach.failure_reason = "\n".join(errors)[:2000]
    outreach.save()
    _audit(
        request,
        school=school,
        action=AuditLog.Action.UPDATE,
        outreach=outreach,
        changes={
            "approved": True,
            "requested_channels": outreach.requested_channels,
            "email_status": outreach.email_status,
            "in_app_status": outreach.in_app_status,
            "status": outreach.status,
        },
    )
    if outreach.status == SchoolConversionOutreach.Status.SENT:
        messages.success(request, "تم إرسال التواصل المعتمد وتسجيل نتيجته في السجل.")
    elif outreach.status == SchoolConversionOutreach.Status.PARTIAL:
        messages.warning(request, "نجحت قناة وتعذرت أخرى. يمكنك إعادة المحاولة دون تكرار القناة الناجحة.")
    else:
        messages.error(request, "تعذر الإرسال. لم تُرسل مسودة واتساب تلقائيًا ويمكن مراجعة السبب أدناه.")
    return redirect(
        f"{reverse('reports:platform_conversion_school', kwargs={'pk': school.pk})}#outreach-review"
    )
