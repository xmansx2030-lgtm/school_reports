from __future__ import annotations

import re

from django.db.models.functions import Lower

from .base import *
from ..lab_kinds import LabKind


_ARABIC_DIACRITICS_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_SCHOOL_NAME_TRANSLATION = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ئ": "ي",
        "ؤ": "و",
        "ة": "ه",
        "ـ": "",
    }
)


def normalize_school_name_identity(value: str) -> str:
    """Return a stable comparison key for school names."""
    text = (value or "").strip().casefold()
    text = text.translate(_SCHOOL_NAME_TRANSLATION)
    text = _ARABIC_DIACRITICS_RE.sub("", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text, flags=re.UNICODE).strip()


def normalize_sa_mobile_identity(value: str) -> str:
    """Normalize common Saudi mobile formats to 05XXXXXXXX when possible."""
    phone = (value or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    phone = "".join(character for character in phone if character.isdigit())
    if phone.startswith("9665") and len(phone) == 12:
        return f"0{phone[3:]}"
    if phone.startswith("5") and len(phone) == 9:
        return f"0{phone}"
    return phone


class SchoolGroup(models.Model):
    """مجموعة المدارس المتكاملة التي يقودها مدير تنفيذي.

    النموذج التنظيمي الذي تبني عليه هذه الفئة: كل مدرسة تحتفظ بمديرها وبكامل
    صلاحياته الإدارية والتنفيذية، ويقود المدير التنفيذي المجموعة إشرافاً
    ومتابعةً دون أن يتولى الإدارة اليومية لأي مدرسة. ولذلك تُنمذَج المجموعة
    طبقةً *فوق* المدرسة لا بديلاً عنها: لا يملك هذا الكيان أي بيانات تشغيلية،
    وحذفه لا يمس مدرسة واحدة.

    عدد المدارس في المجموعة غير مرمَّز عمداً — يُشتق مما يُربط بها فعلاً، فأي
    رقم ثابت في الكود يصير خطأً عند أول تعديل تنظيمي.
    """

    name = models.CharField("اسم المجموعة", max_length=200)
    code = models.SlugField(
        "المعرّف (code)",
        max_length=64,
        unique=True,
        help_text="كود قصير لتمييز المجموعة.",
    )
    education_department = models.CharField(
        "إدارة التعليم",
        max_length=200,
        blank=True,
        default="",
        help_text="الارتباط التنظيمي للمدير التنفيذي.",
    )
    headquarters_school = models.ForeignKey(
        "School",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="headquartered_groups",
        verbose_name="مدرسة المقر",
        help_text="المدرسة التي يتخذها المدير التنفيذي مقراً له.",
    )
    is_active = models.BooleanField("نشطة؟", default=True)
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)

    class Meta:
        verbose_name = "مجموعة مدارس متكاملة"
        verbose_name_plural = "مجموعات المدارس المتكاملة"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        super().clean()
        # مقر المدير التنفيذي يقع داخل إحدى مدارس مجموعته، فمقرٌّ خارجها خطأ إدخال.
        school = self.headquarters_school
        if school is not None and self.pk and school.group_id != self.pk:
            raise ValidationError(
                {"headquarters_school": "مدرسة المقر يجب أن تكون ضمن مدارس هذه المجموعة."}
            )

    @property
    def active_schools(self):
        return self.schools.filter(is_active=True)


