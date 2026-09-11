import threading
import secrets
from django.db import transaction
from django.conf import settings
from django.apps import apps
import logging

from core.observability import report_degraded as _degraded, soft_fail

from core.trace_context import get_trace_id

logger = logging.getLogger(__name__)

def run_task_safe(
    task_func,
    *args,
    force_thread: bool = False,
    inline_fallback: bool = True,
    **kwargs,
):
    """
    محاولة تشغيل المهمة عبر Celery، وإذا فشل (بسبب عدم وجود Redis مثلاً)
    يتم تشغيلها بشكل آمن بدون كسر النظام.

    سياسة التنفيذ:
    - في التطوير (DEBUG=True) أو force_thread=True: نسمح بالـ Thread fallback.
    - في الإنتاج: نتجنب Threads (غير موثوقة مع worker/dyno) ونستخدم تنفيذ inline عند تعذر Celery.

    ``inline_fallback=False`` يمنع التنفيذ داخل الطلب نهائيًا. استخدمه للمهام
    الثقيلة التي تُحسّن النتيجة ولا تُنتجها: إن تعطّل Celery فإن تشغيلها داخل
    الطلب يحوّل بطئًا في مهمة خلفية إلى بطء في كل صفحة، وقد يستنفد عمال الويب.
    المهام التي بدونها تضيع بيانات (مثل إنشاء مستلمي الإشعارات) تبقى
    ``inline_fallback=True``.
    """
    def _thread_wrapper(func, *f_args, **f_kwargs):
        from django.db import connections
        # إغلاق أي اتصالات قديمة موروثة لضمان فتح اتصال جديد نظيف في هذا الـ Thread
        connections.close_all()
        try:
            func(*f_args, **f_kwargs)
        finally:
            # إغلاق الاتصال بعد الانتهاء لتجنب تسريب الاتصالات (Connection Leaks)
            connections.close_all()

    def _execute():
        debug_mode = bool(getattr(settings, 'DEBUG', False))
        allow_thread = bool(force_thread) or debug_mode
        trace_id = get_trace_id() or secrets.token_hex(8)

        # 1) Prefer Celery
        try:
            if hasattr(task_func, "apply_async"):
                async_result = task_func.apply_async(
                    args=args,
                    kwargs=kwargs,
                    headers={"trace_id": trace_id},
                )
            else:
                async_result = task_func.delay(*args, **kwargs)
            logger.info(
                "Task queued via Celery task=%s task_id=%s trace_id=%s args_count=%s",
                getattr(task_func, "__name__", str(task_func)),
                getattr(async_result, "id", None),
                trace_id,
                len(args),
            )
            return
        except Exception as e:
            logger.warning(
                "Celery enqueue failed task=%s trace_id=%s error=%s",
                getattr(task_func, "__name__", str(task_func)),
                trace_id,
                e,
            )
            # A broker outage degrades the whole platform silently otherwise;
            # surface it as a counter so alerting can see it immediately.
            with soft_fail("celery.count_enqueue_failure"):
                from core import opmetrics

                opmetrics.increment("celery.enqueue.failed")

        # 2) Development fallback: background thread
        if allow_thread:
            thread = threading.Thread(target=_thread_wrapper, args=(task_func, *args), kwargs=kwargs)
            thread.daemon = True
            thread.start()
            logger.info(
                "Task fallback thread-started task=%s trace_id=%s debug=%s force_thread=%s",
                getattr(task_func, "__name__", str(task_func)),
                trace_id,
                debug_mode,
                force_thread,
            )
            return

        # 3) Production-safe fallback: inline execution
        if not inline_fallback:
            logger.error(
                "Task dropped (inline fallback disabled) task=%s trace_id=%s args_count=%s",
                getattr(task_func, "__name__", str(task_func)),
                trace_id,
                len(args),
            )
            with soft_fail("celery.count_dropped_task"):
                from core import opmetrics

                opmetrics.increment("celery.task.dropped")
            return

        try:
            if hasattr(task_func, "apply"):
                task_func.apply(args=args, kwargs=kwargs, throw=True)
            else:
                task_func(*args, **kwargs)
            logger.info(
                "Task executed inline fallback task=%s trace_id=%s args_count=%s",
                getattr(task_func, "__name__", str(task_func)),
                trace_id,
                len(args),
            )
        except Exception:
            logger.exception("Task %s failed even with inline fallback.", getattr(task_func, "__name__", str(task_func)))

    # تنفيذ العملية بعد التأكد من حفظ البيانات في قاعدة البيانات
    transaction.on_commit(_execute)

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
