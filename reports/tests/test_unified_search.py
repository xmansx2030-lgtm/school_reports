# -*- coding: utf-8 -*-
"""البحث الموحّد لا يُظهر ما لا يستطيع المستخدم فتحه.

محرّك البحث أخطر سطحٍ في أي منصة متعدّدة المستأجرين: هو — بحكم تعريفه — يمرّ
على كل الجداول دفعةً واحدة. وقاعدةُ رؤيةٍ تُنسى في مزوّدٍ واحد تفتح باباً
خلفياً لا تكشفه الشاشاتُ لأنها تبدو صحيحة.

ونتيجةُ بحثٍ تحمل عنوان تقرير هي **تسريب** حتى لو رُدَّ المستخدم عند النقر:
العنوان وحده يقول إن الشيء موجود، ومن صاحبه، وفي أي مدرسة.

فالفحوص هنا تُقسَّم إلى:

* **عزل المستأجر** — لا شيء من مدرسة أخرى، أبداً.
* **نطاق الصلاحية** — المعلّم لا يرى تقارير زميله ولا كشف المنسوبين.
* **الحدود** — بلا مدرسة نشطة لا نتائج؛ والاستعلام القصير لا يُنفَّذ.
* **التدهور** — تعثّرُ مزوّدٍ لا يُسقط البحث، ويُسجَّل.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from unittest.mock import patch

from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    Assignment,
    Document,
    Notification,
    NotificationRecipient,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    Ticket,
)
from reports.search import MIN_QUERY_LENGTH, search


@override_settings(ALLOWED_HOSTS=["testserver"])
class UnifiedSearchIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        plan = SubscriptionPlan.objects.create(
            name="Plan", price=0, days_duration=30, max_teachers=50
        )
        cls.mine = School.objects.create(name="مدرستي", code="search-mine")
        cls.theirs = School.objects.create(name="مدرسة أخرى", code="search-theirs")
        SchoolSubscription.objects.create(school=cls.mine, plan=plan)
        SchoolSubscription.objects.create(school=cls.theirs, plan=plan)

        cls.category = ReportType.objects.create(
            name="نشاط", code="activity", school=cls.mine
        )
        cls.other_category = ReportType.objects.create(
            name="نشاط", code="activity", school=cls.theirs
        )

        cls.manager = Teacher.objects.create_user(
            phone="500300001", name="مدير المدرسة", password="pass"
        )
        SchoolMembership.objects.create(
            school=cls.mine, teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

        cls.teacher = Teacher.objects.create_user(
            phone="500300002", name="معلم أول", password="pass"
        )
        SchoolMembership.objects.create(
            school=cls.mine, teacher=cls.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        cls.colleague = Teacher.objects.create_user(
            phone="500300003", name="معلم ثانٍ", password="pass"
        )
        SchoolMembership.objects.create(
            school=cls.mine, teacher=cls.colleague,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        cls.outsider = Teacher.objects.create_user(
            phone="500300004", name="معلم غريب", password="pass"
        )
        SchoolMembership.objects.create(
            school=cls.theirs, teacher=cls.outsider,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        # كلمة البحث واحدة في كل الكيانات، فأي تسرّب يظهر فوراً.
        cls.own_report = Report.objects.create(
            school=cls.mine, teacher=cls.teacher, category=cls.category,
            title="زيارة ميدانية للمتحف", idea="فكرة", report_date="2026-03-01",
        )
        cls.colleague_report = Report.objects.create(
            school=cls.mine, teacher=cls.colleague, category=cls.category,
            title="زيارة ميدانية للمصنع", idea="فكرة", report_date="2026-03-02",
        )
        cls.other_school_report = Report.objects.create(
            school=cls.theirs, teacher=cls.outsider, category=cls.other_category,
            title="زيارة ميدانية للحديقة", idea="فكرة", report_date="2026-03-03",
        )

    # ── عزل المستأجر ────────────────────────────────────────────────────

    def test_nothing_from_another_school_is_ever_returned(self):
        for actor in (self.manager, self.teacher):
            with self.subTest(actor=actor.name):
                titles = [hit.title for hit in search(actor, self.mine, "زيارة ميدانية")]
                self.assertNotIn("زيارة ميدانية للحديقة", titles)

    def test_a_manager_searching_their_own_school_sees_only_it(self):
        hits = search(self.manager, self.mine, "زيارة")
        self.assertTrue(hits)
        for hit in hits:
            self.assertNotIn("الحديقة", hit.title)

    def test_the_other_school_manager_cannot_reach_our_data(self):
        outsider_manager = Teacher.objects.create_user(
            phone="500300005", name="مدير آخر", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.theirs, teacher=outsider_manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        titles = [hit.title for hit in search(outsider_manager, self.theirs, "زيارة")]
        self.assertNotIn("زيارة ميدانية للمتحف", titles)
        self.assertNotIn("زيارة ميدانية للمصنع", titles)

    # ── نطاق الصلاحية داخل المدرسة الواحدة ──────────────────────────────

    def test_a_teacher_does_not_see_a_colleagues_report(self):
        titles = [hit.title for hit in search(self.teacher, self.mine, "زيارة ميدانية")]

        self.assertIn("زيارة ميدانية للمتحف", titles)
        self.assertNotIn("زيارة ميدانية للمصنع", titles)

    def test_a_manager_sees_every_report_in_the_school(self):
        titles = [hit.title for hit in search(self.manager, self.mine, "زيارة ميدانية")]

        self.assertIn("زيارة ميدانية للمتحف", titles)
        self.assertIn("زيارة ميدانية للمصنع", titles)

    def test_assignment_result_opens_the_assignment_not_an_unrelated_target(self):
        """The parent id and target id can collide; search must never mix them."""
        assignment = Assignment.objects.create(
            school=self.mine,
            issuer=self.manager,
            title="تكليف متابعة الزيارة",
            description="متابعة التنفيذ",
            due_at=timezone.now() + timedelta(days=2),
        )

        hits = [
            hit
            for hit in search(self.manager, self.mine, "تكليف متابعة الزيارة")
            if hit.kind == "assignment"
        ]

        self.assertEqual(len(hits), 1)
        self.assertEqual(
            hits[0].url,
            reverse("reports:assignment_view", args=[assignment.pk]),
        )
        self.assertNotIn("/assignments/target/", hits[0].url)

    def test_the_staff_directory_is_closed_to_teachers(self):
        """أسماء الزملاء وأرقامهم بيانات شخصية، وشاشتها للمدير."""
        teacher_hits = [h for h in search(self.teacher, self.mine, "معلم") if h.kind == "teacher"]
        manager_hits = [h for h in search(self.manager, self.mine, "معلم") if h.kind == "teacher"]

        self.assertEqual(teacher_hits, [])
        self.assertTrue(manager_hits)

    def test_a_notification_addressed_to_someone_else_stays_hidden(self):
        notification = Notification.objects.create(
            title="تعميم سرّي", message="نص", school=self.mine
        )
        NotificationRecipient.objects.create(
            notification=notification, teacher=self.colleague
        )

        titles = [hit.title for hit in search(self.teacher, self.mine, "سرّي")]
        self.assertNotIn("تعميم سرّي", titles)

        titles = [hit.title for hit in search(self.colleague, self.mine, "سرّي")]
        self.assertIn("تعميم سرّي", titles)

    def test_a_ticket_belonging_to_someone_else_stays_hidden(self):
        ticket = Ticket.objects.create(
            school=self.mine, creator=self.colleague, is_platform=False,
            title="طلب صيانة خاص", body="نص",
        )

        teacher_titles = [h.title for h in search(self.teacher, self.mine, "صيانة خاص")]
        self.assertFalse(any("صيانة خاص" in t for t in teacher_titles))

        owner_titles = [h.title for h in search(self.colleague, self.mine, "صيانة خاص")]
        self.assertTrue(any("صيانة خاص" in t for t in owner_titles))

        manager_titles = [h.title for h in search(self.manager, self.mine, "صيانة خاص")]
        self.assertTrue(any("صيانة خاص" in t for t in manager_titles))
        self.assertTrue(ticket.pk)

    def test_a_draft_document_of_another_owner_stays_hidden(self):
        Document.objects.create(
            school=self.mine, owner=self.colleague, uploaded_by=self.colleague,
            title="مسودة ميزانية القسم",
        )

        titles = [hit.title for hit in search(self.teacher, self.mine, "ميزانية القسم")]
        self.assertNotIn("مسودة ميزانية القسم", titles)

    # ── الحدود ──────────────────────────────────────────────────────────

    def test_no_active_school_means_no_results(self):
        """الغياب يُعامَل كمنعٍ لا كـ«ابحث في كل مدارسك»."""
        self.assertEqual(search(self.manager, None, "زيارة"), [])

    def test_short_queries_are_refused(self):
        self.assertEqual(search(self.manager, self.mine, "ز"), [])
        self.assertEqual(search(self.manager, self.mine, "  "), [])
        self.assertGreaterEqual(MIN_QUERY_LENGTH, 2)

    def test_anonymous_users_get_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(search(AnonymousUser(), self.mine, "زيارة"), [])

    def test_results_are_capped(self):
        for index in range(40):
            Report.objects.create(
                school=self.mine, teacher=self.teacher, category=self.category,
                title=f"زيارة رقم {index}", idea="فكرة", report_date="2026-04-01",
            )
        hits = search(self.manager, self.mine, "زيارة", per_kind=5, max_total=12)
        self.assertLessEqual(len(hits), 12)

    # ── التطبيع العربي ──────────────────────────────────────────────────

    def test_arabic_normalisation_survives_the_pipeline(self):
        """«زياره» بالتاء المربوطة يجب أن تجد «زيارة»، وإلا فالمحرّك بلا فائدة."""
        titles = [hit.title for hit in search(self.manager, self.mine, "زياره ميدانيه")]
        self.assertIn("زيارة ميدانية للمتحف", titles)

    # ── التدهور ─────────────────────────────────────────────────────────

    def test_one_broken_provider_does_not_break_the_search(self):
        with patch(
            "reports.search._search_tickets", side_effect=RuntimeError("تعثّر")
        ):
            with self.assertLogs("tawtheeq.degraded", level=logging.ERROR) as captured:
                hits = search(self.manager, self.mine, "زيارة ميدانية")

        # التقارير ما زالت تصل.
        self.assertTrue([h for h in hits if h.kind == "report"])
        self.assertTrue(any("search.tickets" in line for line in captured.output))


@override_settings(ALLOWED_HOSTS=["testserver"])
class UnifiedSearchEndpointTests(TestCase):
    def setUp(self):
        plan = SubscriptionPlan.objects.create(
            name="Plan", price=0, days_duration=30, max_teachers=10
        )
        self.school = School.objects.create(name="مدرسة", code="search-endpoint")
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.user = Teacher.objects.create_user(
            phone="500400001", name="مدير", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=self.user,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        category = ReportType.objects.create(name="نوع", code="kind", school=self.school)
        Report.objects.create(
            school=self.school, teacher=self.user, category=category,
            title="اجتماع أولياء الأمور", idea="فكرة", report_date="2026-05-01",
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_endpoint_returns_scoped_results(self):
        response = self.client.get(reverse("reports:global_search"), {"q": "أولياء"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["results"])
        self.assertEqual(payload["results"][0]["kind"], "report")

    def test_endpoint_requires_authentication(self):
        self.client.logout()
        response = self.client.get(reverse("reports:global_search"), {"q": "أولياء"})

        self.assertIn(response.status_code, {302, 403})

    def test_endpoint_is_quiet_for_short_queries(self):
        response = self.client.get(reverse("reports:global_search"), {"q": "أ"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_endpoint_is_not_indexable(self):
        """صفحاتُ نتائج البحث لا تُفهرس: محتواها خاص بمن سأل."""
        response = self.client.get(reverse("reports:global_search"), {"q": "أولياء"})

        self.assertIn("noindex", response.headers.get("X-Robots-Tag", ""))


@override_settings(ALLOWED_HOSTS=["testserver"])
class UnifiedSearchInterfaceTests(TestCase):
    """الصندوق صالحٌ لمن يعمل بلوحة المفاتيح، وآمنٌ أمام محتوى المستخدمين."""

    def setUp(self):
        plan = SubscriptionPlan.objects.create(
            name="Plan", price=0, days_duration=30, max_teachers=10
        )
        self.school = School.objects.create(name="مدرسة", code="search-ui")
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.user = Teacher.objects.create_user(
            phone="500500001", name="معلم", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_the_box_is_a_proper_combobox(self):
        html = self.client.get(reverse("reports:home")).content.decode("utf-8")

        self.assertIn('role="combobox"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('aria-controls="globalSearchResults"', html)
        self.assertIn('role="listbox"', html)
        # قارئ الشاشة لا يرى القائمة تنبثق، فيُقال له كم نتيجة وصلت.
        self.assertIn('id="globalSearchStatus"', html)
        self.assertIn('aria-live="polite"', html)

    def test_the_box_is_hidden_without_an_active_school(self):
        session = self.client.session
        session.pop("active_school_id", None)
        session.save()

        html = self.client.get(reverse("reports:select_school")).content.decode("utf-8")
        self.assertNotIn('id="globalSearchInput"', html)

    def test_results_are_built_as_text_never_as_markup(self):
        """عناوين النتائج محتوى مستخدمين — بناؤها كـHTML يجعلها ناقل حقن."""
        script = (
            Path(settings.BASE_DIR) / "static" / "js" / "global-search.js"
        ).read_text(encoding="utf-8")

        self.assertIn("textContent", script)
        # سباق الردود يُلغى صراحةً، وإلا كتب ردٌّ قديم فوق نتيجة أحدث.
        self.assertIn("AbortController", script)

        # الفحص على الشيفرة لا على التعليقات: رأس الملف يشرح لماذا مُنع
        # ``innerHTML`` هنا، فذكرُه فيه ليس استعمالاً له.
        code = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        self.assertNotIn("innerHTML", code)