class School(models.Model):
    name = models.CharField("اسم المدرسة", max_length=200)
    normalized_name = models.CharField(
        "اسم المدرسة للتقييد",
        max_length=220,
        blank=True,
        default="",
        editable=False,
        db_index=True,
        help_text="نسخة مطبّعة من الاسم لمنع تسجيل المدرسة أكثر من مرة.",
    )
    class Stage(models.TextChoices):
        KG = "kg", "رياض أطفال"
        PRIMARY = "primary", "ابتدائي"
        MIDDLE = "middle", "متوسط"
        HIGH = "high", "ثانوي"

    class Gender(models.TextChoices):
        BOYS = "boys", "بنين"
        GIRLS = "girls", "بنات"

    code = models.SlugField(
        "المعرّف (code)",
        max_length=64,
        unique=True,
        help_text="كود قصير لتمييز المدرسة، يُستخدم في الاختيار والتقارير.",
    )
    storage_key = models.SlugField(
        "مفتاح مجلد التخزين",
        max_length=96,
        unique=True,
        editable=False,
        help_text="معرّف ثابت لمجلد المدرسة في التخزين، ولا يتغير عند تعديل رمز المدرسة.",
    )
    stage = models.CharField(
        "المرحلة",
        max_length=16,
        choices=Stage.choices,
        default=Stage.PRIMARY,
    )
    gender = models.CharField(
        "بنين / بنات",
        max_length=8,
        choices=Gender.choices,
        default=Gender.BOYS,
    )
    phone = models.CharField("رقم الجوال", max_length=20, blank=True, null=True)
    email = models.EmailField("البريد الإلكتروني", blank=True, default="")
    city = models.CharField("المدينة", max_length=120, blank=True, null=True)
    is_active = models.BooleanField("نشطة؟", default=True)
    group = models.ForeignKey(
        SchoolGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="schools",
        verbose_name="مجموعة المدارس المتكاملة",
        help_text="اختياري — المدارس المستقلة تُترك بلا مجموعة.",
    )
    print_primary_color = models.CharField(
        "لون قالب الطباعة",
        max_length=9,
        blank=True,
        null=True,
        help_text="لون رئيسي لقالب الطباعة (مثلاً #2563eb).",
    )
    share_link_default_days = models.PositiveSmallIntegerField(
        "مدة صلاحية الروابط الافتراضية (بالأيام)",
        default=7,
        help_text="المدة الافتراضية لروابط مشاركة التقارير/ملفات الإنجاز لهذه المدرسة.",
    )
    allowed_academic_years = models.JSONField(
        "السنوات الدراسية المتاحة/المقبولة",
        default=list,
        blank=True,
        help_text="قائمة بالسنوات الدراسية (هجري) التي تظهر للمعلم عند إنشاء ملف إنجاز.",
    )
    current_academic_year = models.CharField(
        "السنة الدراسية الحالية (هجري)",
        max_length=9,
        blank=True,
        default="",
        help_text="مثال: 1447-1448. تُستخدم لتصنيف التقارير الجديدة وأرشفة السنوات.",
        db_index=True,
    )
    report_approval_enabled = models.BooleanField(
        "تفعيل دورة اعتماد التقارير",
        default=False,
        help_text=(
            "عند تفعيلها يُنشأ التقرير مسودةً ويمر بالمراجعة والاعتماد قبل أن "
            "يصير نهائياً. وعند إيقافها يبقى التقرير نهائياً بمجرد حفظه."
        ),
    )
    storage_used_bytes = models.PositiveBigIntegerField(
        "إجمالي التخزين المستخدم (بايت)",
        default=0,
        help_text="إجمالي تزايدي لحجم ملفات المدرسة (تقارير + ملفات إنجاز + شواهد). يُحدّث تلقائيًا.",
    )
    extra_storage_gb = models.PositiveIntegerField(
        "مساحة تخزين إضافية مشتراة (GB)",
        default=0,
        help_text=(
            "تُضاف فوق المساحة الأساسية المشتقة من سعة المعلمين. تبقى فعّالة ما دام "
            "اشتراك المدرسة فعّالاً، ولا علاقة لها بإضافة الأرشفة السنوية."
        ),
    )
    marketing_source = models.CharField(
        "مصدر التسجيل التسويقي",
        max_length=120,
        blank=True,
        default="",
    )
    marketing_medium = models.CharField(
        "وسيط الحملة",
        max_length=120,
        blank=True,
        default="",
    )
    marketing_campaign = models.CharField(
        "اسم الحملة",
        max_length=200,
        blank=True,
        default="",
    )
    marketing_content = models.CharField(
        "محتوى الإعلان",
        max_length=200,
        blank=True,
        default="",
    )
    marketing_term = models.CharField(
        "الكلمة التسويقية",
        max_length=200,
        blank=True,
        default="",
    )
    marketing_click_id = models.CharField(
        "معرف نقرة الإعلان",
        max_length=255,
        blank=True,
        default="",
    )
    marketing_referrer = models.CharField(
        "نطاق الإحالة",
        max_length=255,
        blank=True,
        default="",
    )
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)
    updated_at = models.DateTimeField("تم التحديث في", auto_now=True)

    class Meta:
        ordering = ("name",)
        verbose_name = "مدرسة"
        verbose_name_plural = "المدارس"
        constraints = [
            models.UniqueConstraint(
                fields=["normalized_name"],
                condition=~models.Q(normalized_name=""),
                name="uniq_school_normalized_name",
            ),
            models.UniqueConstraint(
                fields=["phone"],
                condition=models.Q(phone__isnull=False) & ~models.Q(phone=""),
                name="uniq_school_phone",
            ),
            models.UniqueConstraint(
                Lower("email"),
                condition=~models.Q(email=""),
                name="uniq_school_email_ci",
            ),
        ]

    def __str__(self) -> str:
        return self.name or self.code

    def save(self, *args, **kwargs):
        if self.name:
            self.name = " ".join(self.name.split())
        self.normalized_name = normalize_school_name_identity(self.name)
        if self.code:
            self.code = self.code.strip().lower()
        if self.phone:
            self.phone = normalize_sa_mobile_identity(self.phone)
        if self.email:
            self.email = self.email.strip().lower()
        persisted_storage_key = ""
        if self.pk and not self._state.adding:
            persisted_storage_key = (
                type(self)._base_manager.filter(pk=self.pk)
                .values_list("storage_key", flat=True)
                .first()
                or ""
            )
        if persisted_storage_key:
            # The object prefix is part of the storage identity, not branding.
            # Changing it would strand every previously uploaded object.
            self.storage_key = persisted_storage_key
        elif not self.storage_key:
            self.storage_key = self.code
        if self.current_academic_year:
            self.current_academic_year = _normalize_academic_year_hijri(self.current_academic_year)
        super().save(*args, **kwargs)


