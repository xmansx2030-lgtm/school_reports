from django.conf import settings
from django.apps import apps
import logging

from core.observability import report_degraded as _degraded

from .task_dispatch import run_named_task_safe as run_named_task_safe
from .task_dispatch import run_task_safe as run_task_safe

logger = logging.getLogger(__name__)

def _resolve_department_for_category(cat, school=None):
    """يستخرج كائن القسم المرتبط بالتصنيف (إن وُجد) مع مراعاة عزل المدارس.

    عند وجود أكثر من مدرسة، قد تكون نفس أنواع التقارير/العلاقات موجودة في أكثر من مدرسة.
    لذلك إن كان لدينا school (أو كان cat مرتبطًا بحقل school) سنحاول أولاً حل القسم داخل هذه المدرسة،
    ثم نسمح بالرجوع لقسم عام (school=NULL) كخيار احتياطي.
    """
    Department = apps.get_model('reports', 'Department')
    if not cat or Department is None:
        return None

    school_scope = school
    try:
        if school_scope is None:
            school_scope = getattr(cat, "school", None)
    except Exception:
        school_scope = school

    # 1) علاقة مباشرة cat.department (إن وُجدت)
    try:
        d = getattr(cat, "department", None)
        if d:
            if school_scope is not None and hasattr(d, "school"):
                try:
                    ds = getattr(d, "school", None)
                    # إن كان القسم يخص مدرسة أخرى، نتجاهله
                    if ds is not None and ds != school_scope:
                        d = None
                except Exception:
                    _degraded("notifications.department_scope_check")
                    d = None
            if d:
                return d
    except Exception:
        _degraded("notifications.resolve_target_department")

    # 2) علاقات M2M شائعة: departments / depts / dept_list
    for rel_name in ("departments", "depts", "dept_list"):
        rel = getattr(cat, rel_name, None)
        if rel is not None:
            try:
                qs = rel.all()
                if school_scope is not None and hasattr(Department, "school"):
                    # نفضّل قسم المدرسة، ثم قسم عام
                    d = qs.filter(school=school_scope).first() or qs.filter(school__isnull=True).first()
                else:
                    d = qs.first()
                if d:
                    return d
            except Exception:
                _degraded("notifications.resolve_department_by_slug")

    # 3) استعلام احتياطي
    try:
        qs = Department.objects.filter(reporttypes=cat)
        if school_scope is not None and hasattr(Department, "school"):
            return qs.filter(school=school_scope).first() or qs.filter(school__isnull=True).first()
        return qs.first()
    except Exception:
        return None

def _build_head_decision(dept):
    """
    يُرجع dict يحدّد ماذا نطبع في خانة (اعتماد رئيس القسم).
    """
    DepartmentMembership = apps.get_model('reports', 'DepartmentMembership')
    if not dept or DepartmentMembership is None:
        return {"no_render": True}

    try:
        role_officer = getattr(DepartmentMembership, "OFFICER", "officer")
        qs = (DepartmentMembership.objects
              .select_related("teacher")
              .filter(department=dept, role_type=role_officer, teacher__is_active=True))
        heads = [m.teacher for m in qs]
    except Exception:
        heads = []

    count = len(heads)
    policy = getattr(settings, "PRINT_MULTIHEAD_POLICY", "blank")  # "blank" أو "dept"

    if count == 1:
        return {"single": True, "name": getattr(heads[0], "name", str(heads[0]))}

    if policy == "dept":
        return {"multi_dept": True, "dept_name": getattr(dept, "name", "")}

    return {"multi_blank": True}

def create_system_notification(title, message, school=None, teacher_ids=None, is_important=False):
    """
    Helper to create a notification and trigger the background task to send it.
    """
    from .models import Notification
    from .tasks import send_notification_task

    n = Notification.objects.create(
        title=title,
        message=message,
        school=school,
        is_important=is_important
    )
    
    run_task_safe(send_notification_task, n.pk, teacher_ids)
    return n
