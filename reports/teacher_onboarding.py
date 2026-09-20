from __future__ import annotations

import csv
import io
import re
import secrets
import time
from collections.abc import Iterable
from typing import Any

import openpyxl
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import transaction

from .gender_labels import school_gender_labels
from .models import Department, DepartmentMembership, School, SchoolMembership, Teacher
from .lab_kinds import LabKind
from .staff_assignments import get_assignment


PREVIEW_SESSION_KEY = "teacher_onboarding_preview"
RESULT_SESSION_KEY = "teacher_onboarding_result"
PREVIEW_MAX_AGE_SECONDS = 30 * 60
MAX_IMPORT_ROWS = 2000
MAX_IMPORT_FILE_BYTES = 10 * 1024 * 1024


def normalize_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def normalize_phone(value: Any) -> str:
    raw = normalize_text(value)
    digits = re.sub(r"\D+", "", raw)
    if digits.startswith("966"):
        digits = digits[3:]
    if len(digits) == 9 and digits.startswith("5"):
        digits = "0" + digits
    return digits


def normalize_national_id(value: Any) -> str:
    return re.sub(r"\D+", "", normalize_text(value))


def normalize_header(value: Any) -> str:
    value = normalize_text(value).lower()
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[\-_/\\]+", "", value)


def normalize_job_title(value: Any) -> str | None:
    raw = normalize_text(value).lower()
    if not raw:
        return SchoolMembership.JobTitle.TEACHER
    if raw in SchoolMembership.JobTitle.values or raw in SchoolMembership.RoleType.values:
        return raw
    compact = re.sub(r"\s+", "", raw)
    if "وكيل" in compact or "وكيلة" in compact or "deputy" in compact:
        return SchoolMembership.RoleType.DEPUTY
    if "مختبر" in compact or "lab" in compact:
        return SchoolMembership.JobTitle.LAB_TECH
    if "إدار" in compact or "ادار" in compact or "admin" in compact:
        return SchoolMembership.JobTitle.ADMIN_STAFF
    if "معلم" in compact or "teacher" in compact:
        return SchoolMembership.JobTitle.TEACHER
    return None


def job_title_label(value: str, school: School) -> str:
    gender_labels = school_gender_labels(school)
    try:
        assignment = get_assignment(value)
        return str(gender_labels[assignment.label_key])
    except Exception:
        return str(gender_labels["teacher_indefinite"])


def normalize_lab_kind(value: Any) -> str:
    raw = normalize_text(value).lower()
    if raw in LabKind.values:
        return raw
    compact = re.sub(r"\s+", "", raw)
    if "حاسب" in compact or "computer" in compact:
        return LabKind.COMPUTER
    if "علوم" in compact or "science" in compact:
        return LabKind.SCIENCE
    return ""


def available_departments(school: School):
    return Department.objects.filter(school=school, is_active=True).order_by("name", "id")


def _department_maps(school: School) -> tuple[dict[str, Department], dict[int, Department]]:
    by_text: dict[str, Department] = {}
    by_id: dict[int, Department] = {}
    for department in available_departments(school):
        by_id[int(department.pk)] = department
        for key in (department.name, department.slug):
            normalized = normalize_header(key)
            if normalized:
                by_text[normalized] = department
    return by_text, by_id


