from __future__ import annotations

from .approvals import ApprovalMixin, ApprovalState
from .base import *
from .schools import Department, School, SchoolGroup, Teacher

__all__ = [
    "Meeting",
    "MeetingAgendaItem",
    "MeetingAttendee",
    "MeetingMinutes",
    "Decision",
]


class Meeting(models.Model):
    """اجتماع — مجلس مجموعة، أو اجتماع مدرسة، أو لجنة في نطاق وكيل.

    **الانعقاد شيءٌ واعتماد محضره شيء آخر.** حالة الاجتماع هنا تجيب عن «هل
    انعقد؟» وحدها، وحالة الاعتماد تعيش على ``MeetingMinutes``. دمجهما في حقل
    واحد يجعل اجتماعاً انعقد ولم يُكتب محضره بعدُ يبدو كأنه لم يقع.

    **مستويان بنموذج واحد** — كما في التكليف: ``scope`` يفرّق بين مجلس المجموعة
    واجتماع المدرسة، ولا نموذجان يتباعدان عند أول تعديل.
    """

    class Scope(models.TextChoices):
        SCHOOL = "school", "اجتماع مدرسة"
        GROUP = "group", "مجلس مجموعة المدارس"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "مجدول"
        HELD = "held", "منعقد"
        CANCELLED = "cancelled", "ملغى"

    scope = models.CharField(
        "نطاق الاجتماع", max_length=16, choices=Scope.choices,
        default=Scope.SCHOOL, db_index=True,
    )
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, null=True, blank=True,
        related_name="meetings", verbose_name="المدرسة",
    )
    group = models.ForeignKey(
        SchoolGroup, on_delete=models.CASCADE, null=True, blank=True,
        related_name="meetings", verbose_name="المجموعة",
    )
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="meetings", verbose_name="اللجنة / القسم",
        help_text="يحدّد من يشرف عليه ضمن نطاقه.",
    )

    organizer = models.ForeignKey(
        Teacher, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="meetings_organized", verbose_name="منظّم الاجتماع",
    )
    organizer_name = models.CharField(
        "اسم المنظّم (وقت الإنشاء)", max_length=150, blank=True, default=""
    )

    title = models.CharField("عنوان الاجتماع", max_length=200)
    purpose = models.TextField("الغرض", blank=True, default="")
    scheduled_at = models.DateTimeField("موعد الانعقاد", db_index=True)
    location = models.CharField("المكان", max_length=200, blank=True, default="")

    status = models.CharField(
        "الحالة", max_length=16, choices=Status.choices,
        default=Status.SCHEDULED, db_index=True,
    )
    held_at = models.DateTimeField("انعقد في", null=True, blank=True)
    cancel_reason = models.CharField("سبب الإلغاء", max_length=255, blank=True, default="")

    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-scheduled_at", "-id")
        indexes = [
            models.Index(fields=["school", "-scheduled_at"]),
            models.Index(fields=["group", "-scheduled_at"]),
            models.Index(fields=["organizer", "-scheduled_at"]),
        ]
        verbose_name = "اجتماع"
        verbose_name_plural = "الاجتماعات"

    def __str__(self) -> str:
        return self.title

    # ------------------------------------------------------------------
    @property
    def is_cancelled(self) -> bool:
        return self.status == self.Status.CANCELLED

    @property
    def is_held(self) -> bool:
        return self.status == self.Status.HELD

    @property
    def attendance_summary(self) -> dict:
        rows = list(self.attendees.all())
        return {
            "total": len(rows),
            "present": sum(1 for row in rows if row.status == MeetingAttendee.Status.PRESENT),
            "absent": sum(1 for row in rows if row.status == MeetingAttendee.Status.ABSENT),
            "excused": sum(1 for row in rows if row.status == MeetingAttendee.Status.EXCUSED),
        }

    def clean(self):
        super().clean()
        if self.scope == self.Scope.SCHOOL and self.school_id is None:
            raise ValidationError({"school": "اجتماع المدرسة يحتاج مدرسة."})
        if self.scope == self.Scope.GROUP and self.group_id is None:
            raise ValidationError({"group": "مجلس المجموعة يحتاج مجموعة."})

    def save(self, *args, **kwargs):
        if self.organizer_id and not self.organizer_name:
            try:
                self.organizer_name = (getattr(self.organizer, "name", "") or "")[:150]
            except Exception:
                pass
        return super().save(*args, **kwargs)


class MeetingAgendaItem(models.Model):
    """بند في جدول الأعمال.

    بنود مستقلة لا نصٌّ واحد: القرار يُربط ببنده، فيُعرف لاحقاً **عمّ صدر** لا
    أنه صدر في اجتماع كذا فحسب.
    """

    meeting = models.ForeignKey(
        Meeting, on_delete=models.CASCADE, related_name="agenda_items",
        verbose_name="الاجتماع",
    )
    order = models.PositiveSmallIntegerField("الترتيب", default=1)
    title = models.CharField("البند", max_length=255)
    note = models.TextField("تمهيد / مرفقات", blank=True, default="")

    class Meta:
        ordering = ("order", "id")
        verbose_name = "بند جدول أعمال"
        verbose_name_plural = "بنود جدول الأعمال"

    def __str__(self) -> str:
        return self.title