# =========================
# السنوات الدراسية (مرجع مركزي يديره مدير النظام)
# =========================
class AcademicYear(models.Model):
    """قائمة السنوات الدراسية الهجرية المتاحة على مستوى المنصة.

    يديرها مدير النظام من لوحة الآدمن، وتُستخدم كمصدر للخيارات في إعدادات المدرسة.
    """

    value = models.CharField("السنة الدراسية (هجري)", max_length=9, unique=True, db_index=True)
    is_active = models.BooleanField("نشطة (تظهر للمدارس)", default=True)
    order = models.PositiveIntegerField("الترتيب", default=0)
    created_at = models.DateTimeField("أُضيفت في", auto_now_add=True)

    class Meta:
        ordering = ("-value",)
        verbose_name = "سنة دراسية"
        verbose_name_plural = "السنوات الدراسية"

    def __str__(self) -> str:
        return self.value

    def clean(self):
        super().clean()
        self.value = _normalize_academic_year_hijri(self.value or "")
        _validate_academic_year_hijri(self.value)

    def save(self, *args, **kwargs):
        if self.value:
            self.value = _normalize_academic_year_hijri(self.value)
        super().save(*args, **kwargs)


# =========================
# مستخدم النظام: المعلم
# =========================
class TeacherManager(BaseUserManager):
    def create_user(self, phone, name, password=None, **extra_fields):
        if not phone:
            raise ValueError("رقم الجوال مطلوب")
        if not name:
            raise ValueError("اسم المستخدم مطلوب")
        user = self.model(phone=phone.strip(), name=name.strip(), **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, phone, name, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(phone, name, password, **extra_fields)


class Teacher(AbstractBaseUser, PermissionsMixin):
    class Gender(models.TextChoices):
        MALE = "male", "ذكر"
        FEMALE = "female", "أنثى"

    # Keep legacy imported identifiers intact during the SQLite → PostgreSQL
    # migration. New interactive entries remain constrained by the Saudi phone
    # validators in reports/forms.py.
    phone = models.CharField("رقم الجوال", max_length=64, unique=True)
    email = models.EmailField("البريد الإلكتروني", blank=True, default="")
    national_id = models.CharField("الهوية الوطنية", max_length=20, blank=True, null=True, unique=True)
    name = models.CharField("الاسم", max_length=150, db_index=True)
    gender = models.CharField("الجنس", max_length=6, choices=Gender.choices, blank=True, default="")

    # لاحقاً يمكن ربط المعلّم مباشرة بمدرسة افتراضية
    # school = models.ForeignKey(
    #     School,
    #     on_delete=models.SET_NULL,
    #     null=True,
    #     blank=True,
    #     verbose_name="المدرسة",
    #     related_name="teachers",
    # )

    is_active = models.BooleanField("نشط", default=True)
    is_staff = models.BooleanField("موظّف لوحة", default=False)
    passkey_prompt_opt_out = models.BooleanField(
        "عدم عرض دعوة تفعيل البصمة مجددًا",
        default=False,
        help_text="يوقف الدعوة التلقائية فقط؛ يبقى التفعيل متاحًا من إعدادات الأمان.",
    )
    current_session_key = models.CharField(max_length=64, blank=True, default="")
    date_joined = models.DateTimeField("تاريخ الانضمام", auto_now_add=True)

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["name"]

    objects = TeacherManager()

    class Meta:
        verbose_name = "مستخدم (معلم)"
        verbose_name_plural = "المستخدمون"

    @property
    def personal_teacher_label(self) -> str:
        if self.gender == self.Gender.FEMALE:
            return "المعلمة"
        if self.gender == self.Gender.MALE:
            return "المعلم"
        return "المعلم/المعلمة"

    @property
    def display_role_label(self) -> str:
        """اسم الدور للعرض بالعربية (للواجهات).

        - مدير المدرسة يجب أن يظهر كـ "مدير المدرسة" حتى لو كان is_staff=True.
        """
        active_school = None
        sid = None
        try:
            from ..middleware import get_current_request
            request = get_current_request()
            if request is not None:
                # ── إعادة استخدام ما حمّله الـ middleware ──
                active_school = getattr(request, "active_school", None)
                sid = request.session.get("active_school_id")
                if active_school is None and sid:
                    active_school = School.objects.filter(pk=sid, is_active=True).only("gender").first()
        except Exception:
            active_school = None
            sid = None
        try:
            from ..permissions import effective_user_role_label

            return effective_user_role_label(self, active_school=active_school, active_school_id=sid)
        except Exception:
            return "مستخدم"

    def save(self, *args, **kwargs):
        try:
            if bool(self.is_superuser):
                self.is_staff = True
        except Exception:
            pass
        super().save(*args, **kwargs)

    def __str__(self):
        role_name = self.display_role_label
        return f"{self.name} ({role_name or 'بدون دور'})"


class WebAuthnCredential(models.Model):
    """A passkey used for biometric login.

    Fingerprint/Face ID data remains on the user's device. The server stores
    only the public key and credential id needed to verify future sign-ins.
    """

    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name="webauthn_credentials",
        verbose_name="المستخدم",
    )
    credential_id = models.BinaryField("معرّف المفتاح", unique=True)
    credential_id_hash = models.CharField("بصمة معرّف المفتاح", max_length=64, unique=True, db_index=True)
    public_key_cose = models.BinaryField("المفتاح العام")
    sign_count = models.PositiveBigIntegerField("عداد التوقيع", default=0)
    device_name = models.CharField("اسم الجهاز", max_length=120, blank=True, default="")
    transports = models.JSONField("وسائل النقل", default=list, blank=True)
    is_active = models.BooleanField("نشط", default=True, db_index=True)
    last_used_at = models.DateTimeField("آخر استخدام", null=True, blank=True)
    created_at = models.DateTimeField("تاريخ الإضافة", auto_now_add=True)

    class Meta:
        verbose_name = "مفتاح دخول بالبصمة"
        verbose_name_plural = "مفاتيح الدخول بالبصمة"
        indexes = [
            models.Index(fields=["teacher", "is_active"]),
        ]

    def __str__(self) -> str:
        label = self.device_name or "مفتاح دخول"
        return f"{label} - {self.teacher}"