def rows_from_quick_post(post) -> list[dict[str, Any]]:
    names = post.getlist("name")
    phones = post.getlist("phone")
    national_ids = post.getlist("national_id")
    job_titles = post.getlist("job_title")
    department_ids = post.getlist("department")
    lab_kinds = post.getlist("lab_kind")
    row_count = max(
        len(names),
        len(phones),
        len(national_ids),
        len(job_titles),
        len(department_ids),
        len(lab_kinds),
        0,
    )
    rows: list[dict[str, Any]] = []
    for index in range(row_count):
        values = {
            "name": names[index] if index < len(names) else "",
            "phone": phones[index] if index < len(phones) else "",
            "national_id": national_ids[index] if index < len(national_ids) else "",
            "job_title": job_titles[index] if index < len(job_titles) else "",
            "department": department_ids[index] if index < len(department_ids) else "",
            "lab_kind": lab_kinds[index] if index < len(lab_kinds) else "",
        }
        # لا نعدّ المسمى الافتراضي وحده صفًا مدخلًا؛ الجدول يعرض عدة
        # صفوف فارغة جاهزة، وكلها تحمل افتراضيًا قيمة "معلم".
        has_user_data = any(
            normalize_text(values[field])
            for field in ("name", "phone", "national_id", "department", "lab_kind")
        )
        if has_user_data:
            rows.append({"row_number": index + 1, **values})
            if len(rows) > MAX_IMPORT_ROWS:
                raise ValueError("too_many_rows")

    pasted = normalize_text(post.get("pasted_rows"))
    if pasted:
        start = len(rows) + 1
        for offset, line in enumerate(pasted.splitlines()):
            if not line.strip():
                continue
            columns = next(csv.reader([line], delimiter="\t"))
            columns += [""] * (6 - len(columns))
            rows.append(
                {
                    "row_number": start + offset,
                    "name": columns[0],
                    "phone": columns[1],
                    "national_id": columns[2],
                    "job_title": columns[3],
                    "department": columns[4],
                    "lab_kind": columns[5],
                }
            )
            if len(rows) > MAX_IMPORT_ROWS:
                raise ValueError("too_many_rows")
    return rows


def rows_from_uploaded_file(uploaded_file) -> list[dict[str, Any]]:
    filename = normalize_text(getattr(uploaded_file, "name", "")).lower()
    if int(getattr(uploaded_file, "size", 0) or 0) > MAX_IMPORT_FILE_BYTES:
        raise ValueError("file_too_large")
    if filename.endswith(".xlsx"):
        workbook = openpyxl.load_workbook(uploaded_file, read_only=True, data_only=True)
        sheet = workbook.active
        row_iterator = iter(sheet.iter_rows(values_only=True))
    elif filename.endswith(".csv"):
        raw = uploaded_file.read()
        text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else str(raw)
        row_iterator = iter(csv.reader(io.StringIO(text)))
    else:
        raise ValueError("unsupported_file")

    header_row = next(row_iterator, None)
    if header_row is None:
        return []
    headers = [normalize_header(value) for value in (header_row or ())]

    aliases = {
        "name": ("الاسمالكامل", "الاسم", "اسم", "name"),
        "phone": ("رقمالجوال", "الجوال", "رقمالهاتف", "الهاتف", "phone", "mobile"),
        "national_id": ("رقمالهوية", "الهوية", "السجلالمدني", "nationalid"),
        "job_title": ("المسمىالوظيفي", "المسمى", "الدور", "jobtitle", "role"),
        "department": ("القسم", "اسم القسم", "department"),
        "lab_kind": ("المختبر", "نوع المختبر", "lab"),
    }

    indices: dict[str, int | None] = {}
    for field, candidates in aliases.items():
        indices[field] = next(
            (
                index
                for index, header in enumerate(headers)
                if any(normalize_header(candidate) in header for candidate in candidates)
            ),
            None,
        )

    if indices["name"] is None or indices["phone"] is None:
        raise ValueError("required_headers_missing")

    rows: list[dict[str, Any]] = []
    for row_number, values in enumerate(row_iterator, start=2):
        if len(rows) >= MAX_IMPORT_ROWS:
            raise ValueError("too_many_rows")
        values = values or ()

        def cell(field: str) -> Any:
            index = indices[field]
            return values[index] if index is not None and index < len(values) else ""

        row = {
            "row_number": row_number,
            "name": cell("name"),
            "phone": cell("phone"),
            "national_id": cell("national_id"),
            "job_title": cell("job_title"),
            "department": cell("department"),
            "lab_kind": cell("lab_kind"),
        }
        if any(normalize_text(value) for key, value in row.items() if key != "row_number"):
            rows.append(row)
    return rows


def _membership_capacity(school: School) -> dict[str, int]:
    """المتاح من مقاعد الباقة قبل الاستيراد.

    العدّ من ``seats_used`` وحده: عدّ صفوف ``TEACHER`` يُبقي وكلاء المدرسة
    وموظفيها خارج الحساب، فيَعِد الاستيرادُ بمتّسعٍ لا وجود له ثم يفشل عند
    الحفظ، أو يمرّ فتتجاوز المدرسة باقتها من هذا الباب وحده.
    """
    subscription = getattr(school, "subscription", None)
    maximum = int(getattr(subscription, "teacher_limit", 0) or 0)
    current = SchoolMembership.seats_used(school)
    remaining = max(maximum - current, 0) if maximum > 0 else 0
    return {"maximum": maximum, "current": current, "remaining": remaining}


