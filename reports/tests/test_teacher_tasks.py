"""مهامّ المعلّم: تُفتح، وتُنجَز.

**لماذا هذه الوحدة.** الصقل السابق مسّ ‎add_report.html‎ و‎edit_report.html‎
و‎report-evidence.css‎ — وهي شاشات **المعلّم** لا المدير: هو من يكتب التقارير،
والمدير يراجعها. ومع ذلك جُرّبت كلها بحساب مدير، واختُبر المعلّم سلباً فقط:
أنه ممنوعٌ من شاشات غيره. وذلك يحرس ما لا يخصّه ولا يحرس ما يخصّه.

**وفتحُ الصفحة ليس فحصاً.** ‎200‎ تعني أن القالب صُيّر، لا أن المهمة تُنجَز.
فالتقرير يُرسَل هنا فعلاً ويُتحقّق من حفظه — لأن مساعد التاريخ الهجري ورافع
الشواهد يعملان داخل هذا النموذج، وكسرُهما لا يظهر في رمز الحالة.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class TeacherOwnScreensTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(
            name="مدرسة المعلّم", code="teacher-tasks", current_academic_year="1448-1449"
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة المعلّم", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        cls.report_type = ReportType.objects.create(
            school=cls.school, code="tp", name="تقرير المعلّم", is_active=True
        )
        cls.manager = Teacher.objects.create_user(
            phone="500888001", name="مدير", password="x", is_staff=True
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER, is_active=True,
        )
        cls.teacher = Teacher.objects.create_user(
            phone="500888002", name="معلّم المعاينة", password="x"
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.teacher,
            role_type=SchoolMembership.RoleType.TEACHER, is_active=True,
        )
        cls.other_teacher = Teacher.objects.create_user(
            phone="500888003", name="زميل", password="x"
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.other_teacher,
            role_type=SchoolMembership.RoleType.TEACHER, is_active=True,
        )
        cls.report = Report.objects.create(
            school=cls.school, teacher=cls.teacher, category=cls.report_type,
            title="تقرير المعلّم", report_date=timezone.localdate(),
        )
        cls.colleague_report = Report.objects.create(
            school=cls.school, teacher=cls.other_teacher, category=cls.report_type,
            title="تقرير الزميل", report_date=timezone.localdate(),
        )

    def _login(self, user=None):
        self.client.force_login(user or self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        return self.client

    def test_every_screen_the_teacher_is_sent_to_actually_opens(self):
        """رابطٌ في قائمة المعلّم يؤدي إلى ‎404‎ أسوأ من رابطٍ غائب."""
        self._login()
        routes = (
            "reports:home",
            "reports:add_report",
            "reports:my_reports",
            "reports:my_assignments",
            "reports:my_requests",
            "reports:my_notifications",
            "reports:my_circulars",
            "reports:my_work_archive",
            "reports:my_data",
        )
        for route in routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 200)

    def test_the_teacher_can_open_and_print_their_own_report(self):
        self._login()

        for route in ("reports:edit_my_report", "reports:report_print"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[self.report.pk]))
                self.assertEqual(response.status_code, 200)

    def test_submitting_a_report_actually_saves_it(self):
        """المهمة الأساسية — والفحص الذي لا يقوم مقامه رمز حالة.

        مساعد التاريخ الهجري ورافع الشواهد يعملان داخل هذا النموذج؛ فلو كُسر
        أحدهما لظلّت الصفحة تُفتح بـ‎200‎ ولم يُحفظ شيء.
        """
        self._login()
        before = Report.objects.filter(teacher=self.teacher).count()

        response = self.client.post(
            reverse("reports:add_report"),
            {
                # الحقل يقبل ``code`` لا ``pk`` — ‎to_field_name="code"‎.
                "category": self.report_type.code,
                "title": "تقرير أنشأه المعلّم",
                "report_date": timezone.localdate().isoformat(),
                "day_name": "الخميس",
                "show_details": "on",
                "idea": "وصفٌ كافٍ لما نُفّذ في هذا التقرير المُختبَر.",
                "show_beneficiaries": "on",
                "beneficiaries_count": "20",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Report.objects.filter(teacher=self.teacher).count(), before + 1)
        saved = Report.objects.filter(teacher=self.teacher).latest("id")
        self.assertEqual(saved.title, "تقرير أنشأه المعلّم")
        self.assertEqual(saved.school, self.school)

    def test_a_teacher_cannot_edit_a_colleagues_report(self):
        # «تقاريري» تعني تقاريري: الملكية تُفحص لا تُفترض من الرابط.
        self._login()

        response = self.client.get(
            reverse("reports:edit_my_report", args=[self.colleague_report.pk])
        )

        self.assertNotEqual(response.status_code, 200)

    def test_my_reports_lists_mine_and_not_the_schools(self):
        self._login()

        response = self.client.get(reverse("reports:my_reports"))

        self.assertContains(response, "تقرير المعلّم")
        self.assertNotContains(response, "تقرير الزميل")

    def test_the_report_form_carries_the_shared_hijri_helper(self):
        """الجسر بين الميلادي المُدخَل والهجري المقروء — في شاشة من يكتب.

        وسقوطه هنا أثقل منه عند المدير: المعلّم يفتح هذه الشاشة كل أسبوع،
        والمدير يفتحها نادراً.
        """
        self._login()

        response = self.client.get(reverse("reports:add_report"))
        html = response.content.decode("utf-8")

        self.assertIn("js/hijri-date.js", html)
        self.assertIn("TawtheeqHijri", html)
        self.assertNotIn("islamic-umalqura", html)  # لا مُنسّق محلي ثانٍ


@override_settings(ALLOWED_HOSTS=["testserver"])
class GoldAsInkTests(TestCase):
    """الذهبي لونُ سطحٍ وحدّ، لا لونُ حبرٍ على أبيض.

    ‎--id-gold‎ فوق الأبيض يعطي ‎2.75‎ — دون الحدّ حتى للنص الكبير الغليظ.
    وظهر ذلك في «تكليفاتي»، وهي شاشة معلّم: رقمُ «يستحق خلال 3 أيام» بذهبٍ
    على بطاقةٍ بيضاء. فأُفرد للحبر درجةٌ أعمق بدل تفتيح الخلفية.
    """

    @staticmethod
    def _source(relative_path: str) -> str:
        from pathlib import Path

        from django.conf import settings

        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    def test_the_identity_system_defines_a_gold_meant_for_text(self):
        source = self._source("reports/templates/reports/_identity.html")

        self.assertIn("--id-gold-ink: var(--id-gold-700);", source)  # فاتح: 5.02
        self.assertIn("--id-gold-ink: var(--id-gold-300);", source)  # داكن: 10.69

    def test_assignment_text_uses_the_ink_gold_not_the_surface_gold(self):
        source = self._source("reports/templates/reports/_assignment_theme.html")

        # المرفوض أن يكون ذهبُ السطح هو القيمة *الأولى*. أمّا وجوده احتياطياً
        # داخل ‎var(--id-gold-ink, var(--asg-gold))‎ فمقصود: قالبٌ لم يُحمَّل
        # فيه نظام الهوية يعود إلى لونٍ معقول بدل أن يفقد لونه.
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "color: var(--asg-gold)" in line
        ]

        self.assertEqual(
            offenders,
            [],
            "بقيت قاعدةٌ تُلوّن نصاً بذهب السطح: " + " | ".join(offenders),
        )
        self.assertEqual(source.count("var(--id-gold-ink"), 3)

    def test_the_border_keeps_the_surface_gold(self):
        # الحدّ ليس نصاً، فلا يُعمَّق بلا سبب — وإلا ضاعت النغمة الذهبية.
        source = self._source("reports/templates/reports/_assignment_theme.html")

        self.assertIn('.asg-item[data-soon="1"] { border-inline-start: 3px solid var(--asg-gold); }', source)


@override_settings(ALLOWED_HOSTS=["testserver"])
class TeacherScreenPolishTests(TestCase):
    """ما يقرؤه المعلّم على شاشاته — بنفس قواعد شاشات المدير.

    الصقل الأول شمل المدير وحده، فبقيت شاشات المعلّم على عيوبٍ أصلحتُ نظائرها
    عنده: تواريخُ ميلادية عارية، ومعدودٌ لا يتبع عدده، وأزرارٌ تقول «تعديل»
    عشر مرات بلا أن تقول: تعديلَ أيّ تقرير.
    """

    @staticmethod
    def _source(relative_path: str) -> str:
        from pathlib import Path

        from django.conf import settings

        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    # ── التواريخ ─────────────────────────────────────────────────────────

    BARE_GREGORIAN_SCREENS = (
        "reports/templates/reports/home.html",
        "reports/templates/reports/my_notifications.html",
        "reports/templates/reports/my_notification_detail.html",
        "reports/templates/reports/my_work_archive.html",
    )

    def test_no_teacher_screen_shows_a_gregorian_date_on_its_own(self):
        """المنصة هجرية؛ وتاريخٌ ميلاديٌّ عارٍ بينها يُقرأ خطأً مطبعياً.

        كانت عشرة مواضع في أربع شاشات تعرض ‎2026-09-03‎ بلا هجريٍّ بجانبه —
        منها لحظةُ وصول الإشعار وآخرُ موعدٍ للتوقيع، وهما ما يُقاس عليهما.
        """
        import re

        # ميلاديٌّ يجاوره هجريٌّ أو تحمله سمة ‎datetime‎ مقبول؛ والعاري لا.
        bare = re.compile(r'\{\{\s*[\w.]+\|date:"Y-m-d[^"]*"\s*\}\}')
        for path in self.BARE_GREGORIAN_SCREENS:
            with self.subTest(template=path):
                source = self._source(path)
                for line in source.splitlines():
                    if not bare.search(line):
                        continue
                    self.assertTrue(
                        "|hijri" in line or "datetime=" in line,
                        f"{path}: ميلاديٌّ بلا هجريٍّ ولا سمة — {line.strip()[:80]}",
                    )

    def test_the_companion_gregorian_carries_its_marker(self):
        """«م» ليست زينة.

        ‎2026-09-08‎ بجوار ‎1448/03/26 هـ‎ بلا علامة يُقرأ صيغةً ثانيةً للهجري
        لا تقويماً آخر. والمنصة تعلّمه بـ«م» في ``my_requests``، فيُوحَّد.
        """
        for path in (
            "reports/templates/reports/my_assignments.html",
            "reports/templates/reports/home.html",
            "reports/templates/reports/assignment_detail.html",
        ):
            with self.subTest(template=path):
                source = self._source(path)
                self.assertNotIn('">({{', source)  # لا ميلاديٌّ بين قوسين بلا علامة

    def test_the_teacher_home_notification_time_is_machine_readable(self):
        # كان ‎<time>‎ بلا سمة ‎datetime‎ — عنصرُ وقتٍ لا يحمل وقته.
        source = self._source("reports/templates/reports/home.html")
        self.assertIn(
            '<time datetime="{{ home_notification.created_at|date:\'c\' }}">',
            source,
        )

    # ── تطابق العدد والمعدود ─────────────────────────────────────────────

    def test_counted_nouns_on_teacher_screens_follow_their_number(self):
        assignments = self._source("reports/templates/reports/my_assignments.html")
        archive = self._source("reports/templates/reports/my_work_archive.html")

        self.assertIn('|arabic_count:"يوم,يومان,أيام,يوماً"', assignments)
        self.assertNotIn("{{ t.days_remaining }} يوم", assignments)
        self.assertIn('|arabic_count:"شاهد,شاهدان,شواهد,شاهداً"', archive)
        self.assertNotIn("{{ item.evidence_count }} شاهد", archive)

    def test_zero_days_remaining_reads_as_today(self):
        # «لا أيام» جوابٌ غريب لسؤال «متى؟»؛ والصواب «اليوم».
        source = self._source("reports/templates/reports/my_assignments.html")
        self.assertIn("{% if t.days_remaining == 0 %}اليوم{% else %}", source)

    # ── تسمية الإجراءات ──────────────────────────────────────────────────

    def test_report_row_actions_name_the_report(self):
        """«تعديل» عشر مرات لا تقول لقارئ الشاشة: تعديلَ أيّها."""
        source = self._source("reports/templates/reports/my_reports.html")

        for label in (
            'aria-label="تعديل تقرير:',
            'aria-label="مشاركة تقرير:',
            'aria-label="عرض تقرير:',
            'aria-label="نقل إلى سلة المحذوفات:',
        ):
            with self.subTest(label=label):
                self.assertIn(label, source)
        self.assertNotIn('aria-label="تعديل"', source)
        self.assertNotIn('aria-label="حذف"', source)

    # ── الذهبي حبراً ─────────────────────────────────────────────────────

    def test_the_open_status_badge_uses_the_ink_gold(self):
        """نغمةُ التنبيه سطحٌ وحدّ؛ وحملُها نصاً بـ‎10.5px‎ يعطي ‎3.16‎."""
        source = self._source("reports/templates/reports/home.html")

        self.assertIn(
            ".th-status.status-open { color: var(--id-gold-ink, var(--th-orange));",
            source,
        )

    def test_the_section_hint_ink_clears_aa_on_white(self):
        # ‎#708078‎ على أبيض ‎4.16‎؛ و‎#68766f‎ هو ‎--text-muted‎ ويعطي ‎4.76‎.
        source = self._source("reports/templates/reports/add_report.html")
        self.assertIn(".ar-section-option small{font-size:.72rem;color:#68766f", source)
        self.assertNotIn("color:#708078", source)