# =========================
# تعليقات خاصة (يراها المعلم فقط)
# =========================
class TeacherPrivateComment(models.Model):
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name="private_comments_received",
        verbose_name="المعلم المستهدف",
    )
    created_by = models.ForeignKey(
        Teacher,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="private_comments_created",
        verbose_name="أضيف بواسطة",
    )
    school = models.ForeignKey(
        School,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="private_comments",
        verbose_name="المدرسة",
    )
    achievement_file = models.ForeignKey(
        "TeacherAchievementFile",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="private_comments",
        verbose_name="ملف الإنجاز (اختياري)",
    )
    report = models.ForeignKey(
        "Report",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="private_comments",
        verbose_name="التقرير (اختياري)",
    )
    body = models.TextField("التعليق")
    created_at = models.DateTimeField("تاريخ الإضافة", default=timezone.now)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "تعليق خاص للمعلم"
        verbose_name_plural = "تعليقات خاصة للمعلمين"

    def __str__(self) -> str:
        return f"PrivateComment#{self.pk} to teacher#{self.teacher_id}"


# =========================
# مرجع الأقسام الديناميكي
# =========================
class Department(models.Model):
    school = models.ForeignKey(
        "School",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="departments",
        verbose_name="المدرسة",
        help_text="يظهر هذا القسم فقط داخل المدرسة المحددة.",
    )
    name = models.CharField("اسم القسم", max_length=120)
    slug = models.SlugField("المعرّف (slug)", max_length=64)
    role_label = models.CharField(
        "الاسم الظاهر في قائمة (الدور)",
        max_length=120,
        blank=True,
        help_text="هذا الاسم سيظهر كخيار (دور) عند إضافة المعلّم. إن تُرك فارغًا سيُستخدم اسم القسم.",
    )
    is_active = models.BooleanField("نشط", default=True)

    # ربط القسم بأنواع التقارير
    reporttypes = models.ManyToManyField(
        "ReportType",
        blank=True,
        related_name="departments",
        verbose_name="أنواع التقارير المرتبطة",
        help_text="اختَر الأنواع التي يحق لمسؤولي هذا القسم الاطلاع عليها (تُزامَن تلقائيًا مع دور القسم).",
    )

    class Meta:
        ordering = ("id",)
        constraints = [
            # ✅ السماح بتكرار slug بين المدارس المختلفة
            models.UniqueConstraint(
                fields=["school", "slug"],
                condition=models.Q(school__isnull=False),
                name="uniq_department_slug_per_school",
            ),
            # ✅ لو وُجدت أقسام عامة (school=NULL) تبقى فريدة عالميًا
            models.UniqueConstraint(
                fields=["slug"],
                condition=models.Q(school__isnull=True),
                name="uniq_global_department_slug",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "slug"]),
        ]
        verbose_name = "قسم"
        verbose_name_plural = "الأقسام"

    def __str__(self):
        return self.name

    # ===== منع حذف قسم المدير الدائم =====
    def delete(self, *args, **kwargs):
        if self.slug == MANAGER_SLUG:
            raise ValidationError("لا يمكن حذف قسم المدير الدائم.")
        return super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        """تطبيع slug + فرض خصائص قسم المدير فقط.

        ملاحظة مهمة: كان هناك سابقًا مزامنة تلقائية بين Department.slug و Role.slug.
        هذا لا يعمل مع الأقسام المخصصة لكل مدرسة (لأن Role.slug فريد عالميًا)، لذلك تم إيقافه.
        """
        def _slugify_english(text: str) -> str:
            try:
                from unidecode import unidecode  # type: ignore

                text = unidecode(text or "")
            except Exception:
                pass
            return slugify(text or "", allow_unicode=False)

        if self.slug:
            self.slug = self.slug.strip().lower()
        else:
            self.slug = _slugify_english(self.name or "")

        # fallback: لا نسمح بـ slug فارغ
        if not self.slug:
            self.slug = "dept"

        if self.slug == MANAGER_SLUG:
            self.name = MANAGER_NAME
            self.role_label = MANAGER_ROLE_LABEL
            self.is_active = True

        if not self.role_label:
            self.role_label = self.name

        super().save(*args, **kwargs)