def build_preview(raw_rows: Iterable[dict[str, Any]], school: School) -> dict[str, Any]:
    raw_rows = list(raw_rows)
    by_department_text, by_department_id = _department_maps(school)
    normalized_phones = {normalize_phone(row.get("phone")) for row in raw_rows}
    normalized_phones.discard("")
    normalized_national_ids = {
        normalize_national_id(row.get("national_id"))
        for row in raw_rows
        if normalize_national_id(row.get("national_id"))
    }
    teachers_by_phone = {
        teacher.phone: teacher
        for teacher in Teacher.objects.filter(phone__in=normalized_phones).only(
            "id", "name", "phone", "national_id", "is_active"
        )
    }
    national_owners = {
        teacher.national_id: teacher
        for teacher in Teacher.objects.filter(national_id__in=normalized_national_ids).only(
            "id", "name", "phone", "national_id"
        )
        if teacher.national_id
    }
    # عضوية المنسوب أياً كان دورها. البحث عن ``TEACHER`` وحده كان يجعل محضّر
    # المختبر المُسنَد من شاشة الأدوار يبدو غريباً عن المدرسة، فيُعرض «ربط
    # جديد» ويُحسَب مقعداً ثانياً لرجلٍ فيها أصلاً.
    memberships: dict[int, SchoolMembership] = {}
    for membership in SchoolMembership.objects.filter(
        school=school,
        teacher_id__in=[teacher.id for teacher in teachers_by_phone.values()],
        role_type__in=SchoolMembership.STAFF_ROLES,
    ).only("id", "teacher_id", "is_active", "job_title").order_by("role_type", "id"):
        # ``setdefault`` لا الإسناد: من يحمل دورين يجب أن يعود منه الصفّ نفسه
        # في كل مرة، لا الذي صادف أن جاء أخيراً.
        memberships.setdefault(membership.teacher_id, membership)

    seen_phones: set[str] = set()
    seen_national_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    summary = {
        "total": 0,
        "new": 0,
        "link": 0,
        "reactivate": 0,
        "existing": 0,
        "invalid": 0,
        "warnings": 0,
    }

    for position, source in enumerate(raw_rows, start=1):
        name = normalize_text(source.get("name"))
        phone = normalize_phone(source.get("phone"))
        national_id = normalize_national_id(source.get("national_id"))
        job_title = normalize_job_title(source.get("job_title"))
        department_raw = normalize_text(
            source.get("department")
            if source.get("department") not in (None, "")
            else source.get("department_id")
        )
        lab_kind_raw = normalize_text(
            source.get("lab_kind")
            if source.get("lab_kind") not in (None, "")
            else ""
        )
        errors: list[str] = []
        warnings: list[str] = []

        if not name:
            errors.append("الاسم الكامل مطلوب.")
        elif len(name) > 150:
            errors.append("الاسم الكامل يجب ألا يتجاوز 150 حرفًا.")
        if not re.fullmatch(r"05\d{8}", phone):
            errors.append("رقم الجوال يجب أن يبدأ بـ 05 ويتكون من 10 أرقام.")
        if national_id and not re.fullmatch(r"\d{10}", national_id):
            errors.append("رقم الهوية يجب أن يتكون من 10 أرقام.")
        assignment_valid = job_title is not None
        if assignment_valid:
            try:
                get_assignment(job_title)
            except ValidationError:
                assignment_valid = False
        if not assignment_valid:
            errors.append("التكليف غير مدعوم في الاستيراد الجماعي.")
            # Keep the unsupported input in the session: confirmation rebuilds
            # the preview and must not reinterpret this row as a teacher.
            job_title = job_title or normalize_text(source.get("job_title"))
        if phone and phone in seen_phones:
            errors.append("رقم الجوال مكرر داخل القائمة.")
        if phone:
            seen_phones.add(phone)
        if national_id and national_id in seen_national_ids:
            errors.append("رقم الهوية مكرر داخل القائمة.")
        if national_id:
            seen_national_ids.add(national_id)

        department = None
        if department_raw:
            if department_raw.isdigit():
                department = by_department_id.get(int(department_raw))
            if department is None:
                department = by_department_text.get(normalize_header(department_raw))
            if department is None:
                errors.append("القسم غير موجود في المدرسة الحالية.")

        lab_kind = normalize_lab_kind(lab_kind_raw)
        if lab_kind_raw and not lab_kind:
            errors.append("المختبر غير معروف؛ اختر مختبر العلوم أو مختبر الحاسب الآلي.")
        if job_title == SchoolMembership.JobTitle.LAB_TECH and not lab_kind:
            errors.append("محضّر المختبر يجب ربطه بمختبر العلوم أو مختبر الحاسب الآلي.")
        if job_title != SchoolMembership.JobTitle.LAB_TECH:
            lab_kind = ""

        teacher = teachers_by_phone.get(phone)
        membership = memberships.get(teacher.id) if teacher else None
        state = "new"
        state_label = "حساب جديد"
        national_owner = national_owners.get(national_id) if national_id else None

        if teacher is not None:
            if not teacher.is_active:
                errors.append("الحساب الموجود موقوف على مستوى المنصة؛ يلزم مراجعته قبل ربطه.")
            if name and teacher.name and name != teacher.name:
                warnings.append(f"سيُربط بالحساب الموجود باسم «{teacher.name}» دون تغيير اسمه.")
            if national_id and national_id != (teacher.national_id or ""):
                warnings.append("رقم الهوية مختلف عن الحساب الموجود ولن يتم تغييره.")
            if membership is None:
                state, state_label = "link", "ربط حساب موجود"
            elif not membership.is_active:
                state, state_label = "reactivate", "إعادة تفعيل العضوية"
            else:
                state, state_label = "existing", "مرتبط مسبقًا"
        if national_owner is not None and (
            teacher is None or int(national_owner.pk) != int(teacher.pk)
        ):
            errors.append(
                "رقم الهوية مرتبط بحساب آخر جواله ينتهي بـ "
                f"{national_owner.phone[-4:] if national_owner.phone else '—'}."
            )

        if errors:
            state, state_label = "invalid", "يحتاج تصحيحًا"

        row = {
            "row_number": int(source.get("row_number") or position),
            "name": name,
            "phone": phone,
            "national_id": national_id,
            "job_title": job_title,
            "job_title_label": (
                job_title_label(job_title, school) if assignment_valid else "تكليف غير مدعوم"
            ),
            "department_id": int(department.pk) if department else None,
            "department_name": department.name if department else "",
            "lab_kind": lab_kind,
            "lab_kind_label": dict(LabKind.choices).get(lab_kind, ""),
            "state": state,
            "state_label": state_label,
            "errors": errors,
            "warnings": warnings,
        }
        rows.append(row)
        summary["total"] += 1
        summary[state] += 1
        summary["warnings"] += len(warnings)

    capacity = _membership_capacity(school)
    membership_additions = summary["new"] + summary["link"]
    capacity_error = ""
    if capacity["maximum"] > 0 and membership_additions > capacity["remaining"]:
        capacity_error = (
            f"العملية تحتاج {membership_additions} مقاعد، والمتبقي في الباقة "
            f"{capacity['remaining']} فقط."
        )

    return {
        "rows": rows,
        "summary": summary,
        "capacity": capacity,
        "capacity_error": capacity_error,
        "can_confirm": bool(rows and not summary["invalid"] and not capacity_error),
    }


