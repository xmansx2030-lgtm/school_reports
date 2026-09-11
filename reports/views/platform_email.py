from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from core.client_ip import client_ip

from ..models import AuditLog, PlatformEmail, PlatformEmailAttachment, PlatformEmailConfiguration
from ..platform_email_forms import PlatformEmailComposeForm, PlatformEmailConfigurationForm, PlatformEmailReplyForm
from ..resend_email import (
    ResendError,
    attachment_download_url,
    platform_email_domains,
    process_webhook_event,
    resend_is_configured,
    send_platform_email,
    sync_recent_received_emails,
    verify_webhook_signature,
    webhook_is_configured,
)
from ..view_access import platform_superuser_required


def _audit(request: HttpRequest, action: str, email: PlatformEmail, changes: dict) -> None:
    AuditLog.objects.create(
        teacher=request.user,
        action=action,
        model_name="PlatformEmail",
        object_id=email.pk,
        object_repr=email.subject[:255],
        changes=changes,
        ip_address=client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:500],
    )


def _platform_outbound_scope_q() -> Q:
    query = Q(pk__in=[])
    for domain in platform_email_domains():
        query |= Q(from_email__iendswith=f"@{domain}")
    return query


def _visible_mail_scope_q() -> Q:
    return Q(direction=PlatformEmail.Direction.INBOUND) | (
        Q(direction=PlatformEmail.Direction.OUTBOUND) & _platform_outbound_scope_q()
    )


def _visible_mail_queryset():
    return PlatformEmail.objects.filter(_visible_mail_scope_q())


def _mailbox_stats() -> dict:
    now = timezone.now()
    aggregate = _visible_mail_queryset().aggregate(
        unread=Count(
            "id",
            filter=Q(direction=PlatformEmail.Direction.INBOUND, is_read=False, is_archived=False),
        ),
        inbox=Count("id", filter=Q(direction=PlatformEmail.Direction.INBOUND, is_archived=False)),
        sent=Count("id", filter=Q(direction=PlatformEmail.Direction.OUTBOUND, is_archived=False)),
        starred=Count("id", filter=Q(is_starred=True, is_archived=False)),
        archived=Count("id", filter=Q(is_archived=True)),
        delivered_7d=Count(
            "id",
            filter=Q(
                direction=PlatformEmail.Direction.OUTBOUND,
                status=PlatformEmail.Status.DELIVERED,
                created_at__gte=now - timedelta(days=7),
            ),
        ),
        failed_7d=Count(
            "id",
            filter=Q(
                direction=PlatformEmail.Direction.OUTBOUND,
                status__in=(PlatformEmail.Status.FAILED, PlatformEmail.Status.BOUNCED, PlatformEmail.Status.COMPLAINED),
                created_at__gte=now - timedelta(days=7),
            ),
        ),
    )
    return {key: int(value or 0) for key, value in aggregate.items()}