class DepartmentMembership(models.Model):
    TEACHER = "teacher"
    OFFICER = "officer"
    ROLE_TYPE_CHOICES = [(TEACHER, "Teacher"), (OFFICER, "Officer")]

    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="القسم",
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dept_memberships",
        verbose_name="المعلم",
    )
    role_type = models.CharField("نوع التكليف", max_length=16, choices=ROLE_TYPE_CHOICES, default=TEACHER)

    class Meta:
        unique_together = [("department", "teacher")]
        indexes = [
            models.Index(fields=["department"]),
            models.Index(fields=["teacher"]),
        ]
        verbose_name = "تكليف قسم"
        verbose_name_plural = "تكليفات الأقسام"

    def __str__(self):
        return f"{self.teacher} @ {self.department} ({self.role_type})"

    # ===== ضمان: قسم المدير يقبل موظفين فقط =====
    def clean(self):
        super().clean()
        if getattr(self.department, "slug", "").lower() == MANAGER_SLUG and self.role_type != self.TEACHER:
            raise ValidationError("قسم المدير يقبل تكليف موظفين فقط (لا يوجد مسؤول قسم).")

    def save(self, *args, **kwargs):
        # إجبار الدور داخل القسم على TEACHER لقسم المدير
        if getattr(self.department, "slug", "").lower() == MANAGER_SLUG:
            self.role_type = self.TEACHER
        super().save(*args, **kwargs)


