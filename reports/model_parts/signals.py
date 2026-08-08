from __future__ import annotations

from .base import *
from .audit import AuditLog
from .billing import SchoolSubscription, SubscriptionPlan
from .reports import Report
from .schools import Department, DepartmentMembership, ReportType, School, SchoolMembership, Teacher
from .tickets import Ticket
from .notifications import Notification, TicketImage
from .assignments import Assignment, AssignmentEvidence
from .circular_drafts import CircularDraft
from .documents import Document
from .lab import LabAsset, LabAssetHandover, LabExperiment
from .meetings import Decision, Meeting, MeetingMinutes
from .plans import Initiative, Plan
from .scopes import Delegation, StaffScope


def _invalidate_dashboard_after_commit(*school_ids) -> None:
    ids = tuple(sorted({int(sid) for sid in school_ids if sid}))
    if not ids:
        return

    def _invalidate():
        from ..cache_utils import invalidate_school_dashboard

        for school_id in ids:
            invalidate_school_dashboard(school_id)

    transaction.on_commit(_invalidate)


def _bump_nav_context_role_version(user_id):
    """Invalidate cached navigation whenever a school membership changes."""
    if not user_id:
        return
    from django.core.cache import cache

    key = f"navctx:role-version:u{int(user_id)}"
    try:
        cache.incr(key)
    except (ValueError, TypeError):
        cache.set(key, 2, timeout=None)
    except Exception:
        pass