class MeetingAttendee(models.Model):
    """حضور مدعوّ واحد.

    الدعوة والحضور صفّ واحد لا صفّان: من دُعي ولم يحضر غيابٌ يجب أن يُسجَّل،
    وحذفُ صفّه عند غيابه يمحو الواقعة.
    """

    class Status(models.TextChoices):
        INVITED = "invited", "مدعو"
        PRESENT = "present", "حاضر"
        ABSENT = "absent", "غائب"
        EXCUSED = "excused", "معتذر"

    meeting = models.ForeignKey(
        Meeting, on_delete=models.CASCADE, related_name="attendees",
        verbose_name="الاجتماع",
    )
    person = models.ForeignKey(
        Teacher, on_delete=models.CASCADE, related_name="meeting_attendances",
        verbose_name="المدعو",
    )
    person_name = models.CharField(
        "الاسم (وقت الدعوة)", max_length=150, blank=True, default=""
    )
    status = models.CharField(
        "الحضور", max_length=16, choices=Status.choices, default=Status.INVITED
    )
    note = models.CharField("ملاحظة", max_length=255, blank=True, default="")

    class Meta:
        ordering = ("person_name", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "person"], name="uniq_meeting_attendee"
            )
        ]
        verbose_name = "حضور اجتماع"
        verbose_name_plural = "حضور الاجتماعات"

    def __str__(self) -> str:
        return f"{self.person_name or self.person_id} · {self.get_status_display()}"

    def save(self, *args, **kwargs):
        if self.person_id and not self.person_name:
            try:
                self.person_name = (getattr(self.person, "name", "") or "")[:150]
            except Exception:
                pass
        return super().save(*args, **kwargs)


class MeetingMinutes(ApprovalMixin):
    """محضر الاجتماع — وثيقة تمر بدورة الاعتماد.

    **كاتبُه غير معتمِده.** التوصيف يجعل كتابة المحضر من مهام الموظف الإداري
    واعتمادَه من مهام المدير — وهذا بالضبط ما يفرضه ``ApprovalMixin``: يُرسَل
    للمراجعة، ويُعاد بملاحظة، ويُعتمد ممن لم يكتبه.

    ولذلك يعيش المحضر في نموذج مستقل عن ``Meeting``: الاجتماع واقعة تُجدوَل
    وتنعقد، والمحضر وثيقة تُكتب وتُعتمد — ولكلٍّ دورة حياة لا تشبه الأخرى.
    """

    meeting = models.OneToOneField(
        Meeting, on_delete=models.CASCADE, related_name="minutes",
        verbose_name="الاجتماع",
    )
    recorder = models.ForeignKey(
        Teacher, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="minutes_written", verbose_name="كاتب المحضر",
    )
    class FormatMode(models.TextChoices):
        FREEFORM = "freeform", "نص موحد"
        STRUCTURED = "structured", "محضر منظم"

    format_mode = models.CharField(
        "صيغة المحضر",
        max_length=12,
        choices=FormatMode.choices,
        default=FormatMode.FREEFORM,
    )
    body = models.TextField("نص المحضر", blank=True, default="")
    proceedings = models.TextField("مجريات الاجتماع", blank=True, default="")
    discussions = models.TextField("أبرز النقاشات", blank=True, default="")
    decisions_summary = models.TextField("ملخص القرارات", blank=True, default="")
    recommendations = models.TextField("التوصيات", blank=True, default="")
    assignments_summary = models.TextField("التكليفات", blank=True, default="")
    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)
    updated_at = models.DateTimeField("آخر تعديل", auto_now=True)

    class Meta:
        verbose_name = "محضر اجتماع"
        verbose_name_plural = "محاضر الاجتماعات"

    def __str__(self) -> str:
        return f"محضر {self.meeting_id}"

    # ------------------------------------------------------------------
    @property
    def school(self):
        """المدرسة التي يُنسب إليها المحضر — يقرأها مكوّن الاعتماد."""
        return getattr(self.meeting, "school", None)

    @property
    def approval_block_reason(self) -> str:
        """لا تبدأ دورة اعتماد المحضر قبل ثبوت انعقاد اجتماعه."""
        if not getattr(self.meeting, "is_held", False):
            return "لم يُسجَّل انعقاد الاجتماع بعد."
        return ""

    def _is_organizer(self, user) -> bool:
        return (
            user is not None
            and getattr(self.meeting, "organizer_id", None) == getattr(user, "pk", None)
        )

    def assert_ready_for_submission(self) -> None:
        content = [
            self.body,
            self.proceedings,
            self.discussions,
            self.decisions_summary,
            self.recommendations,
            self.assignments_summary,
        ]
        if not any((value or "").strip() for value in content):
            raise ValidationError("لا يُرسَل محضر فارغ للاعتماد.")
        if not getattr(self.meeting, "is_held", False):
            raise ValidationError("لم يُسجَّل انعقاد الاجتماع بعد.")

    def can_review_approval(self, user, school):
        """منظّم الاجتماع يراجع محضره — ومن سواه يحتاج نطاقاً على لجنته.

        بلا اشتراط صلاحية على المنظّم: من دعا إلى الاجتماع أعلمُ بما جرى فيه،
        وهذا ما يتيح للمدير التنفيذي اعتماد محضر مجلسه دون عضوية في أي مدرسة.
        """
        if self._is_organizer(user):
            return True

        from ..capabilities import MANAGE_MEETINGS
        from ..permissions import capability_source, supervised_department_ids

        if capability_source(user, MANAGE_MEETINGS, school) is None:
            return False
        department_id = getattr(self.meeting, "department_id", None)
        if department_id is None:
            return False
        return department_id in supervised_department_ids(user, school)

    def can_finalize_approval(self, user, school):
        # مجالس مجموعة المدارس يملك منظّمها سلطة إصدار محضر المجلس. أمّا
        # الاجتماع المدرسي فسلطة الاعتماد النهائية تبقى لمدير المدرسة؛ كون
        # الموظف منظّماً لا يحوّله إلى سلطة اعتماد على عمله أو عمل غيره.
        if self.meeting.scope == Meeting.Scope.GROUP and self._is_organizer(user):
            return True
        return None

    def allows_issuance(self, user, school) -> bool:
        """هل يُصدِر هذا المستخدمُ المحضرَ بدل أن يرفعه لمراجع؟

        نعم حين يكون **صاحب سلطة الاعتماد** هو منظّم الاجتماع وكاتب محضره:
        مدير المدرسة في الاجتماع المدرسي، أو منظّم مجلس مجموعة المدارس.

        وفي الاجتماع المدرسي الذي ينظّمه غير المدير يبقى المسار الطبيعي مهما
        كان كاتب المحضر: يُرسَل للمراجعة، ويُعاد بملاحظة، ويعتمده مدير المدرسة.
        وهو ما يحفظ قاعدة «لا يعتمد أحد عمله» ويطابق سلطة الدور المعلنة.
        """
        if not (
            self._is_organizer(user)
            and self.recorder_id == getattr(user, "pk", None)
        ):
            return False

        if self.meeting.scope == Meeting.Scope.GROUP:
            return True

        from ..permissions import is_school_manager

        return is_school_manager(user, active_school=school)