# =========================
# عضوية المدرسة (Teacher ↔ School)
# =========================
class SchoolMembership(models.Model):
    """عضوية مستخدم في مدرسة، وهي مصدر صلاحياته داخلها.

    **العضوية لا العَلَم.** الصلاحية هنا مربوطة بمدرسة بعينها لا بحقل منطقي على
    الحساب. هذا الفرق ليس شكلياً: المشروع سبق أن حمل دورين وسيطين على شكل عَلَم
    عام على الحساب، فتعذّر تقييد نطاقهما وانتهيا بالحذف عبر نحو 180 موضعاً
    (راجع خطة الحذف في مجلد التوثيق). فأي دور جديد يدخل من هنا، بنطاقه
    ومدرسته، أو لا يدخل.

    **المستخدم قد يحمل دورين في المدرسة نفسها** — الوكيل الذي له نصاب تدريسي
    يحمل ``deputy`` و``teacher`` معاً، وهو ما يسمح به ``unique_together``
    عمداً. ولذلك يُعدّ استهلاك المقاعد بعدد **المنسوبين** لا بعدد العضويات،
    وإلا احتُسب الرجل الواحد مقعدين.
    """

    class RoleType(models.TextChoices):
        """الدور الذي تُشتق منه الصلاحية داخل المدرسة."""

        TEACHER = "teacher", "معلم"
        MANAGER = "manager", "مدير مدرسة"
        # وكيل المدرسة: دور إشرافي نيابي في نطاق يحدده المدير — لا هو مدير
        # مصغَّر ولا معلّم بصلاحيات إضافية.
        DEPUTY = "deputy", "وكيل مدرسة"
        # الموظف الإداري: كان مسمّى عرضياً في ``job_title`` بلا أي أثر على
        # الصلاحية، فصار دوراً قائماً بذاته يُسند إليه ويُراجَع عمله.
        ADMIN_STAFF = "admin_staff", "موظف إداري"

    class JobTitle(models.TextChoices):
        """المسمّى الوظيفي — وصف تنظيمي للعرض، لا مصدر صلاحية.

        يبقى مستقلاً عن ``role_type`` لأن التوصيف التنظيمي أدقّ من الصلاحية:
        محضّر المختبر وموظف شؤون الطلاب كلاهما ``ADMIN_STAFF`` صلاحيةً، ويظلّان
        مختلفين في المسمّى وفي كشوف المدرسة.
        """

        TEACHER = "teacher", "معلم"
        ADMIN_STAFF = "admin_staff", "موظف إداري"
        LAB_TECH = "lab_tech", "محضر مختبر"

    # ── مجموعات الأدوار ──────────────────────────────────────────────────
    # «منسوبو المدرسة» = كل من ليس مديرها. كان هذا المعنى مبثوثاً في عشرات
    # المواضع بصيغة ``role_type=TEACHER``، فكان كل دور جديد يكسرها بصمت: يختفي
    # الوكيل من كشف المنسوبين ومن التصدير ومن مستقبلي التعاميم دون أن يفشل شيء.
    # تسميته هنا تجعل إضافة دور رابع تعديلاً في سطر واحد.
    STAFF_ROLES: tuple[str, ...] = (
        RoleType.DEPUTY,
        RoleType.ADMIN_STAFF,
        RoleType.TEACHER,
    )

    # ما يستهلك مقعداً من حد الباقة. قرار تجاري مُعلَن لا مشتق من مصادفة:
    # المدير وحده خارج العدّ، وكل منسوب سواه يستهلك مقعداً واحداً مهما تعددت
    # أدواره.
    SEAT_CONSUMING_ROLES: tuple[str, ...] = STAFF_ROLES

    # المسمّى يقتضي دوراً بعينه. الحقلان مستقلان في التخزين — والاستقلال مقصود
    # لأن التوصيف التنظيمي أدقّ من الصلاحية — لكن الاتجاه من المسمّى إلى الدور
    # ملزَمٌ بما تقوله ``JobTitle`` أعلاه: محضّر المختبر ``ADMIN_STAFF``
    # صلاحيةً. وكانت شاشة الأدوار وحدها تعرف ذلك، فيخرج من باب «إضافة مستخدم»
    # محضّرٌ ``TEACHER`` ومن باب الأدوار محضّرٌ ``ADMIN_STAFF``: اسمٌ واحد
    # وصلاحيتان، ولا شيء يكشف الفرق حتى تُمنح صلاحيةٌ فلا تنطبق.
    _ROLE_BY_JOB_TITLE: dict[str, str] = {
        JobTitle.LAB_TECH: RoleType.ADMIN_STAFF,
        JobTitle.ADMIN_STAFF: RoleType.ADMIN_STAFF,
        JobTitle.TEACHER: RoleType.TEACHER,
    }

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="المدرسة",
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="school_memberships",
        verbose_name="المستخدم",
    )
    role_type = models.CharField(
        "الدور داخل المدرسة",
        max_length=16,
        choices=RoleType.choices,
        default=RoleType.TEACHER,
    )

    job_title = models.CharField(
        "المسمى الوظيفي داخل المدرسة",
        max_length=16,
        choices=JobTitle.choices,
        default=JobTitle.TEACHER,
        help_text="للعرض فقط داخل المدرسة (بنفس الصلاحيات).",
    )
    lab_kind = models.CharField(
        "المختبر",
        max_length=16,
        choices=LabKind.choices,
        blank=True,
        default="",
        help_text="يُستخدم لمحضر المختبر فقط، وهو مستقل عن أقسام التقارير.",
    )
    is_active = models.BooleanField("نشط؟", default=True)
    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)

    class Meta:
        unique_together = [("school", "teacher", "role_type")]
        constraints = [
            # مدرسة واحدة لا يمكن أن يكون لها أكثر من مدير نشط واحد
            models.UniqueConstraint(
                fields=["school"],
                # نستخدم القيمة النصية "manager" لتفادي NameError أثناء تعريف الكلاس
                condition=models.Q(role_type="manager", is_active=True),
                name="uniq_active_manager_per_school",
            )
        ]
        indexes = [
            models.Index(fields=["school"]),
            models.Index(fields=["teacher"]),
        ]
        verbose_name = "عضوية مدرسة"
        verbose_name_plural = "عضويات المدارس"

    def __str__(self) -> str:
        return f"{self.teacher} @ {self.school} ({self.role_type})"

    # ------------------------------------------------------------------
    # المسمّى والدور
    # ------------------------------------------------------------------
    @classmethod
    def role_for_job_title(cls, job_title) -> str:
        """الدور الذي يقتضيه المسمّى الوظيفي — مصدر الحقيقة الوحيد للربط.

        يُسأل من كل باب يُنشئ عضوية: شاشة الأدوار، وإضافة مستخدم، والاستيراد
        الجماعي. وثلاثة أبواب تكتب الربط كلٌّ على حِدة تعني ثلاث إجابات عن
        سؤال واحد، وأولها ينحرف عند أول مسمّى جديد.

        وما لا يُعرف يعود ``TEACHER``: الافتراض الأضيق صلاحيةً هو الافتراض
        الآمن، ومسمّى مجهول يجب ألا يمنح صاحبه صلاحية موظف إداري.
        """
        return cls._ROLE_BY_JOB_TITLE.get(job_title, cls.RoleType.TEACHER)

    # ------------------------------------------------------------------
    # المقاعد
    # ------------------------------------------------------------------
    @classmethod
    def seats_used(cls, school) -> int:
        """المقاعد المشغولة في مدرسة واحدة.

        مصدر الحقيقة الوحيد لهذا الرقم. كان محسوباً في ستة مواضع بصيغة
        ``.count()`` على الصفوف، وهي صيغة صحيحة ما دام لكل منسوب دور واحد —
        وتنكسر بصمت أول ما يحمل وكيلٌ نصاباً تدريسياً فيُحتسب مقعدين. وتفرّق
        الأرقام بين شاشة الفوترة وشاشة المنسوبين أسوأ من خطئها في كلتيهما.
        """
        if school is None:
            return 0
        return (
            cls.objects.filter(school=school, role_type__in=cls.SEAT_CONSUMING_ROLES)
            .values("teacher_id")
            .distinct()
            .count()
        )

    @classmethod
    def seats_used_by_school(cls, school_ids) -> dict[int, int]:
        """المقاعد المشغولة لعدة مدارس في استعلام واحد.

        نسخة الجملة الواحدة من :meth:`seats_used`، للوحات التي تعرض عشرات
        المدارس ولا تحتمل استعلاماً لكل صف.
        """
        ids = list(school_ids or [])
        if not ids:
            return {}
        rows = (
            cls.objects.filter(school_id__in=ids, role_type__in=cls.SEAT_CONSUMING_ROLES)
            .values("school_id")
            .annotate(total=models.Count("teacher_id", distinct=True))
        )
        return {int(row["school_id"]): int(row["total"] or 0) for row in rows}

    def save(self, *args, **kwargs):
        """فرض حد المقاعد حسب باقة المدرسة.

        المتطلبات:
        - لا يُحسب مدير المدرسة ضمن الحد (role_type=MANAGER).
        - الحد يُحسب على عدد **المنسوبين** المرتبطين بالمدرسة بأي دور من
          ``SEAT_CONSUMING_ROLES``، بغض النظر عن is_active.
        - الحذف يفتح مقعدًا (بما أنه يزيل العضوية).

        العدّ بالمنسوبين لا بالعضويات: المستخدم الواحد قد يحمل دورين في المدرسة
        نفسها (وكيل له نصاب تدريسي)، وعدّ الصفوف كان يحتسبه مقعدين ويحرم
        المدرسة مقعداً دفعت ثمنه.

        ملاحظة مهمة:
        - نطبق المنع فقط عند إنشاء عضوية تستهلك مقعداً، أو عند تحويل/نقل عضوية
          إلى دور يستهلك مقعداً أو إلى مدرسة أخرى. لا نمنع تحديثات بسيطة لعضوية
          موجودة (مثل تغيير is_active) حتى لو كانت المدرسة متجاوزة للحد تاريخيًا.
        """
        from django.core.exceptions import ValidationError

        should_enforce = self.pk is None
        if not should_enforce and self.pk is not None:
            try:
                prev = (
                    SchoolMembership.objects.filter(pk=self.pk)
                    .only("role_type", "school_id", "is_active")
                    .first()
                )
                if prev is not None and (
                    prev.role_type != self.role_type
                    or prev.school_id != self.school_id
                    or (not bool(prev.is_active) and bool(self.is_active))
                ):
                    should_enforce = True
            except Exception:
                # إن تعذرت المقارنة، لا نطبق المنع على تحديث عضوية موجودة
                should_enforce = False

        if should_enforce and self.role_type in self.SEAT_CONSUMING_ROLES:
            subscription = getattr(self.school, "subscription", None)
            if subscription is None or bool(getattr(subscription, "is_expired", True)):
                raise ValidationError("لا يوجد اشتراك فعّال لهذه المدرسة.")

            max_teachers = int(getattr(subscription, "teacher_limit", 0) or 0)
            if max_teachers > 0:
                # منسوب يشغل مقعداً واحداً مهما تعددت أدواره، فنعدّ الأشخاص
                # المتميّزين لا صفوف العضوية.
                occupied = set(
                    SchoolMembership.objects.filter(
                        school=self.school,
                        role_type__in=self.SEAT_CONSUMING_ROLES,
                    )
                    .exclude(pk=self.pk)
                    .values_list("teacher_id", flat=True)
                )
                # دور ثانٍ لمنسوب قائم لا يستهلك مقعداً جديداً، فلا يصح منعه
                # حتى عند اكتمال العدد.
                if self.teacher_id not in occupied and len(occupied) >= max_teachers:
                    raise ValidationError(
                        f"لا يمكن إضافة أكثر من {max_teachers} منسوب لهذه المدرسة حسب الباقة."
                    )

        return super().save(*args, **kwargs)


