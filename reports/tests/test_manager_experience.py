import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.forms import NotificationCreateForm
from reports.models import (
    AcademicYear,
    Department,
    DepartmentMembership,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    TeacherAchievementFile,
    SchoolLeadershipPortfolio,
    LeadershipPortfolioSection,
    LeadershipEvidenceImage,
    LeadershipEvidenceReport,
    Ticket,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ManagerExperienceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(
            name="مدرسة تجربة المدير",
            code="manager-experience",
            current_academic_year="1447-1448",
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة تجربة المدير",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500090001",
            name="مدير تجربة المستخدم",
            password="manager-pass",
            is_staff=True,
        )
        self.manager_membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.teacher = Teacher.objects.create_user(
            phone="500090002",
            name="معلم تجربة المستخدم",
            password="teacher-pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.report_type = ReportType.objects.create(
            school=self.school,
            code="manager-ux",
            name="تقارير تجربة المدير",
        )

    def _login_manager(self):
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_manager_selects_current_year_and_achievement_view_defaults_to_it(self):
        AcademicYear.objects.update(is_active=False)
        AcademicYear.objects.update_or_create(value="1447-1448", defaults={"is_active": True})
        AcademicYear.objects.update_or_create(value="1448-1449", defaults={"is_active": True})
        TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1446-1447",
        )
        self._login_manager()

        settings_response = self.client.get(reverse("reports:school_settings"))
        year_field = settings_response.context["form"].fields["current_academic_year"]
        self.assertEqual(
            [value for value, _label in year_field.choices],
            ["", "1447-1448", "1448-1449"],
        )

        update_response = self.client.post(
            reverse("reports:school_settings"),
            {"current_academic_year": "1448-1449", "share_link_default_days": 7},
        )
        self.assertEqual(update_response.status_code, 302)
        self.school.refresh_from_db()
        self.assertEqual(self.school.current_academic_year, "1448-1449")

        files_response = self.client.get(reverse("reports:achievement_school_files"))
        self.assertEqual(files_response.context["year"], "1448-1449")
        self.assertEqual(
            files_response.context["year_choices"],
            ["1446-1447", "1448-1449"],
        )

    def test_manager_can_update_school_email_and_invalid_email_is_rejected(self):
        self._login_manager()

        settings_response = self.client.get(reverse("reports:school_settings"))
        self.assertIn("email", settings_response.context["form"].fields)
        self.assertContains(settings_response, 'type="email"')

        update_response = self.client.post(
            reverse("reports:school_settings"),
            {
                "email": "School.Contact@Example.COM",
                "current_academic_year": "1447-1448",
                "share_link_default_days": 7,
            },
        )
        self.assertEqual(update_response.status_code, 302)
        self.school.refresh_from_db()
        self.assertEqual(self.school.email, "school.contact@example.com")

        invalid_response = self.client.post(
            reverse("reports:school_settings"),
            {
                "email": "invalid-email",
                "current_academic_year": "1447-1448",
                "share_link_default_days": 7,
            },
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertIn("email", invalid_response.context["form"].errors)
        self.school.refresh_from_db()
        self.assertEqual(self.school.email, "school.contact@example.com")

    def test_manager_has_one_clear_home_destination(self):
        self._login_manager()

        response = self.client.get(reverse("reports:home"))

        self.assertRedirects(
            response,
            reverse("reports:admin_dashboard"),
            fetch_redirect_response=False,
        )

    def test_manager_creates_school_leadership_portfolio_with_eight_sections(self):
        self._login_manager()

        list_response = self.client.get(reverse("reports:leadership_portfolio_list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, self.school.name)

        response = self.client.post(reverse("reports:leadership_portfolio_list"))

        portfolio = SchoolLeadershipPortfolio.objects.get(
            school=self.school,
            academic_year="1447-1448",
        )
        self.assertRedirects(
            response,
            reverse("reports:leadership_portfolio_detail", args=[portfolio.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(portfolio.manager, self.manager)
        self.assertEqual(portfolio.school_name, self.school.name)
        self.assertEqual(portfolio.sections.count(), 8)
        self.assertEqual(
            set(portfolio.sections.values_list("code", flat=True)),
            set(LeadershipPortfolioSection.Code.values),
        )
        completed_section = portfolio.sections.first()
        completed_section.is_completed = True
        completed_section.save(update_fields=["is_completed", "updated_at"])
        LeadershipEvidenceImage.objects.create(
            section=completed_section,
            image="leadership/evidence/first.png",
        )
        LeadershipEvidenceImage.objects.create(
            section=completed_section,
            image="leadership/evidence/second.png",
        )
        summary = self.client.get(reverse("reports:leadership_portfolio_list"))
        summary_portfolio = summary.context["portfolios"].get(pk=portfolio.pk)
        self.assertEqual(summary_portfolio.completed_count, 1)
        self.assertEqual(summary_portfolio.evidence_count, 2)
        detail_response = self.client.get(
            reverse("reports:leadership_portfolio_detail", args=[portfolio.pk])
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, self.school.name)

    def test_manager_creates_report_and_adds_it_to_leadership_section(self):
        self._login_manager()
        self.client.post(reverse("reports:leadership_portfolio_list"))
        portfolio = SchoolLeadershipPortfolio.objects.get(school=self.school)
        section = portfolio.sections.get(
            code=LeadershipPortfolioSection.Code.PLANNING
        )

        create_page = self.client.get(
            f"{reverse('reports:add_report')}?leadership_section={section.pk}"
        )
        self.assertEqual(create_page.status_code, 200)
        self.assertContains(create_page, "سيُضاف هذا التقرير تلقائيًا")
        self.assertContains(create_page, section.get_code_display())

        response = self.client.post(
            reverse("reports:add_report"),
            {
                "leadership_section": section.pk,
                "title": "اجتماع إعداد الخطة التشغيلية",
                "report_date": "2026-08-01",
                "beneficiaries_count": 12,
                "idea": "تم إعداد الخطة ومؤشرات المتابعة مع فريق المدرسة.",
                "category": self.report_type.code,
            },
        )

        report = Report.objects.get(title="اجتماع إعداد الخطة التشغيلية")
        self.assertRedirects(
            response,
            reverse("reports:leadership_portfolio_detail", args=[portfolio.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(report.teacher, self.manager)
        self.assertEqual(report.school, self.school)
        self.assertTrue(
            LeadershipEvidenceReport.objects.filter(
                section=section,
                report=report,
            ).exists()
        )

        detail = self.client.get(
            reverse("reports:leadership_portfolio_detail", args=[portfolio.pk])
        )
        self.assertContains(detail, report.title)
        printed = self.client.get(
            reverse("reports:leadership_portfolio_print", args=[portfolio.pk])
        )
        self.assertContains(printed, "التقارير القيادية الموثقة")
        self.assertContains(printed, report.title)

    def test_manager_cannot_link_another_users_report_to_leadership_file(self):
        self._login_manager()
        self.client.post(reverse("reports:leadership_portfolio_list"))
        portfolio = SchoolLeadershipPortfolio.objects.get(school=self.school)
        section = portfolio.sections.first()
        teacher_report = Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            teacher_name=self.teacher.name,
            title="تقرير معلم",
            report_date=timezone.localdate(),
            academic_year=portfolio.academic_year,
            category=self.report_type,
        )

        response = self.client.post(
            reverse("reports:leadership_portfolio_detail", args=[portfolio.pk]),
            {
                "action": "add_report_evidence",
                "section_id": section.pk,
                "report_id": teacher_report.pk,
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(LeadershipEvidenceReport.objects.exists())

    def test_teacher_cannot_access_leadership_portfolio(self):
        portfolio = SchoolLeadershipPortfolio.objects.create(
            school=self.school,
            manager=self.manager,
            academic_year="1447-1448",
        )
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        self.assertEqual(
            self.client.get(reverse("reports:leadership_portfolio_list")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("reports:leadership_portfolio_detail", args=[portfolio.pk])
            ).status_code,
            404,
        )

    def test_manager_cannot_access_another_school_leadership_portfolio(self):
        other_school = School.objects.create(
            name="مدرسة أخرى",
            code="other-leadership-school",
            current_academic_year="1447-1448",
        )
        portfolio = SchoolLeadershipPortfolio.objects.create(
            school=other_school,
            manager=self.manager,
            academic_year="1447-1448",
        )
        self._login_manager()

        response = self.client.get(
            reverse("reports:leadership_portfolio_detail", args=[portfolio.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_leadership_portfolio_print_keeps_school_identity(self):
        self.school.gender = "girls"
        self.school.save(update_fields=["gender"])
        portfolio = SchoolLeadershipPortfolio.objects.create(
            school=self.school,
            manager=self.manager,
            academic_year="1447-1448",
        )
        self._login_manager()
        self.client.post(reverse("reports:leadership_portfolio_list"))

        response = self.client.get(
            reverse("reports:leadership_portfolio_print", args=[portfolio.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ملف الأداء القيادي")
        self.assertContains(response, self.school.name)
        self.assertContains(response, "منصة توثيق")
        self.assertContains(response, "مديرة المدرسة")
        self.assertEqual(response.content.decode("utf-8").count('class="page'), 12)

    def test_dashboard_prioritizes_actionable_manager_work(self):
        Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            title="طلب يحتاج متابعة",
            body="تفاصيل الطلب",
            status=Ticket.Status.OPEN,
            is_platform=False,
        )
        TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
            status=TeacherAchievementFile.Status.SUBMITTED,
        )
        self._login_manager()

        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        # اللوحة كانت تجيب «ماذا ينتظرني؟» ثلاث مرات — بطاقات أرقام، وقائمة
        # بجوارها، ومواعيد تحتها — فدُمجت في «ما يحتاجك الآن». والمعنى الذي
        # يحرسه هذا الاختبار لم يتغيّر: العمل القابل للتنفيذ يظهر، ووجهته
        # قابلة للفتح. وتغيّر موضعُه فقط، فتغيّر ما يُقاس به.
        self.assertContains(response, "ما يحتاجك الآن")
        self.assertContains(response, "مساحات العمل")
        self.assertContains(response, "طلبات المدرسة المفتوحة")
        self.assertContains(response, "اعتمادات الإنجاز")
        self.assertContains(response, "إدارة طلبات المدرسة", count=0)
        self.assertNotContains(response, "Premium 2026")
        self.assertNotContains(response, "إحصائية الطلبات")
        self.assertEqual(response.context["pending_achievement_files"], 1)
        self.assertContains(
            response,
            reverse("reports:manager_school_tickets") + "?status=attention",
        )
        self.assertContains(
            response,
            reverse("reports:ticket_detail", args=[Ticket.objects.get().pk]),
        )

    def test_approving_an_achievement_file_replaces_the_previous_return_note(self):
        achievement_file = TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
            status=TeacherAchievementFile.Status.SUBMITTED,
            manager_notes="ملاحظة إرجاع قديمة",
        )
        self._login_manager()

        response = self.client.post(
            reverse("reports:achievement_file_detail", args=[achievement_file.pk]),
            {
                "action": "approve",
                "manager_notes": "تم الاستكمال والاعتماد مع الشكر.",
            },
        )

        self.assertRedirects(
            response,
            reverse("reports:achievement_file_detail", args=[achievement_file.pk]),
        )
        achievement_file.refresh_from_db()
        self.assertEqual(achievement_file.status, TeacherAchievementFile.Status.APPROVED)
        self.assertEqual(
            achievement_file.manager_notes,
            "تم الاستكمال والاعتماد مع الشكر.",
        )
        self.assertEqual(achievement_file.decided_by, self.manager)

    def test_dashboard_counts_teachers_without_counting_manager_and_guides_setup(self):
        self._login_manager()

        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertEqual(response.context["teachers_count"], 1)
        self.assertEqual(response.context["setup_completed"], 4)
        self.assertEqual(response.context["setup_total"], 7)
        self.assertEqual(response.context["setup_percent"], 57)
        self.assertContains(response, "جاهزية مساحة المدرسة")
        self.assertContains(response, "بيانات المدرسة والسنة الحالية")
        self.assertContains(response, "الأقسام")

    def test_manager_department_does_not_offer_impossible_officer_assignment(self):
        department = Department.objects.create(
            school=self.school,
            name="الإدارة",
            slug="manager",
            is_active=True,
        )
        self._login_manager()

        response = self.client.get(
            reverse("reports:department_members", args=[department.slug])
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["officer_assignment_allowed"])
        self.assertContains(response, "قسم الإدارة لا يحتاج مسؤول قسم منفصل")
        self.assertNotContains(response, 'name="action" value="set_officer"')

        posted = self.client.post(
            reverse("reports:department_members", args=[department.slug]),
            {"action": "set_officer", "teacher_id": self.teacher.pk},
            follow=True,
        )

        self.assertContains(posted, "قسم الإدارة لا يحتاج مسؤول قسم منفصل")
        self.assertFalse(
            DepartmentMembership.objects.filter(
                department=department,
                teacher=self.teacher,
                role_type=DepartmentMembership.OFFICER,
            ).exists()
        )

    def test_dashboard_has_no_weekly_summary_email_control(self):
        """The summary is in-app only, so the dashboard offers no send toggle."""
        self._login_manager()

        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "الملخص الأسبوعي على بريدك")
        self.assertNotContains(response, "weekly_summary_email_enabled")
        self.assertNotContains(response, "toggle_weekly_summary_email")

    def test_dashboard_period_never_hides_old_open_actionable_ticket(self):
        ticket = Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            title="طلب قديم ما زال مفتوحًا",
            body="يجب أن يبقى ظاهرًا في المتابعة",
            status=Ticket.Status.OPEN,
            is_platform=False,
        )
        Ticket.objects.filter(pk=ticket.pk).update(
            created_at=timezone.now() - timedelta(days=90),
        )
        self._login_manager()

        response = self.client.get(
            reverse("reports:api_admin_dashboard_data"),
            {"period": "month"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["kpis"]["tickets_open"], 1)
        self.assertEqual(payload["kpis"]["tickets_total"], 0)

    def test_attention_filter_and_ticket_pagination_keep_manager_context(self):
        tickets = [
            Ticket(
                school=self.school,
                creator=self.teacher,
                title=f"طلب متابعة {index}",
                body="تفاصيل",
                status=(
                    Ticket.Status.IN_PROGRESS
                    if index == 0
                    else Ticket.Status.OPEN
                ),
                is_platform=False,
            )
            for index in range(26)
        ]
        tickets.append(
            Ticket(
                school=self.school,
                creator=self.teacher,
                title="طلب مكتمل",
                body="لا يجب أن يظهر",
                status=Ticket.Status.DONE,
                is_platform=False,
            )
        )
        Ticket.objects.bulk_create(tickets)
        self._login_manager()

        first_page = self.client.get(
            reverse("reports:manager_school_tickets"),
            {"status": "attention"},
        )
        second_page = self.client.get(
            reverse("reports:manager_school_tickets"),
            {"status": "attention", "page": 2},
        )

        self.assertEqual(first_page.context["tickets"].paginator.count, 26)
        self.assertNotContains(first_page, "طلب مكتمل")
        self.assertContains(first_page, "?page=2&status=attention")
        self.assertEqual(len(second_page.context["tickets"].object_list), 1)

    def test_dashboard_archive_actions_explain_inactive_addon(self):
        self._login_manager()

        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertContains(response, reverse("reports:school_archive"))
        self.assertContains(
            response,
            reverse("reports:my_subscription") + "#archiveOrder",
        )
        self.assertNotContains(response, reverse("reports:school_data_export"))

    def test_school_audit_log_is_reachable_and_has_working_clear_links(self):
        self._login_manager()

        response = self.client.get(
            reverse("reports:school_audit_logs"),
            {"action": "login"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "سجل العمليات")
        self.assertGreaterEqual(
            response.content.decode("utf-8").count(
                reverse("reports:school_audit_logs")
            ),
            2,
        )

    def test_achievement_review_filters_by_status_and_excludes_manager(self):
        TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
            status=TeacherAchievementFile.Status.SUBMITTED,
        )
        second_teacher = Teacher.objects.create_user(
            phone="500090003",
            name="معلم بلا ملف",
            password="teacher-pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=second_teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self._login_manager()

        submitted = self.client.get(
            reverse("reports:achievement_school_files"),
            {"year": "1447-1448", "status": "submitted"},
        )
        missing = self.client.get(
            reverse("reports:achievement_school_files"),
            {"year": "1447-1448", "status": "missing"},
        )

        self.assertEqual([row["teacher"] for row in submitted.context["rows"]], [self.teacher])
        self.assertEqual(
            [row["teacher"] for row in missing.context["rows"]],
            [second_teacher],
        )

    def test_manager_navigation_includes_school_reports_on_mobile_and_desktop(self):
        self._login_manager()

        response = self.client.get(reverse("reports:manage_teachers"))
        school_reports_url = reverse("reports:admin_reports")

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(
            response.content.decode("utf-8").count(school_reports_url),
            3,
        )
        self.assertContains(response, "لوحة المدرسة")
        self.assertNotContains(response, "لوحة المدير")
        self.assertContains(response, "فريق المدرسة")
        self.assertContains(response, "التواصل")

    def test_manager_notification_recipients_are_active_teachers_not_manager(self):
        form = NotificationCreateForm(
            user=self.manager,
            active_school=self.school,
            mode="notification",
        )

        self.assertEqual(
            set(form.fields["teachers"].queryset.values_list("id", flat=True)),
            {self.teacher.id},
        )

    def test_circular_copy_and_required_fields_match_behavior(self):
        form = NotificationCreateForm(
            data={
                "message": "نص بلا عنوان",
                "teachers": [str(self.teacher.id)],
            },
            user=self.manager,
            active_school=self.school,
            mode="circular",
        )

        self.assertEqual(form.fields["title"].label, "عنوان التعميم")
        self.assertEqual(form.fields["message"].label, "نص التعميم")
        self.assertTrue(form.fields["title"].required)
        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_manager_core_templates_do_not_use_csp_blocked_inline_handlers(self):
        template_names = [
            "admin_dashboard.html",
            "manage_teachers.html",
            "bulk_import_teachers.html",
            "admin_reports.html",
            "tickets_inbox.html",
            "send_notification.html",
            "send_circular.html",
            "achievement_school_files.html",
            "school_settings.html",
            "school_archive.html",
            "audit_logs.html",
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

    def test_navigation_cache_is_invalidated_when_membership_role_changes(self):
        self._login_manager()
        manager_response = self.client.get(reverse("reports:my_profile"))
        self.assertTrue(manager_response.context["IS_SCHOOL_MANAGER"])

        self.manager_membership.role_type = SchoolMembership.RoleType.TEACHER
        self.manager_membership.save(update_fields=["role_type"])

        teacher_response = self.client.get(reverse("reports:my_profile"))
        self.assertFalse(teacher_response.context["IS_SCHOOL_MANAGER"])
        self.assertFalse(teacher_response.context["SHOW_SCHOOL_REPORTS_LINK"])
