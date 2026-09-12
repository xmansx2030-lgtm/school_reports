# -*- coding: utf-8 -*-
"""عقد ``nav_context``: ما تُنتجه، وبكم استعلاماً تُنتجه.

هذه الوحدة تعمل في **كل طلب لكل مستخدم**، فهي أسخن مسار في المنصة. وكانت دالةً
واحدة من 425 سطراً — لا يمكن قراءتها ولا تعديلها بثقة، ولا يوجد ما يمنع أن
يضيف تعديلٌ صغير استعلاماً لكل صفحة في النظام.

فالاختبار هنا **عقد** لا تغطية:

* **الناتج** — كل مفتاح يقرؤه القالب، لكل دور. تفكيكُ الدالة لا يجوز أن يغيّر
  حرفاً منه.
* **عدد الاستعلامات** — سقفٌ مقفل. الانحدار في الأداء هنا صامت تماماً: لا خطأ
  ولا بطء ملحوظ في التطوير، فقط استعلامٌ إضافي × كل صفحة × كل مستخدم.

الأرقام أدناه قيست قبل التفكيك، وهي **سقف لا هدف**: خفضُها مرحَّب به ويستوجب
تحديث الرقم؛ ورفعُها يجب أن يكون قراراً مكتوباً لا مفاجأة.
"""
from __future__ import annotations

from django.test import RequestFactory, TestCase, override_settings

from reports.context_processors import nav_context
from reports.models import (
    Department,
    DepartmentMembership,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)

# سقف الاستعلامات لكل دور، مقيساً على المسار البارد (بلا كاش).
#
# الأرقام قبل التفكيك كانت 28 / 24 / 29. وخفضُها خمسةً لكل دور جاء من موضعين
# لا من إعادة الهيكلة نفسها:
#
#   * استبعادُ الإشعارات المُخفاة كان يستعلم عن ثمانين معرّفاً في كل طلب ليسأل
#     الكوكيز عنها — ولمن لم يُخفِ شيئاً قط (الغالبية) لا شيء ليُستبعد. صار
#     السؤال يسبق الاستعلام: مفتاحُ كوكي واحد أو لا استعلام. (‑2)
#   * ``_user_department_codes`` كانت تُنادى مرتين في الطلب — مرة لمسار الإشعار
#     البارز ومرة لعدّاد غير المقروء — لنتيجةٍ لا تتغيّر داخل الطلب. (‑2 إلى ‑3)
MAX_QUERIES = {
    "teacher": 23,
    # +1 مقصود عن القياس السابق: ``EXISTS`` لحالة إعداد أنواع التقارير.
    "manager": 20,
    "officer": 24,
    "anonymous": 0,
}

# كل مفتاح يقرؤه أي قالب. غيابُ مفتاح لا يُنتج خطأً في Django — يُقرأ فارغاً،
# فيختفي رابطٌ أو عدّاد بلا أثر. ولذلك تُفحص المجموعة كاملة لا عيّنة منها.
EXPECTED_KEYS = {
    "NAV_MY_OPEN_TICKETS",
    "NAV_ASSIGNED_TO_ME",
    "IS_OFFICER",
    "OFFICER_DEPARTMENT",
    "OFFICER_DEPARTMENTS",
    "SHOW_OFFICER_REPORTS_LINK",
    "SHOW_DEPARTMENT_REPORTS_LINK",
    "SHOW_SCHOOL_REPORTS_LINK",
    "SHOW_ARCHIVE_LINK",
    "HAS_REPORT_TYPES",
    "DEPARTMENT_REPORTS_URLNAME",
    "NAV_OFFICER_REPORTS",
    "SHOW_ADMIN_DASHBOARD_LINK",
    "IS_SCHOOL_MANAGER",
    "IS_SCHOOL_DEPUTY",
    "IS_ADMIN_STAFF",
    "HAS_TEACHER_ROLE",
    "SHOW_PERSONAL_ACHIEVEMENT",
    "IS_LAB_TECHNICIAN",
    "SHOW_LAB_NAV",
    "IS_EXECUTIVE_DIRECTOR",
    "IS_GROUP_ONLY_DIRECTOR",
    "GROUP_NAME",
    "SHOW_ASSIGNED_TO_ME",
    "SHOW_SUPERVISION_GROUP",
    "NAV_NOTIFICATIONS_UNREAD",
    "NAV_SIGNATURES_PENDING",
    "NAV_NOTIFICATION_HERO",
    "CAN_SEND_NOTIFICATIONS",
    "SEND_NOTIFICATION_URL",
    "SCHOOL_NAME",
    "SCHOOL_LOGO_URL",
    "USER_ROLE_LABEL",
}