class SchoolGroupMembership(models.Model):
    """عضوية المدير التنفيذي في مجموعة المدارس المتكاملة.

    العضوية على *المجموعة* لا على أي مدرسة، وهذا مقصود لسببين:

    - تنظيمياً: المدير التنفيذي يرتبط بإدارة التعليم لا بمدرسة بعينها، ولا
      يتولى الإدارة اليومية لأي منها.
    - محاسبياً: مقاعد المعلمين المدفوعة تُحسب من ``SchoolMembership``، فبقاؤه
      خارجها يعني أنه لا يستهلك مقعداً مدفوعاً في أي مدرسة.
    """

    class RoleType(models.TextChoices):
        EXECUTIVE_DIRECTOR = "executive_director", "مدير تنفيذي"

    group = models.ForeignKey(
        SchoolGroup,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="المجموعة",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="school_group_memberships",
        verbose_name="المستخدم",
    )
    role_type = models.CharField(
        "الدور داخل المجموعة",
        max_length=32,
        choices=RoleType.choices,
        default=RoleType.EXECUTIVE_DIRECTOR,
    )
    is_active = models.BooleanField("نشط؟", default=True)
    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)

    class Meta:
        unique_together = [("group", "user", "role_type")]
        constraints = [
            # مجموعة واحدة لا يكون لها أكثر من مدير تنفيذي نشط، على غرار قيد
            # المدير الواحد لكل مدرسة. القيمة نصية لتفادي NameError داخل الكلاس.
            models.UniqueConstraint(
                fields=["group"],
                condition=models.Q(role_type="executive_director", is_active=True),
                name="uniq_active_executive_director_per_group",
            )
        ]
        indexes = [
            models.Index(fields=["group"]),
            models.Index(fields=["user"]),
        ]
        verbose_name = "عضوية مجموعة مدارس"
        verbose_name_plural = "عضويات مجموعات المدارس"

    def __str__(self) -> str:
        return f"{self.user} @ {self.group} ({self.role_type})"


