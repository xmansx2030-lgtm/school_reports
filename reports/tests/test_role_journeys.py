# -*- coding: utf-8 -*-
"""رحلة كل دور من نقطة هبوطه إلى ما يجد إليه طريقاً.

الفحص الذي أنتج هذه الاختبارات كشف نمطاً واحداً متكرراً: **الصلاحية نافذة،
والشاشة قائمة، والطريق إليها معدوم.** طبقة الخدمات كانت تعرف الوكيل والموظف
الإداري، والقائمة تعرف «مديراً» أو «معلّماً» ولا شيء بينهما. فما يُمنح لهما يعمل
عند كتابة المسار يدوياً ولا يُرى.

وثلاث صلاحيات كانت أسوأ من ذلك: مُعرَّفة في مرجع الصلاحيات، تُعرض في شاشة
الأدوار، ولا يفحصها سطر واحد في المشروع — يمنحها المدير ويظنّها نافذة.

ولذلك تُقسَّم الاختبارات هنا إلى ثلاث عائلات:

- **الهبوط**: أين ينزل كل دور بعد الدخول.
- **الإنفاذ**: أن الصلاحية الممنوحة تفتح باباً فعلاً، وأنها لا تفتح أكثر منه.
- **الطريق**: أن ما مُنح يُرى في القائمة، وأن ما لم يُمنح لا يُرى — والاتجاهان
  معاً، فزرٌّ مرئي ممنوع أسوأ ما يقابله مستخدم.
"""
from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from reports import capabilities as caps
from reports.models import (
    Department,
    DepartmentMembership,
    Report,
    ReportType,
    School,
    SchoolGroup,
    SchoolGroupMembership,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
    TeacherAchievementFile,
    Ticket,
)
from reports.model_parts.approvals import ApprovalState
from reports.permissions import effective_user_role_label

PASSWORD = "Passw0rd!123"


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password=PASSWORD)


def _school(name: str, code: str, **kwargs) -> School:
    plan = SubscriptionPlan.objects.create(
        name=f"باقة {code}", price=0, days_duration=365, max_teachers=0
    )
    school = School.objects.create(name=name, code=code, **kwargs)
    SchoolSubscription.objects.create(school=school, plan=plan)
    return school


class RoleJourneyTestCase(TestCase):
    """أدوات مشتركة: مدرسة بمديرها وقسمها ووكيلها ومعلّمها."""

    def setUp(self):
        # الذاكرة تُفرَّغ بين الاختبارات: كونتكست التنقل مخزَّن لثوانٍ، وحالةُ
        # اختبارٍ سابق تجعل التالي يقرأ أعلاماً ليست له.
        cache.clear()
        self.school = _school("ثانوية الرحلات", "journey-school")

        self.manager = _user("مدير الرحلات", "0500021001")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

        self.department = Department.objects.create(
            school=self.school, name="قسم العلوم", slug="science"
        )
        self.other_department = Department.objects.create(
            school=self.school, name="قسم اللغات", slug="languages"
        )

        self.deputy = _user("وكيل الرحلات", "0500021002")
        self.deputy_membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=self.deputy,
            role_type=SchoolMembership.RoleType.DEPUTY,
        )

        self.teacher = _user("معلم الرحلات", "0500021003")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        DepartmentMembership.objects.create(
            department=self.department,
            teacher=self.teacher,
            role_type=DepartmentMembership.TEACHER,
        )

        self.outsider = _user("معلم قسم آخر", "0500021004")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.outsider,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        DepartmentMembership.objects.create(
            department=self.other_department,
            teacher=self.outsider,
            role_type=DepartmentMembership.TEACHER,
        )

    # ------------------------------------------------------------------
    def _grant(self, *capabilities, member=None, departments=None):
        """يمنح صلاحيات بنطاقٍ على عضوية — كما تفعل شاشة الأدوار."""
        membership = member or self.deputy_membership
        scope, _ = StaffScope.objects.get_or_create(membership=membership)
        scope.capabilities = list(capabilities)
        scope.save()
        scope.departments.set(
            departments if departments is not None else [self.department]
        )
        cache.clear()
        return scope

    def _enter(self, user, school=None):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = (school or self.school).pk
        session.save()

    def _page(self, user, url_name: str, *args) -> str:
        self._enter(user)
        return self.client.get(reverse(url_name, args=args)).content.decode()

    def _nav(self, user, url_name: str = "reports:home") -> str:
        """الترويسة والدرج معاً — القائمتان اللتان يصل منهما المستخدم."""
        return self._page(user, url_name)