@override_settings(ALLOWED_HOSTS=["testserver"], NAV_CONTEXT_CACHE_TTL_SECONDS=0)
class NavContextContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="مدرسة العقد", code="nav-contract")
        plan = SubscriptionPlan.objects.create(
            name="Plan", price=0, days_duration=30, max_teachers=50
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)

        cls.teacher = Teacher.objects.create_user(
            phone="500111222", name="معلم", password="pass"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        cls.manager = Teacher.objects.create_user(
            phone="500111333", name="مدير", password="pass"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

        cls.department = Department.objects.create(
            name="قسم", slug="nav-contract-dept", school=cls.school
        )
        cls.officer = Teacher.objects.create_user(
            phone="500111444", name="رئيس قسم", password="pass"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.officer,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        DepartmentMembership.objects.create(
            department=cls.department,
            teacher=cls.officer,
            role_type=getattr(DepartmentMembership, "OFFICER", "officer"),
        )

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, user, *, with_school: bool = True):
        request = self.factory.get("/")
        request.user = user
        request.session = {"active_school_id": self.school.id} if with_school else {}
        request.COOKIES = {}
        return request

    # ── الناتج ──────────────────────────────────────────────────────────

    def test_every_template_key_is_present_for_each_role(self):
        for label, user in (
            ("teacher", self.teacher),
            ("manager", self.manager),
            ("officer", self.officer),
        ):
            with self.subTest(role=label):
                context = nav_context(self._request(user))
                missing = EXPECTED_KEYS - set(context)
                self.assertEqual(missing, set(), f"مفاتيح ناقصة للدور {label}")

    def test_anonymous_context_matches_authenticated_key_set(self):
        """الزائر يأخذ الشكل نفسه بقيم صفرية — لا شكلاً أنقص.

        القالب واحد للحالتين، ومفتاحٌ يوجد لأحدهما دون الآخر يُقرأ فارغاً بلا
        خطأ — فيختفي عنصرٌ في حالةٍ ويظهر في أخرى بلا سبب مفهوم.
        """
        from django.contrib.auth.models import AnonymousUser

        request = self._request(AnonymousUser(), with_school=False)
        context = nav_context(request)

        self.assertEqual(EXPECTED_KEYS - set(context), set())

    def test_manager_flags(self):
        context = nav_context(self._request(self.manager))

        self.assertTrue(context["IS_SCHOOL_MANAGER"])
        self.assertTrue(context["SHOW_SCHOOL_REPORTS_LINK"])
        self.assertTrue(context["SHOW_ADMIN_DASHBOARD_LINK"])
        self.assertTrue(context["CAN_SEND_NOTIFICATIONS"])
        # المدير خارج مجموعة «الإشراف»: مجموعاته تغطّيها.
        self.assertFalse(context["SHOW_SUPERVISION_GROUP"])
        self.assertEqual(context["SCHOOL_NAME"], self.school.name)
        self.assertFalse(context["HAS_REPORT_TYPES"])

        ReportType.objects.create(
            school=self.school,
            code="nav-ready",
            name="نوع جاهز",
        )
        context = nav_context(self._request(self.manager))
        self.assertTrue(context["HAS_REPORT_TYPES"])

    def test_teacher_flags(self):
        context = nav_context(self._request(self.teacher))

        self.assertFalse(context["IS_SCHOOL_MANAGER"])
        self.assertFalse(context["IS_OFFICER"])
        self.assertTrue(context["HAS_TEACHER_ROLE"])
        self.assertTrue(context["SHOW_PERSONAL_ACHIEVEMENT"])
        self.assertFalse(context["CAN_SEND_NOTIFICATIONS"])
        self.assertFalse(context["HAS_REPORT_TYPES"])

    def test_officer_flags(self):
        context = nav_context(self._request(self.officer))

        self.assertTrue(context["IS_OFFICER"])
        self.assertEqual(context["OFFICER_DEPARTMENT"], self.department)
        self.assertEqual(list(context["OFFICER_DEPARTMENTS"]), [self.department])
        self.assertTrue(context["SHOW_OFFICER_REPORTS_LINK"])
        self.assertTrue(context["SHOW_DEPARTMENT_REPORTS_LINK"])
        self.assertEqual(context["DEPARTMENT_REPORTS_URLNAME"], "reports:officer_reports")
        self.assertTrue(context["CAN_SEND_NOTIFICATIONS"])

    # ── عدد الاستعلامات ─────────────────────────────────────────────────

    def test_query_budget_teacher(self):
        with self.assertNumQueries(MAX_QUERIES["teacher"]):
            nav_context(self._request(self.teacher))

    def test_query_budget_manager(self):
        # استعلام ``EXISTS`` واحد خاص بالمدير يمنع زر «تقرير جديد» المسدود على
        # كل الصفحات، ولا يحمّل قائمة الأنواع أو يضيف كلفةً لبقية الأدوار.
        with self.assertNumQueries(MAX_QUERIES["manager"]):
            nav_context(self._request(self.manager))

    def test_query_budget_officer(self):
        with self.assertNumQueries(MAX_QUERIES["officer"]):
            nav_context(self._request(self.officer))

    def test_anonymous_costs_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        with self.assertNumQueries(0):
            nav_context(self._request(AnonymousUser(), with_school=False))

    # ── قِصَر الدائرة عند غياب كوكيز الإخفاء ─────────────────────────────

    def test_dismissal_filtering_is_skipped_when_no_cookie_exists(self):
        """السؤال يسبق الاستعلام — والعكس كان يكلّف استعلامين لكل طلب."""
        from reports.context_processors import (
            _exclude_notif_dismissed_cookies_notif_qs,
            _has_dismissal_cookies,
        )
        from reports.models import Notification

        request = self._request(self.teacher)
        self.assertFalse(_has_dismissal_cookies(request))

        with self.assertNumQueries(0):
            returned = _exclude_notif_dismissed_cookies_notif_qs(
                Notification.objects.all(), request
            )
        # ولا يُغيَّر الاستعلام أصلاً حين لا شيء ليُستبعد.
        self.assertIs(returned.query.model, Notification)

    def test_dismissal_filtering_still_runs_when_a_cookie_exists(self):
        """التحسين لا يُلغي الوظيفة: من أخفى إشعاراً لا يراه ثانيةً."""
        from reports.context_processors import _exclude_notif_dismissed_cookies_notif_qs
        from reports.models import Notification

        hidden = Notification.objects.create(
            title="مُخفى", message="نص", school=self.school
        )
        visible = Notification.objects.create(
            title="ظاهر", message="نص", school=self.school
        )

        request = self._request(self.teacher)
        request.COOKIES = {f"notif_dismissed_{hidden.pk}": "1"}

        remaining = list(
            _exclude_notif_dismissed_cookies_notif_qs(Notification.objects.all(), request)
        )
        self.assertIn(visible, remaining)
        self.assertNotIn(hidden, remaining)

    def test_department_codes_are_resolved_once_per_request(self):
        """النتيجة لا تتغيّر داخل الطلب، فلا تُستعلم مرتين."""
        from reports.context_processors import _user_department_codes

        with self.assertNumQueries(1):
            first = _user_department_codes(self.officer, self.school)
            second = _user_department_codes(self.officer, self.school)

        self.assertEqual(first, second)
        self.assertEqual(first, [self.department.slug])