def save_preview(request, preview: dict[str, Any], school: School, *, source: str) -> str:
    token = secrets.token_urlsafe(18)
    request.session[PREVIEW_SESSION_KEY] = {
        "token": token,
        "user_id": int(request.user.pk),
        "school_id": int(school.pk),
        "created_at": int(time.time()),
        "source": source,
        **preview,
    }
    request.session.pop(RESULT_SESSION_KEY, None)
    return token


def load_preview(request, school: School) -> dict[str, Any] | None:
    preview = request.session.get(PREVIEW_SESSION_KEY)
    if not isinstance(preview, dict):
        return None
    valid_owner = (
        int(preview.get("user_id") or 0) == int(request.user.pk)
        and int(preview.get("school_id") or 0) == int(school.pk)
    )
    fresh = int(time.time()) - int(preview.get("created_at") or 0) <= PREVIEW_MAX_AGE_SECONDS
    if not valid_owner or not fresh:
        request.session.pop(PREVIEW_SESSION_KEY, None)
        return None
    return preview


def clear_preview(request) -> None:
    request.session.pop(PREVIEW_SESSION_KEY, None)


def confirm_preview(request, school: School, token: str) -> dict[str, Any]:
    stored = load_preview(request, school)
    if stored is None or not secrets.compare_digest(str(stored.get("token") or ""), str(token or "")):
        raise ValueError("preview_expired")

    # Re-run validation immediately before writing so concurrent changes and
    # subscription capacity are accounted for.
    fresh = build_preview(stored.get("rows") or [], school)
    if not fresh["can_confirm"]:
        save_preview(request, fresh, school, source=str(stored.get("source") or "import"))
        raise ValueError("preview_changed")

    created_rows: list[dict[str, str]] = []
    linked_count = 0
    reactivated_count = 0
    existing_count = 0

    with transaction.atomic():
        for row in fresh["rows"]:
            phone = row["phone"]
            teacher = Teacher.objects.filter(phone=phone).first()
            if teacher is None:
                teacher = Teacher.objects.create(
                    name=row["name"],
                    phone=phone,
                    national_id=row["national_id"] or None,
                    password=make_password(phone),
                    is_active=True,
                )
                created_rows.append(
                    {
                        "name": teacher.name,
                        "phone": teacher.phone,
                        "temporary_password": teacher.phone,
                    }
                )

            # الاختيار الظاهر للمدير يترجم إلى دور وصفي ومسمى تنظيمي من
            # كتالوج واحد تستخدمه شاشة الإضافة الفردية وشاشة الأدوار.
            assignment = get_assignment(row["job_title"])
            membership, membership_created = SchoolMembership.objects.get_or_create(
                school=school,
                teacher=teacher,
                role_type=assignment.role_type,
                defaults={"is_active": True},
            )
            if assignment.job_title and membership_created:
                membership.job_title = assignment.job_title
                membership.lab_kind = row.get("lab_kind") or ""
                membership.save(update_fields=["job_title", "lab_kind"])
            if membership_created:
                if row["state"] == "link":
                    linked_count += 1
            else:
                changed_fields: list[str] = []
                if not membership.is_active:
                    membership.is_active = True
                    changed_fields.append("is_active")
                    reactivated_count += 1
                else:
                    existing_count += 1
                if assignment.job_title and membership.job_title != assignment.job_title:
                    membership.job_title = assignment.job_title
                    changed_fields.append("job_title")
                wanted_lab_kind = row.get("lab_kind") or ""
                if membership.lab_kind != wanted_lab_kind:
                    membership.lab_kind = wanted_lab_kind
                    changed_fields.append("lab_kind")
                if changed_fields:
                    membership.save(update_fields=changed_fields)

            department_id = row.get("department_id")
            if department_id:
                department = Department.objects.get(
                    pk=department_id, school=school, is_active=True
                )
                DepartmentMembership.objects.get_or_create(
                    department=department,
                    teacher=teacher,
                    defaults={"role_type": DepartmentMembership.TEACHER},
                )

    result = {
        "token": secrets.token_urlsafe(18),
        "user_id": int(request.user.pk),
        "school_id": int(school.pk),
        "created_at": int(time.time()),
        "created": len(created_rows),
        "linked": linked_count,
        "reactivated": reactivated_count,
        "existing": existing_count,
        "created_rows": created_rows,
        "total": len(fresh["rows"]),
    }
    request.session[RESULT_SESSION_KEY] = result
    clear_preview(request)
    return result


def load_result(request, school: School | None = None) -> dict[str, Any] | None:
    result = request.session.get(RESULT_SESSION_KEY)
    if not isinstance(result, dict):
        return None
    valid_user = int(result.get("user_id") or 0) == int(request.user.pk)
    valid_school = school is None or int(result.get("school_id") or 0) == int(school.pk)
    fresh = int(time.time()) - int(result.get("created_at") or 0) <= PREVIEW_MAX_AGE_SECONDS
    if not valid_user or not valid_school or not fresh:
        request.session.pop(RESULT_SESSION_KEY, None)
        return None
    return result