@receiver(post_save, sender=SchoolMembership)
def invalidate_nav_context_after_membership_save(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    _bump_nav_context_role_version(getattr(instance, "teacher_id", None))
    _invalidate_dashboard_after_commit(getattr(instance, "school_id", None))


@receiver(models.signals.post_delete, sender=SchoolMembership)
def invalidate_nav_context_after_membership_delete(sender, instance, **kwargs):
    _bump_nav_context_role_version(getattr(instance, "teacher_id", None))
    _invalidate_dashboard_after_commit(getattr(instance, "school_id", None))


@receiver(post_save, sender=Teacher)
def invalidate_school_dashboards_after_teacher_save(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    school_ids = SchoolMembership.objects.filter(teacher_id=instance.pk).values_list(
        "school_id", flat=True
    )
    _invalidate_dashboard_after_commit(*school_ids)


@receiver(models.signals.pre_delete, sender=Teacher)
def remember_teacher_schools_before_delete(sender, instance, **kwargs):
    instance._dashboard_school_ids = tuple(
        SchoolMembership.objects.filter(teacher_id=instance.pk).values_list("school_id", flat=True)
    )


@receiver(models.signals.post_delete, sender=Teacher)
def invalidate_school_dashboards_after_teacher_delete(sender, instance, **kwargs):
    _invalidate_dashboard_after_commit(*getattr(instance, "_dashboard_school_ids", ()))


@receiver(post_save, sender=Report)
@receiver(models.signals.post_delete, sender=Report)
@receiver(post_save, sender=Ticket)
@receiver(models.signals.post_delete, sender=Ticket)
def invalidate_dashboard_after_school_activity(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    _invalidate_dashboard_after_commit(getattr(instance, "school_id", None))


@receiver(post_save, sender=Department)
@receiver(models.signals.post_delete, sender=Department)
@receiver(post_save, sender=ReportType)
@receiver(models.signals.post_delete, sender=ReportType)
def invalidate_dashboard_after_department_change(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    _invalidate_dashboard_after_commit(getattr(instance, "school_id", None))


# النطاق والتفويض يبطلان القائمة كما تبطلها العضوية.
#
# صار كونتكست التنقل يحمل علماً لكل صلاحية، وهو مخزَّن لعشرين ثانية. فبلا هذين
# الخطّافين يمنح المديرُ الوكيلَ صلاحيةَ المراجعة، ويُحدِّث الوكيل صفحته، فلا
# يرى الرابط — ويُبلغ أن المنح «لم يعمل» وقد عمل. والصلاحية نفسها نافذة لحظةَ
# منحها (``capability_source`` لا يقرأ من هذه الذاكرة)، فالمخفيُّ هو الطريق
# إليها وحده — وهو أسوأ نوع من العطل: صحيحٌ في الجوهر ومعطَّلٌ في الظاهر.
@receiver(post_save, sender=StaffScope)
def invalidate_nav_context_after_scope_save(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    _bump_nav_context_role_version(
        getattr(getattr(instance, "membership", None), "teacher_id", None)
    )


@receiver(models.signals.post_delete, sender=StaffScope)
def invalidate_nav_context_after_scope_delete(sender, instance, **kwargs):
    _bump_nav_context_role_version(
        getattr(getattr(instance, "membership", None), "teacher_id", None)
    )


@receiver(post_save, sender=Delegation)
def invalidate_nav_context_after_delegation_save(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    # المفوَّض إليه هو من تتغيّر قائمته. والسحب حالةُ حفظٍ لا حذف
    # (``revoke`` يكتب ``revoked_at``)، فهذا الخطّاف يغطّيه أيضاً.
    _bump_nav_context_role_version(getattr(instance, "delegate_id", None))


@receiver(models.signals.post_delete, sender=Delegation)
def invalidate_nav_context_after_delegation_delete(sender, instance, **kwargs):
    _bump_nav_context_role_version(getattr(instance, "delegate_id", None))


@receiver(post_save, sender=Report)
def trigger_report_background_tasks(sender, instance, created, **kwargs):
    """
    عند إنشاء تقرير جديد أو تحديثه، نقوم بجدولة المهام في الخلفية وتحديث الكاش.
    """
    if kwargs.get("raw"):
        return

    from django.core.cache import cache
    if instance.school_id:
        cache.delete(f"admin_stats_{instance.school_id}")
    cache.delete("platform_admin_stats")

    from ..tasks import process_report_images
    from ..utils import run_task_safe

    # 1. معالجة الصور (إذا وجدت)
    has_images = any([instance.image1, instance.image2, instance.image3, instance.image4])
    
    if has_images:
        # معالجة الصور فقط (لا نقوم بتوليد PDF).
        # ضغط الصور تحسين وليس شرطًا لصحة التقرير: الصور الأصلية محفوظة وتعمل
        # بدونه. لذلك لا نشغّله داخل الطلب عند تعطّل Celery، لأن ضغط أربع صور
        # بـ Pillow داخل طلب الويب يحوّل عطل الوسيط إلى بطء في كل الصفحات.
        run_task_safe(process_report_images, instance.pk, inline_fallback=False)
    # إذا لم توجد صور: لا يوجد أي مهام مطلوبة هنا


# عدّاد ملحق الأرشفة كان يُعاد حسابه هنا بعد كل حفظ أو حذف لتقرير أو ملف إنجاز
# أو شاهد — ثمانية مواضع لا تغيّر النسخ السنوية أصلًا. صار العدّاد يقيس النسخ
# وحدها، ومكان صيانته الآن ``storage_tracking`` عند تغيّر النسخة نفسها. وإجمالي
# تخزين المدرسة يتولاه ``storage_tracking`` تزايديًا في كل الأحوال.


@receiver(post_save, sender=Ticket)
def trigger_ticket_notifications(sender, instance, created, **kwargs):
    """
    عند إنشاء تذكرة جديدة، نقوم بإرسال إشعارات للمسؤولين المعنيين وتحديث الكاش.
    """
    if kwargs.get("raw"):
        return

    from django.core.cache import cache
    if instance.school_id:
        cache.delete(f"admin_stats_{instance.school_id}")
    cache.delete("platform_admin_stats")

    if not created:
        return

    from ..utils import create_system_notification

    title = f"تذكرة جديدة: {instance.title}"
    message = f"تم إنشاء طلب جديد بواسطة {instance.creator.name}. الحالة: {instance.get_status_display()}"

    if instance.is_platform:
        # تذكرة منصة: إشعار للسوبر يوزر
        superusers = Teacher.objects.filter(is_superuser=True).values_list('id', flat=True)
        if superusers:
            create_system_notification(
                title=f"🆘 دعم فني: {instance.title}",
                message=message,
                teacher_ids=list(superusers),
                is_important=True
            )
    else:
        # تذكرة مدرسة: إشعار للمدير ومسؤول القسم
        recipients = set()
        
        # 1. مدير المدرسة
        if instance.school:
            managers = SchoolMembership.objects.filter(
                school=instance.school,
                role_type=SchoolMembership.RoleType.MANAGER,
                is_active=True
            ).values_list('teacher_id', flat=True)
            recipients.update(managers)

        # 2. مسؤول القسم (إذا تم تحديد قسم)
        if instance.department:
            officers = DepartmentMembership.objects.filter(
                department=instance.department,
                role_type=DepartmentMembership.OFFICER
            ).values_list('teacher_id', flat=True)
            recipients.update(officers)

        if recipients:
            create_system_notification(
                title=title,
                message=message,
                school=instance.school,
                teacher_ids=list(recipients)
            )


@receiver(post_save, sender=TicketImage)
def trigger_ticket_image_processing(sender, instance, created, **kwargs):
    """
    عند رفع صورة تذكرة، نقوم بجدولة معالجتها في الخلفية.
    """
    if kwargs.get("raw"):
        return

    from ..tasks import process_ticket_image
    if instance.image:
        try:
            _pk = instance.pk
            def _enqueue_ticket_image():
                try:
                    from core.trace_context import get_trace_id as _get_trace_id
                    _tid = _get_trace_id()
                except Exception:
                    _tid = None
                if not _tid:
                    import secrets
                    _tid = secrets.token_hex(8)
                process_ticket_image.apply_async(args=[_pk], headers={"trace_id": _tid})
            transaction.on_commit(_enqueue_ticket_image)
        except Exception:
            pass


# =========================
# إبطال كاش تسعير الصفحة الرئيسية
def _clear_landing_pricing_cache(*_args, **_kwargs):
    """Publish plan edits to the landing page immediately.

    The landing pricing context is cached (it runs for every campaign visitor),
    so a price or capacity change must drop that entry instead of waiting for
    the TTL to lapse.
    """
    from django.core.cache import cache

    try:
        from ..views.auth import LANDING_PRICING_CACHE_KEY

        cache.delete(LANDING_PRICING_CACHE_KEY)
    except Exception:
        pass


receiver(post_save, sender=SubscriptionPlan)(_clear_landing_pricing_cache)
receiver(models.signals.post_delete, sender=SubscriptionPlan)(_clear_landing_pricing_cache)


# =========================
# سجل العمليات (Audit Logs)
#
# مسجّلة لكل موديل على حدة عمدًا. الاشتراك العام في ``post_save`` بلا ``sender``
# كان يُستدعى عند كل عملية حفظ في المشروع كله — بما فيها صفوف الجلسات وسجل
# التدقيق نفسه — ليخرج فورًا بعد مقارنة اسم الصنف.
AUDITED_SAVE_MODELS = (
    Report,
    Teacher,
    School,
    Department,
    Ticket,
    SchoolSubscription,
    # ما يمسّ الصلاحيات أولى بالتسجيل من غيره: تغيير عضوية أو دور هو أخطر
    # إجراء إداري في المنصة، وكان يمر بلا أثر.
    SchoolMembership,
    DepartmentMembership,
    # التعميم وثيقة رسمية تُلزم مستلميها، فإنشاؤه وتعديله واقعة تُسجَّل.
    Notification,
    # منح النطاق والتفويض هو منح الصلاحية نفسها. تسجيلهما ليس تفصيلاً: سؤال
    # «مَن منح فلاناً هذه الصلاحية ومتى؟» هو أول ما يُسأل بعد أي خلل.
    StaffScope,
    Delegation,
    # الكيانات التي بُنيت لاحقاً. خطوات اعتمادها مسجَّلة في
    # ``ApprovalTransition``، لكن **إنشاءها وتعديلها وحذفها** كان يمر بلا أثر:
    # مديرٌ يعدّل موعد تكليف أو نصّ محضر لا يترك سطراً في السجل. والانتقال
    # يحكي «كيف تدرّج القرار»، والسجل يحكي «مَن مسّ الشيء ومتى» — وكلاهما
    # مطلوب.
    Assignment,
    AssignmentEvidence,
    Meeting,
    MeetingMinutes,
    Decision,
    Plan,
    Initiative,
    Document,
    CircularDraft,
    # المختبر: العهدة ثقةٌ مادية يُسأل عنها المحضّر، فإضافة صنف وتعديل
    # كميته وتغيير حالته إلى «مفقود» وقائعُ تُسجَّل. وحركةُ العهدة أخصُّ من
    # ذلك: هي الجواب على «من تسلّمه ومتى؟» — وهو أول سؤال عند فقد صنف.
    LabAsset,
    LabAssetHandover,
    LabExperiment,
)

# ما لا يُسجَّل عمداً: صفوف الأبناء التي تتغيّر كثيراً ولا تحمل قراراً —
# ``AssignmentTarget`` (نسبة الإنجاز تُحدَّث عشرات المرات)، وبنود جدول الأعمال،
# وأهداف الخطط ومهامها. تسجيلها يغرق السجل بضجيج يُخفي ما يهم فيه، وحالاتها
# المهمة محفوظة في ``ApprovalTransition`` أو في الكيان الأب المسجَّل.

AUDITED_DELETE_MODELS = (
    Report,
    Teacher,
    School,
    Department,
    Ticket,
    SchoolMembership,
    DepartmentMembership,
    Notification,
    StaffScope,
    Delegation,
    Assignment,
    AssignmentEvidence,
    Meeting,
    MeetingMinutes,
    Decision,
    Plan,
    Initiative,
    Document,
    CircularDraft,
    LabAsset,
    LabAssetHandover,
    LabExperiment,
)


def _audit_actor_snapshot(user, school) -> tuple[str, str]:
    """اسم الفاعل ودوره **لحظة الحدث**.

    الدور يُلتقط لقطةً لا يُشتق عند العرض: من كان معلماً حين حذف تقريراً ثم صار
    مديراً لا يصح أن يظهر السجل وكأن مديراً حذفه. والاشتقاق المتأخر يفعل ذلك
    بالضبط.

    التكلفة مقبولة لأن ``effective_user_role_label`` يخزّن نتيجته على كائن
    المستخدم، فأول كتابة في الطلب تدفع الثمن وما بعدها مجاني.
    """
    name = ""
    role = ""
    try:
        name = (getattr(user, "name", "") or "")[:150]
    except Exception:
        pass
    try:
        from ..permissions import effective_user_role_label

        role = str(effective_user_role_label(user, school) or "")[:64]
    except Exception:
        role = ""
    return name, role


def _audit_school_for(sender, instance):
    """المدرسة التي يُنسب إليها الحدث.

    سجل بلا مدرسة لا يظهر في صفحة أي مدرسة، فهو عملياً سجل ضائع. لذلك نشتق
    المدرسة صراحةً لكل شكل من أشكال الارتباط بدل الاكتفاء بـ ``instance.school``.
    """
    if sender.__name__ == "School":
        return instance

    school = getattr(instance, "school", None)
    if school is not None:
        return school

    # عضوية القسم ترتبط بالمدرسة عبر قسمها.
    department = getattr(instance, "department", None)
    if department is not None:
        resolved = getattr(department, "school", None)
        if resolved is not None:
            return resolved

    # القرار ومحضر الاجتماع يرتبطان بالمدرسة عبر اجتماعهما.
    meeting = getattr(instance, "meeting", None)
    if meeting is not None:
        return getattr(meeting, "school", None)

    # شاهد التكليف يرتبط بها عبر حصة مكلَّفه.
    target = getattr(instance, "target", None)
    if target is not None:
        return getattr(target, "school", None)

    return None


def _audit_write(sender, instance, action, *, school_override=None) -> None:
    """كتابة صف سجل واحد — نقطة واحدة تمر بها كل الإشارات."""
    from ..middleware import get_current_request, is_audit_logging_suppressed

    if is_audit_logging_suppressed():
        return

    request = get_current_request()
    if not request or not request.user.is_authenticated:
        return

    school = school_override if school_override is not None else _audit_school_for(sender, instance)

    actor_name, actor_role = _audit_actor_snapshot(request.user, school)

    AuditLog.objects.create(
        school=school,
        teacher=request.user,
        actor_name=actor_name,
        actor_role=actor_role,
        action=action,
        model_name=sender.__name__,
        object_id=getattr(instance, "pk", None),
        object_repr=str(instance)[:255],
        changes={},
        ip_address=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
    )


def audit_log_save(sender, instance, created, **kwargs):
    if kwargs.get("raw"):
        return
    _audit_write(
        sender,
        instance,
        AuditLog.Action.CREATE if created else AuditLog.Action.UPDATE,
    )


def audit_log_delete(sender, instance, **kwargs):
    _audit_write(sender, instance, AuditLog.Action.DELETE)



for _audited_model in AUDITED_SAVE_MODELS:
    post_save.connect(
        audit_log_save,
        sender=_audited_model,
        dispatch_uid=f"audit_log_save:{_audited_model.__name__}",
    )

for _audited_model in AUDITED_DELETE_MODELS:
    models.signals.post_delete.connect(
        audit_log_delete,
        sender=_audited_model,
        dispatch_uid=f"audit_log_delete:{_audited_model.__name__}",
    )
