"""Maintenance authorization, tenant scope, audit identity, and stale-tab safety."""

from django.contrib.auth import SESSION_KEY as AUTH_SESSION_KEY
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from personal.models import PersonalReport, PersonalWorkspace
from personal.services import ensure_personal_subscription
from reports.models import AuditLog, School, Teacher
from reports.platform_support import CONTEXT_FIELD, SESSION_KEY
from reports.audit_labels import describe


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PlatformSupportTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = Teacher.objects.create_superuser(phone="0557990101", name="مالك المنصة", password="SupportTest#2026")
        self.teacher = Teacher.objects.create_user(phone="0557990102", name="المعلم المستقل", password="TeacherTest#2026")
        self.other = Teacher.objects.create_user(phone="0557990103", name="معلم آخر", password="OtherTest#2026")
        self.workspace = PersonalWorkspace.objects.create(owner=self.teacher, school_name="مدرسة تعريفية", current_academic_year="1447-1448")
        self.other_workspace = PersonalWorkspace.objects.create(owner=self.other, school_name="مدرسة أخرى")
        self.subscription = ensure_personal_subscription(self.workspace)
        ensure_personal_subscription(self.other_workspace)
        self.school = School.objects.create(name="مدرسة الصيانة", code="support-a")
        self.other_school = School.objects.create(name="مدرسة أخرى", code="support-b")
        self.client.force_login(self.admin)

    def tearDown(self):
        cache.clear()

    def token(self):
        return self.client.session[SESSION_KEY]["token"]

    def enter_personal(self, workspace=None):
        payload = {CONTEXT_FIELD: self.token()} if SESSION_KEY in self.client.session else {}
        return self.client.post(reverse("reports:platform_support_personal", args=[(workspace or self.workspace).pk]), payload)

    def enter_school(self, school=None):
        payload = {CONTEXT_FIELD: self.token()} if SESSION_KEY in self.client.session else {}
        return self.client.post(reverse("reports:platform_support_school", args=[(school or self.school).pk]), payload)

    @staticmethod
    def report_payload():
        return {"title": "تقرير الصيانة", "category": "نشاط", "report_date": "2026-10-02",
                "academic_year": "1447-1448", "description": "تهيئة تقرير للمعلم", "status": "draft"}

    def test_only_owner_can_enter_and_state_changes_require_post(self):
        for route, pk in [("reports:platform_support_personal", self.workspace.pk), ("reports:platform_support_school", self.school.pk)]:
            self.assertEqual(self.client.get(reverse(route, args=[pk])).status_code, 405)
            staff = Client()
            staff.force_login(self.teacher)
            self.assertEqual(staff.post(reverse(route, args=[pk])).status_code, 403)
            self.assertNotIn(SESSION_KEY, staff.session)

    def test_csrf_is_required_to_enter_and_exit(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse("reports:platform_support_personal", args=[self.workspace.pk])).status_code, 403)
        self.assertEqual(client.post(reverse("reports:platform_support_exit")).status_code, 403)
        self.client.force_login(self.admin)
        self.enter_personal()
        self.assertEqual(self.client.get(reverse("reports:platform_support_exit")).status_code, 405)

    def test_personal_entry_keeps_admin_authentication_and_teacher_session(self):
        self.teacher.current_session_key = "teacher-existing-device"
        self.teacher.save(update_fields=["current_session_key"])
        before_login = self.teacher.last_login
        before_password = self.teacher.password
        self.assertRedirects(self.enter_personal(), reverse("personal:dashboard"), fetch_redirect_response=False)
        page = self.client.get(reverse("personal:dashboard"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["workspace"].pk, self.workspace.pk)
        self.assertContains(page, "وضع الإدارة والصيانة")
        self.assertContains(page, self.teacher.name)
        self.assertEqual(self.client.session[AUTH_SESSION_KEY], str(self.admin.pk))
        self.teacher.refresh_from_db()
        self.assertEqual((self.teacher.current_session_key, self.teacher.last_login, self.teacher.password),
                         ("teacher-existing-device", before_login, before_password))
        self.assertContains(self.client.get(reverse("reports:platform_admin_dashboard")), "إدارة المنصة")

    def test_personal_report_writes_into_selected_workspace_and_audits_real_actor(self):
        self.enter_personal()
        data = self.report_payload() | {CONTEXT_FIELD: self.token(), "workspace_id": self.other_workspace.pk}
        response = self.client.post(reverse("personal:report_create"), data)
        self.assertEqual(response.status_code, 302)
        report = PersonalReport.objects.get(title="تقرير الصيانة")
        self.assertEqual(report.workspace_id, self.workspace.pk)
        self.assertEqual(report.teacher_name, self.teacher.name)
        event = AuditLog.objects.get(model_name="PersonalReport", object_id=report.pk, action=AuditLog.Action.CREATE)
        self.assertEqual(event.teacher_id, self.admin.pk)
        self.assertEqual(event.changes["support"]["target_id"], self.workspace.pk)
        self.assertEqual(self.other_workspace.reports.count(), 0)

    def test_other_teachers_report_is_not_accessible_in_selected_personal_workspace(self):
        report = PersonalReport.objects.create(workspace=self.other_workspace, title="خاص", category="نشاط",
                                              report_date=timezone.localdate(), academic_year="1447-1448", teacher_name=self.other.name,
                                              school_name="مدرسة أخرى")
        self.enter_personal()
        self.assertEqual(self.client.get(reverse("personal:report_detail", args=[report.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("personal:report_edit", args=[report.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("personal:report_delete", args=[report.pk]), {CONTEXT_FIELD: self.token()}).status_code, 404)

    def test_maintenance_can_configure_expired_account_without_activating_subscription(self):
        self.subscription.is_active = False
        self.subscription.save(update_fields=["is_active"])
        self.subscription.plan.max_reports = 1
        self.subscription.plan.save(update_fields=["max_reports"])
        self.subscription.reports_created = 1
        self.subscription.save(update_fields=["reports_created"])
        self.enter_personal()
        response = self.client.post(reverse("personal:report_create"), self.report_payload() | {CONTEXT_FIELD: self.token()})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.workspace.reports.exists())
        self.subscription.refresh_from_db()
        self.assertFalse(self.subscription.is_active)
        self.assertEqual(self.subscription.reports_created, 2)
        normal = Client()
        normal.force_login(self.teacher)
        result = normal.post(reverse("personal:report_create"), self.report_payload() | {"title": "محاولة عادية"})
        self.assertEqual(result.status_code, 302)
        self.assertFalse(self.workspace.reports.filter(title="محاولة عادية").exists())

    def test_inactive_school_is_accessible_only_in_authorized_maintenance(self):
        self.school.is_active = False
        self.school.save(update_fields=["is_active"])
        self.enter_school()
        for route in ("reports:admin_dashboard", "reports:school_settings", "reports:manage_teachers", "reports:departments_list", "reports:staff_roles", "reports:admin_reports"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.wsgi_request.active_school.pk, self.school.pk)
                self.assertContains(response, "وضع الإدارة والصيانة")
        self.school.refresh_from_db()
        self.assertFalse(self.school.is_active)

    def test_school_context_restores_after_exit_and_personal_entry_clears_it(self):
        session = self.client.session
        session["active_school_id"] = self.other_school.pk
        session.save()
        self.enter_school()
        self.assertEqual(self.client.session["active_school_id"], self.school.pk)
        self.enter_personal()
        self.assertNotIn("active_school_id", self.client.session)
        personal_event = AuditLog.objects.filter(model_name="PlatformSupportAction", changes__kind="personal").first()
        self.assertIsNone(personal_event.school_id)
        token = self.token()
        response = self.client.post(reverse("reports:platform_support_exit"), {CONTEXT_FIELD: token})
        self.assertRedirects(response, reverse("reports:platform_admin_dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["active_school_id"], self.other_school.pk)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(response["X-Platform-Support"], "ended")

    def test_stale_tab_and_missing_context_cannot_save_after_switching(self):
        self.enter_personal()
        old_token = self.token()
        self.enter_personal(self.other_workspace)
        response = self.client.post(reverse("personal:report_create"), self.report_payload() | {CONTEXT_FIELD: old_token})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.post(reverse("personal:report_create"), self.report_payload()).status_code, 409)
        self.assertEqual(PersonalReport.objects.count(), 0)
        self.assertEqual(self.client.post(reverse("reports:platform_support_exit"), {CONTEXT_FIELD: old_token}).status_code, 409)

    def test_stale_tab_cannot_write_after_exit(self):
        self.enter_personal()
        token = self.token()
        self.client.post(reverse("reports:platform_support_exit"), {CONTEXT_FIELD: token})
        self.assertEqual(self.client.post(reverse("personal:report_create"), self.report_payload() | {CONTEXT_FIELD: token}).status_code, 409)

    def test_expiration_and_owner_privilege_revocation_stop_maintenance(self):
        self.enter_personal()
        token = self.token()
        session = self.client.session
        scope = session[SESSION_KEY]
        scope["expires_at"] = timezone.now().timestamp() - 1
        session[SESSION_KEY] = scope
        session.save()
        self.assertEqual(self.client.post(reverse("personal:report_create"), self.report_payload() | {CONTEXT_FIELD: token}).status_code, 409)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.enter_personal()
        token = self.token()
        Teacher.objects.filter(pk=self.admin.pk).update(is_superuser=False)
        self.assertEqual(self.client.post(reverse("personal:report_create"), self.report_payload() | {CONTEXT_FIELD: token}).status_code, 409)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(PersonalReport.objects.count(), 0)

    def test_resetting_teacher_password_does_not_replace_admin_auth_hash(self):
        self.enter_personal()
        original_hash = self.client.session["_auth_user_hash"]
        original_key = self.teacher.current_session_key
        response = self.client.post(reverse("personal:account"), {
            "action": "password", "password-new_password1": "ChangedTeacher#2026",
            "password-new_password2": "ChangedTeacher#2026", CONTEXT_FIELD: self.token(),
        })
        self.assertEqual(response.status_code, 302)
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password("ChangedTeacher#2026"))
        self.assertEqual(self.teacher.current_session_key, original_key)
        self.assertEqual(self.client.session["_auth_user_hash"], original_hash)
        self.assertEqual(self.client.session[AUTH_SESSION_KEY], str(self.admin.pk))
        self.assertEqual(self.client.get(reverse("reports:platform_admin_dashboard")).status_code, 200)

    def test_shared_home_and_profile_resolve_to_current_personal_space(self):
        self.enter_personal()
        self.assertRedirects(self.client.get(reverse("reports:home")), reverse("personal:dashboard"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("reports:my_profile")), reverse("personal:account"), fetch_redirect_response=False)

    def test_personal_platform_views_keep_owner_authority_and_can_configure_subscription(self):
        self.enter_personal()
        page = self.client.get(reverse("personal:platform_subscription_settings", args=[self.workspace.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.wsgi_request.user.pk, self.admin.pk)
        response = self.client.post(reverse("personal:platform_subscription_settings", args=[self.workspace.pk]), {
            "plan": self.subscription.plan_id, "end_date": "", CONTEXT_FIELD: self.token(),
        })
        self.assertEqual(response.status_code, 302)
        self.subscription.refresh_from_db()
        self.assertFalse(self.subscription.is_active)
        self.assertIsNone(self.subscription.end_date)
        self.assertEqual(self.other_workspace.subscription.is_active, True)
        self.assertTrue(AuditLog.objects.filter(model_name="PersonalSubscription", teacher=self.admin, changes__workspace_id=self.workspace.pk).exists())

    def test_no_support_headers_or_scope_for_normal_teacher(self):
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("personal:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-Platform-Support", response)
        self.assertNotContains(response, "وضع الإدارة والصيانة")

    def test_private_maintenance_responses_are_never_cached(self):
        self.enter_personal()
        page = self.client.get(reverse("personal:reports"))
        self.assertIn("no-store", page["Cache-Control"])
        self.assertContains(page, "platform-support.js")
        self.assertContains(page, 'name="_support_context"')

    def test_ajax_context_header_allows_write_and_records_original_admin(self):
        self.enter_personal()
        response = self.client.post(reverse("personal:report_create"), self.report_payload(),
                                    HTTP_X_PLATFORM_SUPPORT_CONTEXT=self.token())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.workspace.reports.count(), 1)
        event = AuditLog.objects.get(model_name="PersonalReport", action=AuditLog.Action.CREATE)
        self.assertEqual(event.teacher_id, self.admin.pk)

    def test_forged_maintenance_context_does_not_grant_access_to_normal_user(self):
        self.enter_personal()
        scope = self.client.session[SESSION_KEY].copy()
        normal = Client()
        normal.force_login(self.other)
        session = normal.session
        scope["actor_id"] = self.other.pk
        session[SESSION_KEY] = scope
        session.save()
        response = normal.get(reverse("personal:dashboard"))
        self.assertEqual(response.context["workspace"].pk, self.other_workspace.pk)
        self.assertNotIn(SESSION_KEY, normal.session)
        self.assertNotContains(response, "وضع الإدارة والصيانة")

    def test_maintenance_session_has_readable_arabic_audit_history(self):
        self.enter_personal()
        log = AuditLog.objects.get(model_name="PlatformSupportSession", changes__event="enter")
        self.assertEqual(describe(log).headline, "بدء جلسة إدارة وصيانة")
        self.client.post(reverse("reports:platform_support_exit"), {CONTEXT_FIELD: self.token()})
        log = AuditLog.objects.get(model_name="PlatformSupportSession", changes__event="exit")
        self.assertEqual(describe(log).headline, "إنهاء جلسة الصيانة")

    def test_teacher_cannot_use_platform_subscription_configuration(self):
        self.client.force_login(self.teacher)
        url = reverse("personal:platform_subscription_settings", args=[self.workspace.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"plan": self.subscription.plan_id, "is_active": "on"}).status_code, 403)

    def test_context_error_is_readable_in_html_and_structured_for_ajax(self):
        self.enter_personal()
        response = self.client.post(reverse("personal:report_create"), {CONTEXT_FIELD: "سياق خاطئ"})
        self.assertContains(response, "تغيّرت مساحة الصيانة", status_code=409)
        self.assertContains(response, "لم يُنفَّذ الحفظ", status_code=409)
        response = self.client.post(reverse("personal:report_create"), {CONTEXT_FIELD: "wrong"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "support_context_changed")
        response = self.client.post(reverse("personal:report_create"), self.report_payload(), HTTP_X_PLATFORM_SUPPORT_CONTEXT="wrong")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "support_context_changed")