def _system_email_configured() -> bool:
    backend = (getattr(settings, "EMAIL_BACKEND", "") or "").strip()
    from_email = (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip()
    if "@" not in from_email:
        return False
    if backend == "reports.email_backends.ResendEmailBackend":
        return resend_is_configured()
    if backend == "django.core.mail.backends.smtp.EmailBackend":
        return bool(getattr(settings, "EMAIL_HOST", "") and getattr(settings, "EMAIL_PORT", None))
    return False


@platform_superuser_required
@require_http_methods(["GET"])
def platform_email_inbox(request: HttpRequest) -> HttpResponse:
    folder = (request.GET.get("folder") or "inbox").strip().lower()
    if folder not in {"inbox", "sent", "starred", "archive", "all"}:
        folder = "inbox"
    query = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    valid_statuses = {value for value, _label in PlatformEmail.Status.choices}
    if status not in valid_statuses:
        status = ""

    emails = _visible_mail_queryset().prefetch_related("attachments")
    if folder == "inbox":
        emails = emails.filter(direction=PlatformEmail.Direction.INBOUND, is_archived=False)
    elif folder == "sent":
        emails = emails.filter(direction=PlatformEmail.Direction.OUTBOUND, is_archived=False)
    elif folder == "starred":
        emails = emails.filter(is_starred=True, is_archived=False)
    elif folder == "archive":
        emails = emails.filter(is_archived=True)
    else:
        emails = emails.filter(is_archived=False)
    if status:
        emails = emails.filter(status=status)
    if query:
        emails = emails.filter(
            Q(subject__icontains=query)
            | Q(from_email__icontains=query)
            | Q(from_name__icontains=query)
            | Q(text_body__icontains=query)
            | Q(snippet__icontains=query)
            | Q(to_emails__icontains=query)
        )
    page_obj = Paginator(emails, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "reports/platform_email_inbox.html",
        {
            "emails": page_obj,
            "page_obj": page_obj,
            "folder": folder,
            "search_query": query,
            "current_status": status,
            "status_choices": PlatformEmail.Status.choices,
            "mail_stats": _mailbox_stats(),
            "email_config": PlatformEmailConfiguration.load(),
            "api_configured": resend_is_configured(),
            "webhook_configured": webhook_is_configured(),
            "system_email_configured": _system_email_configured(),
        },
    )


def _uploaded_attachments(files) -> list[dict]:
    if len(files) > 5:
        raise ValueError("الحد الأعلى خمسة مرفقات في الرسالة.")
    allowed_extensions = {
        ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".doc", ".docx",
        ".xls", ".xlsx", ".csv", ".txt", ".zip",
    }
    total_size = 0
    output = []
    for uploaded in files:
        size = int(getattr(uploaded, "size", 0) or 0)
        suffix = Path(uploaded.name or "").suffix.lower()
        if suffix not in allowed_extensions:
            raise ValueError(f"نوع المرفق غير مسموح: {uploaded.name}")
        if size <= 0 or size > 10 * 1024 * 1024:
            raise ValueError(f"حجم المرفق {uploaded.name} يجب ألا يتجاوز 10 ميجابايت.")
        total_size += size
        if total_size > 20 * 1024 * 1024:
            raise ValueError("إجمالي المرفقات يجب ألا يتجاوز 20 ميجابايت.")
        output.append(
            {
                "filename": Path(uploaded.name).name[:255],
                "content": base64.b64encode(uploaded.read()).decode("ascii"),
                "content_type": getattr(uploaded, "content_type", "") or "",
                "size": size,
            }
        )
    return output


@platform_superuser_required
@require_http_methods(["GET", "POST"])
def platform_email_compose(request: HttpRequest) -> HttpResponse:
    initial = {}
    forward_id = request.GET.get("forward")
    forwarded = PlatformEmail.objects.filter(pk=forward_id).first() if str(forward_id or "").isdigit() else None
    if forwarded:
        initial = {
            "subject": forwarded.subject if forwarded.subject.lower().startswith("fwd:") else f"إعادة توجيه: {forwarded.subject}",
            "body": "\n\n---------- الرسالة المحولة ----------\nمن: {}\nالموضوع: {}\n\n{}".format(
                forwarded.display_sender,
                forwarded.subject,
                forwarded.text_body or forwarded.snippet,
            ),
        }
    form = PlatformEmailComposeForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            attachments = _uploaded_attachments(request.FILES.getlist("attachments"))
            email = send_platform_email(
                created_by=request.user,
                to=form.cleaned_data["to"],
                cc=form.cleaned_data["cc"],
                bcc=form.cleaned_data["bcc"],
                subject=form.cleaned_data["subject"],
                body=form.cleaned_data["body"],
                attachments=attachments,
            )
            _audit(request, AuditLog.Action.CREATE, email, {"action": "send", "recipients": email.to_emails})
            messages.success(request, "أُرسلت الرسالة وسنحدّث حالة التسليم تلقائيًا.")
            return redirect("reports:platform_email_detail", pk=email.pk)
        except (ResendError, ValueError) as exc:
            messages.error(request, str(exc))
    return render(
        request,
        "reports/platform_email_compose.html",
        {"form": form, "forwarded": forwarded, "email_config": PlatformEmailConfiguration.load()},
    )


@platform_superuser_required
@require_http_methods(["GET", "POST"])
def platform_email_detail(request: HttpRequest, pk: int) -> HttpResponse:
    email = get_object_or_404(_visible_mail_queryset().prefetch_related("attachments", "events"), pk=pk)
    if not email.is_read:
        email.is_read = True
        email.save(update_fields=("is_read", "updated_at"))
    reply_form = PlatformEmailReplyForm(request.POST or None)
    if (
        request.method == "POST"
        and request.POST.get("action") == "reply"
        and email.direction == PlatformEmail.Direction.INBOUND
        and reply_form.is_valid()
    ):
        recipient = (email.reply_to_emails or [email.from_email])[0]
        subject = email.subject
        if not subject.lower().startswith(("re:", "رد:")):
            subject = f"رد: {subject}"
        try:
            reply = send_platform_email(
                created_by=request.user,
                to=[recipient],
                subject=subject,
                body=reply_form.cleaned_data["body"],
                parent=email,
            )
            _audit(request, AuditLog.Action.CREATE, reply, {"action": "reply", "parent": email.pk})
            messages.success(request, "أُرسل الرد وأضيف إلى المحادثة.")
            return redirect("reports:platform_email_detail", pk=reply.pk)
        except ResendError as exc:
            messages.error(request, str(exc))
    thread = (
        _visible_mail_queryset()
        .filter(thread_key=email.thread_key)
        .select_related("created_by")
        .order_by("created_at", "id")
    )
    return render(
        request,
        "reports/platform_email_detail.html",
        {"email": email, "thread": thread, "reply_form": reply_form},
    )


@platform_superuser_required
@require_POST
def platform_email_action(request: HttpRequest, pk: int) -> HttpResponse:
    email = get_object_or_404(_visible_mail_queryset(), pk=pk)
    action = (request.POST.get("action") or "").strip()
    changes = {}
    if action == "star":
        email.is_starred = not email.is_starred
        changes["is_starred"] = email.is_starred
    elif action == "archive":
        email.is_archived = not email.is_archived
        changes["is_archived"] = email.is_archived
    elif action == "unread":
        email.is_read = False
        changes["is_read"] = False
    else:
        raise Http404("إجراء غير معروف")
    email.save(update_fields=tuple(changes) + ("updated_at",))
    _audit(request, AuditLog.Action.UPDATE, email, changes)
    messages.success(request, "تم تحديث الرسالة.")
    next_url = request.POST.get("next") or ""
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = reverse("reports:platform_email_detail", args=[email.pk])
    return redirect(next_url)


@platform_superuser_required
@require_http_methods(["GET", "POST"])
def platform_email_settings(request: HttpRequest) -> HttpResponse:
    config = PlatformEmailConfiguration.load()
    form = PlatformEmailConfigurationForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم حفظ هوية البريد وإعدادات التشغيل.")
        return redirect("reports:platform_email_settings")
    return render(
        request,
        "reports/platform_email_settings.html",
        {
            "form": form,
            "email_config": config,
            "api_configured": resend_is_configured(),
            "webhook_configured": webhook_is_configured(),
            "system_email_configured": _system_email_configured(),
            "webhook_url": request.build_absolute_uri(reverse("reports:resend_webhook")),
        },
    )


@platform_superuser_required
@require_POST
def platform_email_sync(request: HttpRequest) -> HttpResponse:
    try:
        created, failed = sync_recent_received_emails()
        if failed:
            messages.warning(request, f"تمت مزامنة {created} رسالة، وتعذرت مزامنة {failed} رسالة.")
        else:
            messages.success(request, f"اكتملت المزامنة. أضيفت {created} رسالة جديدة.")
    except ResendError as exc:
        messages.error(request, str(exc))
    return redirect("reports:platform_email_inbox")


@platform_superuser_required
@require_http_methods(["GET"])
def platform_email_attachment_download(request: HttpRequest, pk: int, attachment_pk: int) -> HttpResponse:
    email = get_object_or_404(_visible_mail_queryset(), pk=pk)
    attachment = get_object_or_404(PlatformEmailAttachment, pk=attachment_pk, email=email)
    try:
        return HttpResponseRedirect(attachment_download_url(email, attachment))
    except ResendError as exc:
        messages.error(request, str(exc))
        return redirect("reports:platform_email_detail", pk=email.pk)


@csrf_exempt
@require_POST
def resend_webhook(request: HttpRequest) -> HttpResponse:
    payload = request.body
    webhook_headers = {
        "svix-id": request.headers.get("svix-id", ""),
        "svix-timestamp": request.headers.get("svix-timestamp", ""),
        "svix-signature": request.headers.get("svix-signature", ""),
    }
    if not webhook_is_configured():
        return JsonResponse({"detail": "webhook not configured"}, status=503)
    if not verify_webhook_signature(payload, webhook_headers):
        return HttpResponseBadRequest("invalid signature")
    try:
        event = json.loads(payload.decode("utf-8"))
        if not isinstance(event, dict):
            raise ValueError
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return HttpResponseBadRequest("invalid payload")
    try:
        process_webhook_event(event, webhook_headers["svix-id"])
    except ResendError:
        return JsonResponse({"detail": "provider temporarily unavailable"}, status=503)
    return JsonResponse({"ok": True})