class Decision(models.Model):
    """قرار أو توصية صادرة عن اجتماع.

    **القرار يتحوّل إلى تكليف ولا يُنسخ إليه.** الحقل ``assignment`` رابطٌ لا
    قيمة مكرّرة، فمتابعةُ تنفيذ القرار هي متابعة تكليفه نفسه — بموعده وشواهده
    واعتماده. ونسخُ نصّ القرار في تكليف منفصل كان سيخلق مصدرَي حقيقة يفترقان
    عند أول تعديل.

    وقرارٌ بلا مسؤول ولا موعد يبقى قراراً مشروعاً: ليس كل ما يُقرَّر يُنفَّذ
    بيد شخص بعينه. لكن **تحويله إلى تكليف يشترطهما** — وذلك ما يفصل التوثيق
    عن المتابعة.
    """

    class Kind(models.TextChoices):
        DECISION = "decision", "قرار"
        RECOMMENDATION = "recommendation", "توصية"

    meeting = models.ForeignKey(
        Meeting, on_delete=models.CASCADE, related_name="decisions",
        verbose_name="الاجتماع",
    )
    agenda_item = models.ForeignKey(
        MeetingAgendaItem, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="decisions", verbose_name="البند",
    )
    order = models.PositiveSmallIntegerField("الترتيب", default=1)
    kind = models.CharField(
        "النوع", max_length=16, choices=Kind.choices, default=Kind.DECISION
    )
    title = models.CharField("نص القرار", max_length=255)
    body = models.TextField("التفصيل", blank=True, default="")

    responsible = models.ForeignKey(
        Teacher, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="decisions_responsible", verbose_name="المسؤول عن التنفيذ",
    )
    due_at = models.DateTimeField("موعد التنفيذ", null=True, blank=True)

    assignment = models.OneToOneField(
        "Assignment", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_decision", verbose_name="التكليف المنبثق",
    )

    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)

    class Meta:
        ordering = ("order", "id")
        indexes = [models.Index(fields=["meeting", "order"])]
        verbose_name = "قرار"
        verbose_name_plural = "القرارات"

    def __str__(self) -> str:
        return self.title

    # ------------------------------------------------------------------
    @property
    def is_tracked(self) -> bool:
        return self.assignment_id is not None

    @property
    def execution_state(self) -> str:
        """حالة تنفيذ القرار — مقروءة من تكليفه لا مخزَّنة عليه."""
        if not self.is_tracked:
            return "untracked"
        targets = list(self.assignment.targets.all())
        if not targets:
            return "untracked"
        if all(item.approval_state == ApprovalState.APPROVED for item in targets):
            return "done"
        if any(item.is_overdue for item in targets):
            return "late"
        return "running"

    def can_become_assignment(self) -> bool:
        return (
            self.assignment_id is None
            and self.responsible_id is not None
            and self.due_at is not None
        )