# ═══════════════════════════════════════════════════════════════════════
# المدير التنفيذي
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class ExecutiveDirectorJourneyTests(TestCase):
    """أسوأ رحلة كانت في المشروع: يهبط على لوحة معلّم فارغة بتحذير عطل.

    والسبب واحد: عضوية المدير التنفيذي على **المجموعة** لا على مدرسة — وهو ما
    يجعله لا يستهلك مقعداً مدفوعاً — فكان كل فحصٍ يسأل عن عضوية مدرسة يعدّه
    حساباً ناقص الربط.
    """

    def setUp(self):
        cache.clear()
        self.group = SchoolGroup.objects.create(name="مجموعة الرحلات", code="journey-group")
        self.school = _school("مدرسة المجموعة", "group-school-1", group=self.group)
        self.director = _user("المدير التنفيذي", "0500022001")
        SchoolGroupMembership.objects.create(
            group=self.group,
            user=self.director,
            role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
        )

    def _login(self):
        return self.client.post(
            reverse("reports:login"),
            {"identifier": self.director.phone, "password": PASSWORD},
            follow=False,
        )

    def test_the_director_lands_on_their_group_dashboard(self):
        response = self._login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("reports:executive_dashboard"))

    def test_the_director_is_not_warned_about_a_missing_school(self):
        """التحذير كان يستقبله عند كل دخول عن حالةٍ صحيحة بحكم التصميم."""
        response = self.client.post(
            reverse("reports:login"),
            {"identifier": self.director.phone, "password": PASSWORD},
            follow=True,
        )
        texts = [str(message) for message in response.context["messages"]]
        self.assertFalse(
            [text for text in texts if "غير مرتبط بمدرسة" in text],
            f"المدير التنفيذي حُذِّر بلا سبب: {texts}",
        )

    def test_home_sends_the_director_to_their_group(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:home"))
        self.assertRedirects(response, reverse("reports:executive_dashboard"))

    def test_the_header_calls_the_director_by_their_title(self):
        """كان يُنادى «مستخدم» لأن التسمية تُشتق من عضوية مدرسة لا يملكها."""
        self.assertEqual(effective_user_role_label(self.director), "مدير تنفيذي")

    def test_the_group_name_stands_in_for_a_school_name(self):
        self.client.force_login(self.director)
        page = self.client.get(reverse("reports:executive_dashboard")).content.decode()
        self.assertIn(self.group.name, page)

    def test_a_director_who_also_teaches_keeps_their_personal_home(self):
        """من جمع الصفتين لا يفقد لوحته: «الرئيسية» طريقها الوحيد."""
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.director,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.director)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        response = self.client.get(reverse("reports:home"))
        self.assertEqual(response.status_code, 200)

    def test_a_director_who_also_teaches_lands_on_their_personal_home(self):
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.director,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        response = self._login()
        self.assertRedirects(
            response,
            reverse("reports:home"),
            fetch_redirect_response=False,
        )
        self.assertEqual(self.client.session["active_school_id"], self.school.pk)

    def test_the_director_still_reaches_their_group_screens(self):
        self.client.force_login(self.director)
        for name in (
            "reports:executive_dashboard",
            "reports:group_assignment_board",
            "reports:council_list",
            "reports:group_report",
        ):
            with self.subTest(destination=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


# ═══════════════════════════════════════════════════════════════════════
# الصلاحيات التي كانت معرَّفة ولا تُفحَص
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class DeadCapabilitiesAreNowEnforcedTests(RoleJourneyTestCase):
    """ثلاث صلاحيات كانت تُمنَح ولا تفعل شيئاً.

    وهو أخطر عطلٍ في نظام صلاحيات: لا رسالة خطأ ولا نتيجة خاطئة — فقط مديرٌ
    يظن أنه أسند عملاً، ووكيلٌ لا يعلم أن شيئاً أُسند إليه.
    """

    # ── الاطلاع على مؤشرات المدرسة ────────────────────────────────────
    def test_the_scope_screen_opens_for_a_granted_deputy(self):
        self._grant(caps.VIEW_SCHOOL_DASHBOARD)
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:staff_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.department.name, response.content.decode())

    def test_the_scope_screen_is_closed_without_the_capability(self):
        self._enter(self.teacher)
        response = self.client.get(reverse("reports:staff_dashboard"))
        self.assertRedirects(response, reverse("reports:home"))

    def test_a_bare_deputy_role_does_not_open_the_scope_screen(self):
        """حملُ الدور لا يمنح شيئاً — المنح بالنطاق لا بالمسمّى."""
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:staff_dashboard"))
        self.assertRedirects(response, reverse("reports:home"))

    def test_the_manager_is_sent_to_their_own_full_dashboard(self):
        """لا نسخة مصغَّرة لمن يملك اللوحة كاملة."""
        self._enter(self.manager)
        response = self.client.get(reverse("reports:staff_dashboard"))
        self.assertRedirects(
            response, reverse("reports:admin_dashboard"), target_status_code=200
        )

    def test_an_unset_scope_is_announced_not_shown_as_zero(self):
        """أصفارٌ بلا تفسير تُقرأ «لا عمل عليك» لا «نطاقك لم يُضبط»."""
        self._grant(caps.VIEW_SCHOOL_DASHBOARD, departments=[])
        page = self._page(self.deputy, "reports:staff_dashboard")
        self.assertIn("لم تُسنَد إليك أقسام", page)

    def test_the_scope_screen_carries_no_billing_data(self):
        """نصّ الصلاحية «دون بيانات خارج إشرافه» — والفوترة أوّلها.

        الفحص على **سياق** الشاشة لا على نصّها: كلمةٌ قد تَرِد في ترويسة مشتركة
        لسبب آخر، والسؤال الحقيقي هل تقرأ هذه الشاشة بيانات اشتراك أصلاً.
        """
        self._grant(caps.VIEW_SCHOOL_DASHBOARD)
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:staff_dashboard"))
        for forbidden in ("subscription", "consumption", "seats", "storage"):
            with self.subTest(key=forbidden):
                self.assertNotIn(forbidden, response.context.keys())

    # ── متابعة ملفات الإنجاز ──────────────────────────────────────────
    def test_view_achievements_opens_the_school_files_list(self):
        self._grant(caps.VIEW_ACHIEVEMENTS)
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:achievement_school_files"))
        self.assertEqual(response.status_code, 200)

    def test_the_watcher_sees_their_scope_and_not_beyond_it(self):
        self._grant(caps.VIEW_ACHIEVEMENTS)
        page = self._page(self.deputy, "reports:achievement_school_files")
        self.assertIn(self.teacher.name, page)
        self.assertNotIn(self.outsider.name, page)

    def test_the_watcher_reads_a_file_but_cannot_decide_on_it(self):
        """«يطّلع … دون اعتمادها» — فالاطلاع يُفتح والاعتماد يبقى مغلقاً."""
        self._grant(caps.VIEW_ACHIEVEMENTS)
        ach = TeacherAchievementFile.objects.create(
            school=self.school, teacher=self.teacher, academic_year="1447-1448"
        )
        self._enter(self.deputy)
        response = self.client.get(
            reverse("reports:achievement_file_detail", args=[ach.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_manager"])

    def test_a_file_outside_the_scope_is_refused(self):
        self._grant(caps.VIEW_ACHIEVEMENTS)
        ach = TeacherAchievementFile.objects.create(
            school=self.school, teacher=self.outsider, academic_year="1447-1448"
        )
        self._enter(self.deputy)
        response = self.client.get(
            reverse("reports:achievement_file_detail", args=[ach.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_printing_a_file_outside_the_scope_is_refused_too(self):
        """الطباعة كانت تكتفي بسؤال «يطّلع على ملفات غيره؟» بلا سؤال «ملفات مَن»."""
        self._grant(caps.VIEW_ACHIEVEMENTS)
        ach = TeacherAchievementFile.objects.create(
            school=self.school, teacher=self.outsider, academic_year="1447-1448"
        )
        self._enter(self.deputy)
        response = self.client.get(
            reverse("reports:achievement_file_print", args=[ach.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_the_owner_still_reads_their_own_file(self):
        ach = TeacherAchievementFile.objects.create(
            school=self.school, teacher=self.teacher, academic_year="1447-1448"
        )
        self._enter(self.teacher)
        response = self.client.get(
            reverse("reports:achievement_file_detail", args=[ach.pk])
        )
        self.assertEqual(response.status_code, 200)

    # ── متابعة الطلبات ────────────────────────────────────────────────
    def _ticket(self, department, title="طلب صيانة"):
        return Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            department=department,
            title=title,
            body="نص الطلب",
            status="open",
        )

    def test_handle_requests_opens_the_school_requests_screen(self):
        self._grant(caps.HANDLE_REQUESTS)
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:manager_school_tickets"))
        self.assertEqual(response.status_code, 200)

    def test_requests_outside_the_scope_stay_hidden(self):
        self._grant(caps.HANDLE_REQUESTS)
        self._ticket(self.department, "طلب قسمي")
        self._ticket(self.other_department, "طلب قسم آخر")
        page = self._page(self.deputy, "reports:manager_school_tickets")
        self.assertIn("طلب قسمي", page)
        self.assertNotIn("طلب قسم آخر", page)

    def test_the_screen_tells_the_deputy_the_list_is_partial(self):
        """كشفٌ مُنطَق يُقرأ كاملاً يجعل صاحبه يظن مدرسته بلا طلبات."""
        self._grant(caps.HANDLE_REQUESTS)
        page = self._page(self.deputy, "reports:manager_school_tickets")
        self.assertIn("نطاق", page)

    def test_the_manager_still_sees_every_school_request(self):
        self._ticket(self.department, "طلب قسمي")
        self._ticket(self.other_department, "طلب قسم آخر")
        page = self._page(self.manager, "reports:manager_school_tickets")
        self.assertIn("طلب قسمي", page)
        self.assertIn("طلب قسم آخر", page)

    def test_requests_stay_closed_without_the_capability(self):
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:manager_school_tickets"))
        self.assertRedirects(response, reverse("reports:home"))

    def test_an_empty_scope_grants_no_requests_at_all(self):
        """أقسامٌ فارغة تعني لا شيء لا كل شيء."""
        self._grant(caps.HANDLE_REQUESTS, departments=[])
        self._ticket(self.department, "طلب قسمي")
        page = self._page(self.deputy, "reports:manager_school_tickets")
        self.assertNotIn("طلب قسمي", page)

    def test_the_deputy_may_act_on_a_request_inside_their_scope(self):
        self._grant(caps.HANDLE_REQUESTS)
        ticket = self._ticket(self.department)
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:ticket_detail", args=[ticket.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_act"])

    def test_the_deputy_may_not_act_outside_their_scope(self):
        self._grant(caps.HANDLE_REQUESTS)
        ticket = self._ticket(self.other_department)
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:ticket_detail", args=[ticket.pk]))
        if response.status_code == 200:
            self.assertFalse(response.context["can_act"])
        else:
            self.assertIn(response.status_code, (403, 404))

    # ── حراسة: لا صلاحية بلا موضع فحص ────────────────────────────────
    def test_every_available_capability_is_checked_somewhere(self):
        """صلاحيةٌ لا يفحصها سطر واحد وعدٌ للمدير لا يُوفى.

        العلّة التي فتحت هذا الملف: ثلاث صلاحيات ``available=True`` بلا أي
        موضع فحص. والاختبار يقرأ المصدر لأن الفحص قد يقع في عرضٍ أو قالبٍ أو
        كونتكست — ولا يجمعها إلا البحث النصّي.
        """
        from pathlib import Path

        from django.conf import settings

        root = Path(settings.BASE_DIR) / "reports"
        sources: list[str] = []
        for path in list(root.rglob("*.py")) + list(root.rglob("*.html")):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            if path.name == "capabilities.py":
                continue
            try:
                sources.append(path.read_text(encoding="utf-8"))
            except Exception:
                continue
        haystack = "\n".join(sources)

        constant_by_code = {
            value: name
            for name, value in vars(caps).items()
            if isinstance(value, str) and value in caps.VALID_CODES and name.isupper()
        }
        for code in sorted(caps.AVAILABLE_CODES):
            with self.subTest(capability=code):
                constant = constant_by_code.get(code, "")
                self.assertTrue(
                    f'"{code}"' in haystack
                    or f"'{code}'" in haystack
                    or (constant and f"caps.{constant}" in haystack)
                    or (constant and f"{constant}" in haystack),
                    f"الصلاحية {code} معلَنة ولا يفحصها موضع واحد",
                )


# ═══════════════════════════════════════════════════════════════════════
# الطريق: ما مُنح يُرى، وما لم يُمنح لا يُرى
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class NavigationFollowsCapabilitiesTests(RoleJourneyTestCase):
    """القائمة كانت تعرف شرطاً واحداً: «مدير مدرسة»."""

    def test_the_deputy_finds_every_granted_destination(self):
        self._grant(
            caps.REVIEW_REPORTS,
            caps.ASSIGN_TASKS,
            caps.MANAGE_MEETINGS,
            caps.TRACK_PLANS,
            caps.VIEW_ACHIEVEMENTS,
            caps.HANDLE_REQUESTS,
            caps.DRAFT_CIRCULARS,
            caps.ARCHIVE_DOCUMENTS,
            caps.VIEW_SCHOOL_DASHBOARD,
        )
        page = self._nav(self.deputy)
        for name in (
            "reports:approval_inbox",
            "reports:assignment_board",
            "reports:meeting_list",
            "reports:plan_list",
            "reports:achievement_school_files",
            "reports:manager_school_tickets",
            "reports:circular_draft_list",
            "reports:document_archive",
            "reports:staff_dashboard",
        ):
            with self.subTest(destination=name):
                self.assertIn(f'href="{reverse(name)}"', page)

    def test_the_bar_hides_what_was_not_granted(self):
        """مُنح المحاضر وحدها، فلا يُعرض له صندوق الاعتماد ولا لوحة التكليفات."""
        self._grant(caps.MANAGE_MEETINGS)
        page = self._nav(self.deputy)
        self.assertIn(f'href="{reverse("reports:meeting_list")}"', page)
        self.assertNotIn(f'href="{reverse("reports:approval_inbox")}"', page)
        self.assertNotIn(f'href="{reverse("reports:assignment_board")}"', page)
        self.assertNotIn(f'href="{reverse("reports:staff_dashboard")}"', page)

    def test_a_plain_teacher_is_not_offered_circular_drafts(self):
        """كان يُعرض للجميع، ويردّ العرضُ المعلّمَ برسالة منع."""
        page = self._nav(self.teacher)
        self.assertNotIn(f'href="{reverse("reports:circular_draft_list")}"', page)

    def test_the_refusal_the_hidden_button_used_to_cause_is_real(self):
        """إثبات أن الإخفاء ليس تجميلاً: الزرّ كان يقود إلى منع."""
        self._enter(self.teacher)
        response = self.client.get(reverse("reports:circular_draft_list"))
        self.assertRedirects(response, reverse("reports:home"))

    def test_a_granted_deputy_is_offered_circular_drafts(self):
        self._grant(caps.DRAFT_CIRCULARS)
        page = self._nav(self.deputy)
        self.assertIn(f'href="{reverse("reports:circular_draft_list")}"', page)

    def test_assigned_work_is_linked_for_whoever_has_any(self):
        """العدّاد كان يُحسب لكل مستخدم والرابط مشروطاً بدور مسؤول القسم."""
        staff = _user("موظف إداري", "0500021005")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=staff,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            assignee=staff,
            title="طلب محال",
            body="نص",
            status="open",
        )
        cache.clear()
        page = self._nav(staff)
        self.assertIn(f'href="{reverse("reports:assigned_to_me")}"', page)

    def test_granting_a_capability_shows_its_link_at_once(self):
        """كونتكست التنقل مخزَّن، فبلا إبطالٍ عند المنح يُقال «المنح لم يعمل»."""
        before = self._nav(self.deputy)
        self.assertNotIn(f'href="{reverse("reports:approval_inbox")}"', before)

        self._grant(caps.REVIEW_REPORTS)
        after = self._nav(self.deputy)
        self.assertIn(f'href="{reverse("reports:approval_inbox")}"', after)

    def test_the_manager_bar_is_untouched(self):
        """مجموعات المدير الخمس تغطّي هذه الوجهات، فلا تُزاد له سادسة."""
        import re

        page = self._nav(self.manager, "reports:admin_dashboard")
        # الشريط وحده: كلمة «الإشراف» قد تَرِد في نصّ المحتوى، وفحصُ الصفحة
        # كاملةً يجعل الاختبار يفشل لسببٍ لا علاقة له بالتنقّل.
        nav = re.search(r'<nav class="hdr-nav".*?</nav>', page, re.S)
        self.assertIsNotNone(nav)
        self.assertNotIn(
            f'href="{reverse("reports:staff_dashboard")}"', nav.group(0)
        )

    def test_a_plain_teacher_keeps_a_flat_bar(self):
        page = self._nav(self.teacher)
        self.assertNotIn("SHOW_SUPERVISION_GROUP", page)
        self.assertNotIn("مؤشرات نطاقي", page)


# ═══════════════════════════════════════════════════════════════════════
# لوحة الهبوط: ما ينتظر قرارك
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class SupervisionLandingPanelTests(RoleJourneyTestCase):
    """اللوحة كانت تعرض عمل صاحبها وحده — وهو نصف صورة الوكيل."""

    def _pending_report(self):
        report_type = ReportType.objects.create(
            school=self.school, code="science-report", name="تقرير علمي"
        )
        report_type.departments.add(self.department)
        return Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            category=report_type,
            title="تقرير ينتظر المراجعة",
            report_date="2026-08-01",
            approval_state=ApprovalState.SUBMITTED,
        )

    def test_the_deputy_sees_what_awaits_their_decision(self):
        self._grant(caps.REVIEW_REPORTS)
        self._pending_report()
        page = self._page(self.deputy, "reports:home")
        self.assertIn("ما ينتظر قرارك", page)
        self.assertIn("تقارير تنتظر مراجعتك", page)

    def test_the_panel_counts_only_the_deputys_scope(self):
        self._grant(caps.REVIEW_REPORTS)
        self._pending_report()
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:home"))
        rows = {row["key"]: row["count"] for row in response.context["supervision_rows"]}
        self.assertEqual(rows["reports"], 1)

    def test_the_panel_stays_off_a_plain_teachers_home(self):
        self._pending_report()
        page = self._page(self.teacher, "reports:home")
        self.assertNotIn("ما ينتظر قرارك", page)

    def test_the_panel_only_lists_what_was_granted(self):
        self._grant(caps.REVIEW_REPORTS)
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:home"))
        keys = {row["key"] for row in response.context["supervision_rows"]}
        self.assertEqual(keys, {"reports"})

    def test_an_unset_scope_is_announced_on_the_panel(self):
        self._grant(caps.REVIEW_REPORTS, departments=[])
        page = self._page(self.deputy, "reports:home")
        self.assertIn("لم تُسنَد إليك أقسام", page)

    def test_the_panel_survives_an_unset_session(self):
        """صفحةُ هبوطٍ تسقط لأجل بطاقة جانبية أسوأ من غياب البطاقة.

        ولا تُشترط هنا بطاقةٌ فارغة: ``_get_active_school`` تستنتج المدرسة من
        عضوية وحيدة حين تخلو الجلسة منها — فالمطلوب أن تصمد الصفحة لا أن تصمت.
        """
        self._grant(caps.REVIEW_REPORTS)
        self.client.force_login(self.deputy)
        response = self.client.get(reverse("reports:home"))
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.context["supervision_rows"], list)

    def test_a_bare_deputy_gets_an_explicit_setup_state(self):
        page = self._page(self.deputy, "reports:home")
        self.assertIn("مركز قيادة الوكيل", page)
        self.assertIn("لم تُفعّل مهامك الإشرافية بعد", page)
        self.assertIn("0 صلاحية", page)
        self.assertIn("أدواتي الشخصية", page)

    def test_a_plain_teacher_never_gets_the_deputy_workspace(self):
        page = self._page(self.teacher, "reports:home")
        self.assertNotIn("مركز قيادة الوكيل", page)
        self.assertNotIn("إدارة نطاقي", page)

    def test_deputy_workspace_only_links_granted_capabilities(self):
        self._grant(caps.VIEW_SCHOOL_DASHBOARD, caps.ASSIGN_TASKS)
        page = self._page(self.deputy, "reports:home")
        self.assertIn(reverse("reports:staff_dashboard"), page)
        self.assertIn(reverse("reports:assignment_board"), page)
        self.assertNotIn(reverse("reports:admin_reports"), page)
        self.assertIn("مؤشرات نطاقي", page)
        self.assertIn("إدارة التكليفات", page)

    def test_deputy_profile_explains_role_domain_scope_and_capabilities(self):
        scope = self._grant(caps.REVIEW_REPORTS)
        scope.domain = StaffScope.Domain.ACADEMIC
        scope.template_code = "deputy_academic"
        scope.save()
        page = self._page(self.deputy, "reports:my_profile")
        self.assertIn("اختصاصي وصلاحياتي", page)
        self.assertIn("الشؤون التعليمية", page)
        self.assertIn(self.department.name, page)
        self.assertIn(caps.BY_CODE[caps.REVIEW_REPORTS].label, page)
        self.assertIn(self.deputy_membership.get_role_type_display(), page)


# ═══════════════════════════════════════════════════════════════════════
# مساحة الموظف الإداري: تنفيذٌ أولاً، وصلاحيات إضافية عند المنح
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class AdministrativeStaffWorkspaceTests(RoleJourneyTestCase):
    def setUp(self):
        super().setUp()
        self.admin_staff = _user("موظف الرحلات الإداري", "0500021010")
        self.admin_membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=self.admin_staff,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
            job_title=SchoolMembership.JobTitle.ADMIN_STAFF,
        )

    def test_basic_employee_gets_execution_center_without_deputy_tools(self):
        page = self._page(self.admin_staff, "reports:home")

        self.assertIn("مركز عمل الموظف الإداري", page)
        self.assertIn("القالب الأساسي فعّال", page)
        self.assertIn(reverse("reports:assigned_to_me"), page)
        self.assertIn(reverse("reports:my_assignments"), page)
        self.assertIn(reverse("reports:document_archive"), page)
        self.assertNotIn("مركز قيادة الوكيل", page)
        self.assertNotIn(reverse("reports:assignment_board"), page)
        self.assertNotIn(reverse("reports:achievement_my_files"), page)

    def test_employee_navigation_keeps_assigned_requests_even_when_empty(self):
        page = self._nav(self.admin_staff)
        self.assertIn("الطلبات المسندة", page)
        self.assertIn(reverse("reports:assigned_to_me"), page)

    def test_document_template_exposes_only_its_granted_tools(self):
        scope = self._grant(
            caps.DRAFT_CIRCULARS,
            caps.ARCHIVE_DOCUMENTS,
            caps.MANAGE_MEETINGS,
            member=self.admin_membership,
        )
        scope.template_code = "admin_staff_documents"
        scope.save()

        page = self._page(self.admin_staff, "reports:home")
        self.assertIn("موظف إداري — الوثائق والتعاميم", page)
        self.assertIn(reverse("reports:circular_draft_list"), page)
        self.assertIn("تنظيم الاجتماعات وكتابة المحاضر", page)
        self.assertNotIn(reverse("reports:assignment_board"), page)
        self.assertNotIn(reverse("reports:plan_create"), page)

    def test_employee_scope_gap_is_explained_when_a_capability_needs_it(self):
        self._grant(
            caps.VIEW_SCHOOL_DASHBOARD,
            member=self.admin_membership,
            departments=[],
        )
        page = self._page(self.admin_staff, "reports:home")
        self.assertIn("الصلاحيات فعّالة لكن نطاق الأقسام غير مكتمل", page)

    def test_employee_profile_explains_the_basic_template(self):
        page = self._page(self.admin_staff, "reports:my_profile")
        self.assertIn("دوري الإداري وصلاحياتي", page)
        self.assertIn("موظف إداري (الأساسي)", page)
        self.assertIn("مهام التنفيذ الشخصية لا تحتاج نطاقاً إشرافياً", page)
        self.assertIn(self.admin_membership.get_role_type_display(), page)

    def test_dual_role_account_is_explicit_and_keeps_teacher_portfolio(self):
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
            job_title=SchoolMembership.JobTitle.ADMIN_STAFF,
        )
        page = self._page(self.teacher, "reports:home")

        self.assertIn("لهذا الحساب أكثر من دور في المدرسة", page)
        self.assertIn("موظف إداري، معلم", page)
        self.assertIn("ملف الإنجاز كمعلم", page)
        self.assertIn(reverse("reports:achievement_my_files"), page)

    def test_admin_report_copy_is_written_for_execution_work(self):
        page = self._page(self.admin_staff, "reports:add_report")
        self.assertIn("توثيق عمل إداري", page)
        self.assertIn("سجّل ما نُفّذ ونتائجه وشواهده", page)

    def test_meeting_page_explains_read_only_mode(self):
        page = self._page(self.admin_staff, "reports:meeting_list")
        self.assertIn("الاجتماعات التي دُعيت إليها في المدرسة النشطة", page)
        self.assertNotIn(reverse("reports:meeting_create"), page)

    def test_mansour_launcher_has_an_accessible_name(self):
        page = self._page(self.admin_staff, "reports:home")
        self.assertIn('aria-label="فتح المساعد الذكي منصور"', page)


# ═══════════════════════════════════════════════════════════════════════
# محضر المختبر والموظف الإداري: بابٌ واحد للإسناد
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class JobTitleAndRoleStayConsistentTests(RoleJourneyTestCase):
    """الترويسة كانت تناديه «محضر مختبر» والنظام يعامله معلّماً.

    بابان لإسناد الدور — «فريق المدرسة» و«الأدوار والصلاحيات» — كانا يكتبان
    شيئين مختلفين، فيُسند المديرُ محضّراً ويظنّ الأمر تمّ.
    """

    def _add_member(self, *, phone, name, job_title):
        self._enter(self.manager)
        payload = {"name": name, "phone": phone, "job_title": job_title}
        if job_title == SchoolMembership.JobTitle.LAB_TECH:
            payload["lab_kind"] = "science"
        return self.client.post(
            reverse("reports:add_teacher"),
            payload,
            follow=True,
        )

    def test_adding_a_lab_technician_writes_the_matching_role(self):
        self._add_member(
            phone="0500023001",
            name="محضر المختبر",
            job_title=SchoolMembership.JobTitle.LAB_TECH,
        )
        membership = SchoolMembership.objects.get(
            school=self.school, teacher__phone="0500023001"
        )
        self.assertEqual(membership.job_title, SchoolMembership.JobTitle.LAB_TECH)
        self.assertEqual(membership.role_type, SchoolMembership.RoleType.ADMIN_STAFF)

    def test_adding_an_admin_employee_writes_the_matching_role(self):
        self._add_member(
            phone="0500023002",
            name="موظف الشؤون",
            job_title=SchoolMembership.JobTitle.ADMIN_STAFF,
        )
        membership = SchoolMembership.objects.get(
            school=self.school, teacher__phone="0500023002"
        )
        self.assertEqual(membership.role_type, SchoolMembership.RoleType.ADMIN_STAFF)

    def test_adding_a_teacher_stays_a_teacher(self):
        self._add_member(
            phone="0500023003",
            name="معلم جديد",
            job_title=SchoolMembership.JobTitle.TEACHER,
        )
        membership = SchoolMembership.objects.get(
            school=self.school, teacher__phone="0500023003"
        )
        self.assertEqual(membership.role_type, SchoolMembership.RoleType.TEACHER)

    def test_a_lab_technician_can_then_be_granted_a_scope(self):
        """الأثر العملي: دورٌ خاطئ كان يمنع منحَه أي صلاحية على الإطلاق."""
        self._add_member(
            phone="0500023004",
            name="محضر ثانٍ",
            job_title=SchoolMembership.JobTitle.LAB_TECH,
        )
        membership = SchoolMembership.objects.get(
            school=self.school, teacher__phone="0500023004"
        )
        scope = self._grant(
            caps.ARCHIVE_DOCUMENTS, member=membership, departments=[self.department]
        )
        self.assertEqual(scope.capability_codes(), {caps.ARCHIVE_DOCUMENTS})

    def test_the_header_calls_a_lab_technician_by_their_title(self):
        self._add_member(
            phone="0500023005",
            name="محضر ثالث",
            job_title=SchoolMembership.JobTitle.LAB_TECH,
        )
        tech = Teacher.objects.get(phone="0500023005")
        self.assertEqual(
            effective_user_role_label(tech, active_school=self.school), "محضر مختبر"
        )
