import re
from datetime import date
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.forms import TicketCreateForm
from reports.models import (
    AcademicYear,
    Department,
    DepartmentMembership,
    Report,
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    TeacherAchievementFile,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class TeacherExperienceTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(
            name="مدرسة تجربة المعلم",
            code="teacher-experience",
            current_academic_year="1447-1448",
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة تجربة المعلم",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.teacher = Teacher.objects.create_user(
            phone="500091001",
            name="معلم تجربة المستخدم",
            password="teacher-pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

    def _login_teacher(self):
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_achievement_creation_uses_only_school_current_academic_year(self):
        AcademicYear.objects.update(is_active=False)
        AcademicYear.objects.update_or_create(value="1447-1448", defaults={"is_active": True})
        AcademicYear.objects.update_or_create(value="1448-1449", defaults={"is_active": True})
        TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1446-1447",
        )
        self._login_teacher()

        response = self.client.get(reverse("reports:achievement_my_files"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["create_form"].fields["academic_year"].choices),
            [("1447-1448", "1447-1448 هـ")],
        )

    def test_achievement_creation_waits_for_manager_to_select_current_year(self):
        self.school.current_academic_year = ""
        self.school.save(update_fields=["current_academic_year"])
        self._login_teacher()

        response = self.client.get(reverse("reports:achievement_my_files"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["create_form"].fields["academic_year"].choices),
            [],
        )
        self.assertContains(response, "لم تُحدد السنة الدراسية الحالية")
        self.assertContains(response, "مدير المدرسة")

    def test_teacher_cannot_move_achievement_file_outside_school_current_year(self):
        achievement_file = TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1446-1447",
        )
        self._login_teacher()

        response = self.client.post(
            reverse("reports:achievement_file_update_year", args=[achievement_file.pk]),
            {"academic_year": "1448-1449"},
        )

        self.assertEqual(response.status_code, 302)
        achievement_file.refresh_from_db()
        self.assertEqual(achievement_file.academic_year, "1446-1447")

    def test_achievement_print_has_structured_tawtheeq_portfolio_sections(self):
        achievement_file = TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
        )
        self._login_teacher()

        response = self.client.get(
            reverse("reports:achievement_file_print", args=[achievement_file.pk])
        )
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ملف الإنجاز المهني")
        self.assertContains(response, "محتويات الملف")
        self.assertContains(response, "الفصل الأول")
        self.assertContains(response, "شواهد الأداء المهني")
        self.assertContains(response, "منصة توثيق")
        self.assertEqual(html.count('class="page page-break criterion-page"'), 11)

    def test_achievement_print_uses_feminine_role_for_girls_school(self):
        self.school.gender = "girls"
        self.school.save(update_fields=["gender"])
        achievement_file = TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
        )
        self._login_teacher()

        response = self.client.get(
            reverse("reports:achievement_file_print", args=[achievement_file.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ملف الإنجاز المهني")
        self.assertContains(response, "للمعلمة")

    def test_home_prioritizes_daily_teacher_work_without_repeated_legacy_sections(self):
        self._login_teacher()

        response = self.client.get(reverse("reports:home"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مساحة عمل المعلم")
        self.assertContains(response, "ما يحتاج انتباهك الآن")
        self.assertContains(response, "إجراءاتك اليومية")
        self.assertContains(response, "أحدث تقاريري")
        self.assertContains(response, "طلباتي المدرسية")
        self.assertContains(response, "إضافة تقرير")
        self.assertNotContains(response, "متابعة اليوم")
        self.assertNotContains(response, "مساحة عملي")
        self.assertNotContains(response, "Premium 2026")
        self.assertNotContains(response, "أحدث النشاطات")
        self.assertEqual(len(re.findall(r"<h1\b", html, re.IGNORECASE)), 1)
        self.assertLess(
            html.index('id="teacherAttentionTitle"'),
            html.index('id="teacherActionsTitle"'),
        )
        self.assertLess(
            html.index('id="teacherActionsTitle"'),
            html.index('id="recentReportsTitle"'),
        )
        self.assertEqual(
            response.context["active_requests_count"],
            response.context["req_stats"]["open"] + response.context["req_stats"]["in_progress"],
        )

    def test_regular_teacher_navigation_matches_real_daily_destinations(self):
        self._login_teacher()

        response = self.client.get(reverse("reports:home"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(html.count(reverse("reports:my_reports")), 3)
        self.assertGreaterEqual(html.count(reverse("reports:my_requests")), 3)
        self.assertGreaterEqual(html.count(reverse("reports:achievement_my_files")), 3)
        self.assertNotIn(reverse("reports:assigned_to_me"), html)
        self.assertNotIn(reverse("reports:support_ticket_create"), html)
        self.assertNotIn(reverse("reports:school_archive"), html)

    def test_regular_teacher_never_sees_archive_nav_even_if_archive_addon_is_active(self):
        SchoolArchiveAddon.objects.create(
            school=self.school,
            is_enabled=True,
            start_date=timezone.localdate(),
            paid_amount=399,
        )
        self._login_teacher()

        response = self.client.get(reverse("reports:home"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(reverse("reports:school_archive"), html)

    def test_department_officer_navigation_adds_tasks_and_department_reports(self):
        department = Department.objects.create(
            school=self.school,
            name="قسم تجربة المعلم",
            slug="teacher-experience-dept",
        )
        DepartmentMembership.objects.create(
            department=department,
            teacher=self.teacher,
            role_type=DepartmentMembership.OFFICER,
        )
        self._login_teacher()

        response = self.client.get(reverse("reports:home"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["IS_OFFICER"])
        self.assertIn(reverse("reports:assigned_to_me"), html)
        self.assertIn(reverse("reports:officer_reports"), html)
        self.assertContains(response, "الطلبات المسندة")
        self.assertContains(response, "تقارير قسمي")
        self.assertContains(response, "تقاريري")
        self.assertContains(response, "طلباتي")

    def test_teacher_core_pages_render_with_one_clear_page_heading(self):
        self._login_teacher()
        route_names = [
            "home",
            "add_report",
            "my_reports",
            "request_create",
            "my_requests",
            "assigned_to_me",
            "my_notifications",
            "my_circulars",
            "achievement_my_files",
            "my_profile",
        ]

        for route_name in route_names:
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(f"reports:{route_name}"))
                html = response.content.decode("utf-8")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    len(re.findall(r"<h1\b", html, re.IGNORECASE)),
                    1,
                    f"{route_name} must render exactly one h1",
                )

    def test_teacher_facing_copy_describes_internal_school_workflow(self):
        self._login_teacher()

        request_response = self.client.get(reverse("reports:request_create"))
        circulars_response = self.client.get(reverse("reports:my_circulars"))
        reports_response = self.client.get(reverse("reports:my_reports"))

        self.assertContains(request_response, "إنشاء طلب مدرسي جديد")
        self.assertContains(request_response, "تفاصيل الطلب")
        self.assertNotContains(request_response, "تفاصيل المشكلة")
        self.assertContains(circulars_response, "التعاميم الواردة")
        self.assertNotContains(circulars_response, "<span>تعاميمي</span>", html=True)
        self.assertContains(reports_response, "سجل تقاريري")

        form = TicketCreateForm(active_school=self.school)
        self.assertEqual(form.fields["title"].label, "عنوان الطلب")
        self.assertEqual(form.fields["body"].label, "تفاصيل الطلب")
        self.assertTrue(form.fields["body"].required)

    def test_my_reports_exposes_each_management_action_without_a_hidden_menu(self):
        report = Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            teacher_name=self.teacher.name,
            title="تقرير واضح الإجراءات",
            idea="وصف التقرير",
            report_date=date.today(),
        )
        self._login_teacher()

        response = self.client.get(reverse("reports:my_reports"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count('class="report-action-list"'), 2)
        self.assertNotIn("<details class=\"ui-actions-menu\"", html)
        for label in ("عرض", "تعديل", "مشاركة", "حذف"):
            with self.subTest(label=label):
                self.assertEqual(html.count(f"<span>{label}</span>"), 2)
        for route_name in (
            "report_print",
            "edit_my_report",
            "report_share_manage",
            "delete_my_report",
        ):
            with self.subTest(route_name=route_name):
                self.assertIn(reverse(f"reports:{route_name}", args=[report.pk]), html)

    def test_request_create_wires_external_recipients_loader(self):
        self._login_teacher()

        department = Department.objects.create(
            school=self.school,
            name="الإدارة",
            slug="admin-office",
        )
        DepartmentMembership.objects.create(
            department=department,
            teacher=self.teacher,
            role_type=DepartmentMembership.TEACHER,
        )

        response = self.client.get(reverse("reports:request_create"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-members-url="/api/department-members/"', html)
        self.assertIn("/static/js/request-create-recipients.js", html)

    def test_department_members_api_returns_department_teachers(self):
        self._login_teacher()

        department = Department.objects.create(
            school=self.school,
            name="الإدارة",
            slug="admin-office",
        )
        DepartmentMembership.objects.create(
            department=department,
            teacher=self.teacher,
            role_type=DepartmentMembership.TEACHER,
        )

        response = self.client.get(
            reverse("reports:api_department_members"),
            {"department": department.slug},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("results", payload)
        self.assertTrue(
            any(item.get("id") == self.teacher.id for item in payload["results"]),
            "Expected the current teacher to appear in recipients API results",
        )

    def test_department_reports_explains_missing_report_type_configuration(self):
        self._login_teacher()
        department = Department.objects.create(
            school=self.school,
            name="قسم غير مهيأ",
            slug="unconfigured-department",
        )
        DepartmentMembership.objects.create(
            department=department,
            teacher=self.teacher,
            role_type=DepartmentMembership.TEACHER,
        )

        response = self.client.get(reverse("reports:department_reports"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["configuration_missing"])
        self.assertContains(response, "تقارير القسم غير مهيأة بعد")
        self.assertContains(response, "الإدارة لم تربط القسم بأنواع التقارير بعد")

    def test_teacher_core_templates_do_not_use_csp_blocked_inline_handlers(self):
        template_names = [
            "home.html",
            "add_report.html",
            "my_reports.html",
            "request_create.html",
            "my_requests.html",
            "assigned_to_me.html",
            "ticket_print.html",
            "my_notifications.html",
            "my_circulars.html",
            "achievement_my_files.html",
            "partials/passkey_enrollment_prompt.html",
        ]
        templates_dir = Path(settings.BASE_DIR) / "reports" / "templates" / "reports"
        inline_handler = re.compile(
            r"\son(?:click|change|submit|input|keydown|keyup|load|error|blur)\s*=",
            re.IGNORECASE,
        )

        for template_name in template_names:
            source = (templates_dir / template_name).read_text(encoding="utf-8")
            self.assertIsNone(
                inline_handler.search(source),
                f"{template_name} contains a CSP-blocked inline event handler",
            )

        for template_name in (
            "achievement_my_files.html",
            "partials/passkey_enrollment_prompt.html",
            "ticket_print.html",
        ):
            source = (templates_dir / template_name).read_text(encoding="utf-8")
            self.assertIn(
                'nonce="{{ CSP_NONCE }}" data-cfasync="false"',
                source,
                f"{template_name} must bypass Rocket Loader to preserve its CSP nonce",
            )

            report_authoring_source = (
                Path(settings.BASE_DIR) / "static/js/report-authoring.js"
            ).read_text(encoding="utf-8")
            self.assertIn(
                'dateInput.addEventListener("input", syncDateFields);',
                report_authoring_source,
            )
            self.assertIn(
                'dateInput.addEventListener("change", syncDateFields);',
                report_authoring_source,
            )


@override_settings(ALLOWED_HOSTS=["testserver"])
class AchievementFileOwnerLockTests(TestCase):
    """ملف الإنجاز يخرج من يد صاحبه متى خرج إلى مراجعه.

    الشاشة كانت تعرف هذا (``can_edit_teacher``) فتمنع تحرير المحاور بعد
    الإرسال، لكنّ الحذف وتعديل السنة كانا خارج المعرفة: يمحو المعلّمُ ملفاً
    اعتمده مديرُه محواً نهائياً، أو ينقل شواهد سنةٍ إلى سجلّ سنةٍ أخرى.
    """

    def setUp(self):
        self.school = School.objects.create(
            name="مدرسة قفل الإنجاز",
            code="achievement-lock",
            current_academic_year="1447-1448",
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة قفل الإنجاز", price=0, days_duration=30, max_teachers=0
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.teacher = Teacher.objects.create_user(
            phone="500092001", name="معلم ملف الإنجاز", password="teacher-pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def _file(self, status: str) -> TeacherAchievementFile:
        return TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
            status=status,
        )

    def test_the_states_that_belong_to_the_owner(self):
        for status in (
            TeacherAchievementFile.Status.DRAFT,
            TeacherAchievementFile.Status.RETURNED,
        ):
            self.assertTrue(self._file(status).is_editable_by_owner, status)
            TeacherAchievementFile.objects.all().delete()

        for status in (
            TeacherAchievementFile.Status.SUBMITTED,
            TeacherAchievementFile.Status.APPROVED,
        ):
            self.assertFalse(self._file(status).is_editable_by_owner, status)
            TeacherAchievementFile.objects.all().delete()

    def test_an_approved_file_survives_a_delete_request_from_its_owner(self):
        ach_file = self._file(TeacherAchievementFile.Status.APPROVED)

        self.client.post(reverse("reports:achievement_file_delete", args=[ach_file.pk]))

        self.assertTrue(TeacherAchievementFile.objects.filter(pk=ach_file.pk).exists())

    def test_a_submitted_file_survives_a_delete_request_from_its_owner(self):
        ach_file = self._file(TeacherAchievementFile.Status.SUBMITTED)

        self.client.post(reverse("reports:achievement_file_delete", args=[ach_file.pk]))

        self.assertTrue(TeacherAchievementFile.objects.filter(pk=ach_file.pk).exists())

    def test_a_draft_is_still_deletable_by_its_owner(self):
        ach_file = self._file(TeacherAchievementFile.Status.DRAFT)

        self.client.post(reverse("reports:achievement_file_delete", args=[ach_file.pk]))

        self.assertFalse(TeacherAchievementFile.objects.filter(pk=ach_file.pk).exists())

    def test_an_approved_file_keeps_its_year(self):
        """السنة هويّة الملف لا حقلاً فيه، فتغييرها يحرّك شواهد سنةٍ إلى غيرها."""
        ach_file = self._file(TeacherAchievementFile.Status.APPROVED)
        self.school.current_academic_year = "1448-1449"
        self.school.save(update_fields=["current_academic_year"])

        self.client.post(
            reverse("reports:achievement_file_update_year", args=[ach_file.pk]),
            {"academic_year": "1448-1449"},
        )

        ach_file.refresh_from_db()
        self.assertEqual(ach_file.academic_year, "1447-1448")

    def test_the_list_hides_the_actions_the_server_would_refuse(self):
        ach_file = self._file(TeacherAchievementFile.Status.APPROVED)

        body = self.client.get(reverse("reports:achievement_my_files")).content.decode("utf-8")

        self.assertNotIn(reverse("reports:achievement_file_delete", args=[ach_file.pk]), body)
        self.assertNotIn(reverse("reports:achievement_file_update_year", args=[ach_file.pk]), body)


@override_settings(ALLOWED_HOSTS=["testserver"])
class UnsetAcademicYearIsSurfacedToTheManagerTests(TestCase):
    """العائق الصامت يُرفع إلى من يملك رفعه.

    اشتراطُ سنةٍ يحددها المدير مقصود، لكنه كان صامتاً في اتجاهه: المعلّم يقرأ
    «لم تُحدد السنة» ولا يملك تحديدها، والمدير لا يقرأ شيئاً ولا يعلم أن مساراً
    كاملاً متوقّف عنده.
    """

    def setUp(self):
        self.school = School.objects.create(
            name="مدرسة بلا سنة", code="year-unset", current_academic_year=""
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة بلا سنة", price=0, days_duration=30, max_teachers=0
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500093001", name="مدير بلا سنة", password="manager-pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_the_dashboard_tells_the_manager_and_points_at_the_setting(self):
        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertContains(response, "السنة الدراسية الحالية غير محددة")
        self.assertContains(response, reverse("reports:school_settings"))

    def test_the_alert_carries_the_year_from_the_calendar(self):
        """الضبط نظرةٌ لا بحث: السنة المقترحة معروضة في التنبيه نفسه."""
        from reports.hijri_utils import current_academic_year

        suggestion = current_academic_year()
        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertEqual(response.context["academic_year_suggestion"], suggestion)
        if suggestion:
            self.assertContains(response, suggestion)

    def test_a_school_with_a_year_shows_no_alert(self):
        self.school.current_academic_year = "1447-1448"
        self.school.save(update_fields=["current_academic_year"])

        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertNotContains(response, "السنة الدراسية الحالية غير محددة")
