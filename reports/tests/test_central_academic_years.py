"""Platform calendar authority, school rollover and immutable historical work."""
from datetime import date
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import Permission
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from personal.models import PersonalWorkspace
from reports.academic_years import (
    MINISTRY_CALENDAR_URL, approved_academic_year_options,
    current_academic_year_value, hijri_academic_year_options, sync_academic_years,
)
from reports.forms import PlatformAcademicYearForm
from reports.middleware import ActiveSchoolGuardMiddleware
from reports.guidance import school_readiness
from reports.models import (
    AcademicYear, Report, School, SchoolMembership, SchoolSubscription,
    SubscriptionPlan, Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class CentralAcademicYearTests(TestCase):
    CURRENT = "1450-1451"
    FUTURE = "1451-1452"
    OLD = "1449-1450"
    TODAY = date(2026, 10, 10)

    def setUp(self):
        self.today_patch = patch("reports.academic_years.timezone.localdate", return_value=self.TODAY)
        self.today_patch.start()
        self.addCleanup(self.today_patch.stop)
        AcademicYear.objects.update(is_active=False)
        # Legacy migration 0061 may have seeded these labels by estimation.
        # Keep each test's synthetic fixtures independent of that old range.
        AcademicYear.objects.filter(value__in=[self.CURRENT, self.FUTURE, self.OLD, "1452-1453"]).delete()
        self.current = AcademicYear.objects.create(
            value=self.CURRENT, is_active=True, start_date=date(2026, 8, 23),
            end_date=date(2027, 6, 24), source_url=MINISTRY_CALENDAR_URL,
        )
        self.future = AcademicYear.objects.create(
            value=self.FUTURE, is_active=True, start_date=date(2027, 8, 22),
            end_date=date(2028, 6, 22), source_url=MINISTRY_CALENDAR_URL,
        )
        self.school = School.objects.create(
            name="مدرسة التقويم المركزي", code="central-calendar", current_academic_year=self.OLD,
        )
        self.other = School.objects.create(
            name="مدرسة أخرى", code="central-calendar-other", current_academic_year=self.OLD,
        )
        plan = SubscriptionPlan.objects.create(name="خطة تقويم", price=0, days_duration=30, max_teachers=0)
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        SchoolSubscription.objects.create(school=self.other, plan=plan)
        self.manager = Teacher.objects.create_user(phone="0557900001", name="مدير", password="test-pass", is_staff=True)
        SchoolMembership.objects.create(school=self.school, teacher=self.manager, role_type=SchoolMembership.RoleType.MANAGER)
        self.owner = Teacher.objects.create_superuser(phone="0557900002", name="مالك", password="test-pass")

    def _login(self, user, client=None):
        client = client or self.client
        client.force_login(user)
        session = client.session
        session["active_school_id"] = self.school.pk
        session.save()
        return client

    def _form_payload(self, **updates):
        payload = {
            "action": "add", "value": "1452-1453", "start_date": "2028-08-20",
            "end_date": "2029-06-21", "source_url": MINISTRY_CALENDAR_URL, "is_active": "on",
        }
        payload.update(updates)
        return payload

    def _guard(self, user, school):
        request = RequestFactory().get("/dashboard/", HTTP_ACCEPT="application/json")
        request.user = user
        request.session = {"active_school_id": school.pk}
        response = ActiveSchoolGuardMiddleware(lambda req: HttpResponse("ok"))(request)
        return request, response

    def test_future_publishing_never_changes_current_year_early(self):
        self.assertEqual(current_academic_year_value(), self.CURRENT)
        self.assertEqual(current_academic_year_value(date(2027, 8, 21)), self.CURRENT)
        self.assertEqual(current_academic_year_value(date(2027, 8, 22)), self.FUTURE)

    def test_no_calendar_means_no_estimated_years(self):
        AcademicYear.objects.update(is_active=False)
        self.assertEqual(current_academic_year_value(), "")
        self.assertEqual(approved_academic_year_options(), [])
        self.assertEqual(hijri_academic_year_options(), [])

    def test_new_document_and_plan_forms_select_the_central_year(self):
        from reports.forms_documents import DocumentUploadForm
        from reports.forms_plans import PlanForm

        document = DocumentUploadForm(school=self.school, uploader=self.manager)
        plan = PlanForm(school=self.school, owner=self.manager)
        self.assertEqual(document["academic_year"].value(), self.CURRENT)
        self.assertEqual(plan["academic_year"].value(), self.CURRENT)
        self.assertTrue(PlanForm({"title": "خطة", "academic_year": "1500-1501"}, school=self.school, owner=self.manager).has_error("academic_year"))

    def test_new_school_automatically_gets_platform_current_year(self):
        school = School.objects.create(name="مدرسة جديدة", code="calendar-new")
        self.assertEqual(school.current_academic_year, self.CURRENT)

    def test_guard_rolls_authorized_school_and_membership_context(self):
        request, response = self._guard(self.manager, self.school)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.active_school.current_academic_year, self.CURRENT)
        self.assertEqual(request._active_membership.school.current_academic_year, self.CURRENT)
        self.school.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.school.current_academic_year, self.CURRENT)
        self.assertEqual(self.other.current_academic_year, self.OLD)

    def test_guard_never_changes_foreign_school(self):
        request, response = self._guard(self.manager, self.other)
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(request.active_school)
        self.other.refresh_from_db()
        self.assertEqual(self.other.current_academic_year, self.OLD)

    def test_rollover_never_relabels_historical_reports_or_loses_history(self):
        report = Report.objects.create(
            school=self.school, teacher=self.manager, title="عمل محفوظ", report_date=date(2025, 10, 10),
            academic_year=self.OLD,
        )
        workspace = PersonalWorkspace.objects.create(owner=self.manager, current_academic_year=self.OLD)
        result = sync_academic_years()
        self.assertEqual(result, {"value": self.CURRENT, "schools": 2, "teachers": 1})
        report.refresh_from_db()
        workspace.refresh_from_db()
        self.school.refresh_from_db()
        self.assertEqual(report.academic_year, self.OLD)
        self.assertEqual(workspace.current_academic_year, self.CURRENT)
        self.assertIn(self.OLD, hijri_academic_year_options(self.school))
        self.assertEqual(sync_academic_years(), {"value": self.CURRENT, "schools": 0, "teachers": 0})

    def test_school_settings_ignore_crafted_year_and_still_save_contact(self):
        self._login(self.manager)
        response = self.client.post(reverse("reports:school_settings"), {
            "current_academic_year": self.FUTURE, "allowed_academic_years": [self.FUTURE],
            "email": "school@example.test", "phone": "0551234567", "share_link_default_days": "7",
        })
        self.assertEqual(response.status_code, 302)
        self.school.refresh_from_db()
        self.assertEqual(self.school.current_academic_year, self.CURRENT)
        self.assertEqual(self.school.allowed_academic_years, [])
        self.assertEqual(self.school.email, "school@example.test")
        page = self.client.get(reverse("reports:school_settings"))
        self.assertTrue(page.context["form"].fields["current_academic_year"].disabled)

    def test_all_school_roles_are_denied_platform_calendar_mutation(self):
        roles = [
            (SchoolMembership.RoleType.TEACHER, SchoolMembership.JobTitle.TEACHER),
            (SchoolMembership.RoleType.DEPUTY, ""),
            (SchoolMembership.RoleType.ADMIN_STAFF, SchoolMembership.JobTitle.ADMIN_STAFF),
            (SchoolMembership.RoleType.ADMIN_STAFF, SchoolMembership.JobTitle.LAB_TECH),
        ]
        users = [self.manager]
        for index, (role, title) in enumerate(roles, start=3):
            user = Teacher.objects.create_user(phone=f"055790000{index}", name=f"مستخدم {index}", password="test-pass", is_staff=True)
            SchoolMembership.objects.create(school=self.school, teacher=user, role_type=role, job_title=title)
            users.append(user)
        for user in users:
            with self.subTest(user=user.name):
                self._login(user)
                response = self.client.post(reverse("reports:platform_academic_years"), self._form_payload())
                self.assertEqual(response.status_code, 302)
                self.assertFalse(AcademicYear.objects.filter(value="1452-1453").exists())

    def test_platform_owner_can_publish_and_edit_full_calendar_form(self):
        self._login(self.owner)
        response = self.client.post(reverse("reports:platform_academic_years"), self._form_payload())
        self.assertEqual(response.status_code, 302)
        year = AcademicYear.objects.get(value="1452-1453")
        self.assertTrue(year.is_active)
        page = self.client.get(reverse("reports:platform_academic_years"), {"edit": year.pk})
        self.assertEqual(page.status_code, 200)
        for field in ("value", "start_date", "end_date", "source_url", "is_active"):
            self.assertContains(page, f'name="{field}"')
        self.assertContains(page, "css/platform-academic-years.css")
        self.assertContains(page, 'value="2028-08-20"')
        self.assertNotContains(page, 'value="generate"')
        edited = self.client.post(reverse("reports:platform_academic_years"), self._form_payload(
            action="save", id=year.pk, end_date="2029-06-22",
        ))
        self.assertEqual(edited.status_code, 302)
        year.refresh_from_db()
        self.assertEqual(year.end_date, date(2029, 6, 22))

    def test_publish_requires_complete_valid_calendar_and_official_source(self):
        for changes, expected in (
            ({"start_date": "", "end_date": ""}, "start_date"),
            ({"source_url": "https://example.test/calendar"}, "source_url"),
            ({"source_url": "https://moe.gov.sa.example.test/calendar"}, "source_url"),
            ({"start_date": "2029-06-22"}, "end_date"),
            ({"start_date": "2026-10-01", "end_date": "2027-06-10"}, "start_date"),
        ):
            with self.subTest(changes=changes):
                form = PlatformAcademicYearForm(self._form_payload(**changes))
                self.assertFalse(form.is_valid())
                self.assertIn(expected, form.errors)

    def test_validation_errors_render_next_to_calendar_fields(self):
        self._login(self.owner)
        response = self.client.post(reverse("reports:platform_academic_years"), self._form_payload(start_date=""))
        self.assertEqual(response.status_code, 200)
        self.assertIn("start_date", response.context["form"].errors)
        self.assertContains(response, 'id="id_start_date_error"')
        self.assertContains(response, 'aria-invalid="true"')
        self.assertFalse(AcademicYear.objects.filter(value="1452-1453").exists())

    def test_django_admin_rejects_school_manager_even_with_all_model_permissions(self):
        self.manager.user_permissions.set(Permission.objects.filter(content_type__app_label="reports", content_type__model="academicyear"))
        request = RequestFactory().get("/admin/reports/academicyear/")
        request.user = Teacher.objects.get(pk=self.manager.pk)
        calendar_admin = admin.site._registry[AcademicYear]
        self.assertFalse(calendar_admin.has_view_permission(request))
        self.assertFalse(calendar_admin.has_add_permission(request))
        self.assertFalse(calendar_admin.has_change_permission(request, self.current))
        self.assertFalse(calendar_admin.has_delete_permission(request))

    def test_used_historical_year_cannot_be_deleted_or_renamed(self):
        old = AcademicYear.objects.create(value=self.OLD)
        Report.objects.create(school=self.school, teacher=self.manager, title="تقرير قديم", report_date=date(2025, 10, 10), academic_year=self.OLD)
        self._login(self.owner)
        removed = self.client.post(reverse("reports:platform_academic_years"), {"action": "delete", "id": old.pk})
        self.assertEqual(removed.status_code, 302)
        self.assertTrue(AcademicYear.objects.filter(pk=old.pk).exists())
        changed = self.client.post(reverse("reports:platform_academic_years"), self._form_payload(action="save", id=old.pk, is_active=""))
        self.assertEqual(changed.status_code, 200)
        old.refresh_from_db()
        self.assertEqual(old.value, self.OLD)

    def test_current_year_cannot_be_unpublished_or_deleted(self):
        self._login(self.owner)
        for action in ("toggle", "delete"):
            response = self.client.post(reverse("reports:platform_academic_years"), {"action": action, "id": self.current.pk})
            self.assertEqual(response.status_code, 302)
            self.current.refresh_from_db()
            self.assertTrue(self.current.is_active)

    def test_calendar_publication_requires_csrf(self):
        client = self._login(self.owner, Client(enforce_csrf_checks=True))
        self.assertEqual(client.post(reverse("reports:platform_academic_years"), self._form_payload()).status_code, 403)

    def test_manager_missing_calendar_is_directed_to_platform_support(self):
        AcademicYear.objects.update(is_active=False)
        self.school.current_academic_year = ""
        self.school.save(update_fields=["current_academic_year"])
        guidance = school_readiness(self.school)
        profile_step = next(step for step in guidance["steps"] if step["key"] == "profile")
        self.assertFalse(profile_step["complete"])
        self.assertEqual(profile_step["url"], reverse("reports:support_ticket_create"))
        self.assertIn("إدارة المنصة", profile_step["description"])

    def test_first_request_school_autoselection_uses_platform_year(self):
        from reports.views._helpers import _get_active_school

        request = RequestFactory().get("/dashboard/")
        request.user = self.manager
        request.session = {}
        resolved = _get_active_school(request)
        self.assertEqual(resolved.current_academic_year, self.CURRENT)
        self.assertEqual(request.active_school.current_academic_year, self.CURRENT)
        self.assertEqual(request.session["active_school_id"], self.school.pk)

    def test_malformed_or_missing_calendar_id_cannot_create_instead_of_editing(self):
        self._login(self.owner)
        initial_count = AcademicYear.objects.count()
        for value in ("not-a-number", "0", "999999999999999999999999999999999999", "987654321"):
            for action in ("save", "toggle", "delete"):
                with self.subTest(value=value, action=action):
                    response = self.client.post(reverse("reports:platform_academic_years"), self._form_payload(action=action, id=value))
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(AcademicYear.objects.count(), initial_count)
        for action in ("toggle", "delete"):
            response = self.client.post(reverse("reports:platform_academic_years"), {"action": action})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(AcademicYear.objects.count(), initial_count)