# =========================
# طلب إضافة مدرسة لحساب مدير قائم
# =========================
class SchoolAdditionRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "قيد المراجعة"
        APPROVED = "approved", "معتمد"
        REJECTED = "rejected", "مرفوض"

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="school_addition_requests",
        verbose_name="مقدم الطلب",
    )
    source_school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="addition_requests_from",
        verbose_name="المدرسة الحالية",
    )
    school_name = models.CharField("اسم المدرسة المطلوبة", max_length=200)
    stage = models.CharField("المرحلة", max_length=16, choices=School.Stage.choices)
    gender = models.CharField("بنين / بنات", max_length=8, choices=School.Gender.choices)
    city = models.CharField("المدينة", max_length=120, blank=True, default="")
    phone = models.CharField("جوال المدرسة", max_length=20, blank=True, default="")
    email = models.EmailField("بريد المدرسة", blank=True, default="")
    manager_notes = models.TextField("ملاحظات المدير", max_length=1000, blank=True, default="")
    status = models.CharField(
        "الحالة",
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_school = models.OneToOneField(
        School,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_addition_request",
        verbose_name="المدرسة المنشأة",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_school_addition_requests",
        verbose_name="راجع الطلب",
    )
    review_notes = models.TextField("ملاحظات المراجعة", max_length=1000, blank=True, default="")
    reviewed_at = models.DateTimeField("تاريخ المراجعة", null=True, blank=True)
    created_at = models.DateTimeField("تاريخ الطلب", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "طلب إضافة مدرسة"
        verbose_name_plural = "طلبات إضافة المدارس"
        indexes = [
            models.Index(fields=["requested_by", "status"], name="reports_sar_user_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.school_name} — {self.get_status_display()}"


# =========================
# مرجع أنواع التقارير الديناميكي
# =========================
class ReportType(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="report_types",
        verbose_name="المدرسة",
        help_text="يظهر هذا النوع فقط في المدرسة المحددة.",
    )
    code = models.SlugField("الكود", max_length=40)
    name = models.CharField("الاسم", max_length=120)
    description = models.TextField("الوصف", blank=True)
    order = models.PositiveIntegerField("الترتيب", default=0)
    is_active = models.BooleanField("نشط", default=True)
    # مسار الاعتماد يُخزَّن على النوع لا على التقرير: توصيف الأدوار ينصّ على أن
    # «لا يلزم مرور كل تقرير بالوكيل» وأن المدير يحدّد المسار بحسب نوع العمل.
    # فتخزينه هنا يجعل تغيير السياسة تعديلَ حقل لا نشرَ إصدار.
    approval_route = models.CharField(
        "مسار الاعتماد",
        max_length=16,
        default="direct",
        help_text="من يراجع هذا النوع ومن يعتمده. الافتراضي: مباشرةً إلى مدير المدرسة.",
    )
    created_at = models.DateTimeField("أُنشئ", auto_now_add=True)
    updated_at = models.DateTimeField("تحديث", auto_now=True)

    class Meta:
        ordering = ("order", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["school", "code"],
                condition=models.Q(school__isnull=False),
                name="uniq_reporttype_code_per_school",
            ),
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(school__isnull=True),
                name="uniq_global_reporttype_code",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "code"]),
        ]
        verbose_name = "نوع تقرير"
        verbose_name_plural = "أنواع التقارير"

    def __str__(self) -> str:
        return self.name or self.code

    def save(self, *args, **kwargs):
        # تطبيع code إلى lowercase
        if self.code:
            self.code = self.code.strip().lower()
        super().save(*args, **kwargs)


# =========================
# نموذج التقرير العام
