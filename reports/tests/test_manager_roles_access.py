"""من يرى ماذا: الأدوار الأربعة على المسارات التي مسّها صقل لوحة المدير.

**لماذا وحدةٌ مستقلة عن ``test_manager_ux_polish``.** تلك تحرس *ما يراه* مدير
المدرسة، وهذه تحرس *من يراه*. والفرق بينهما ليس تنظيماً: الأولى تسقط إن ساءت
الصياغة، وهذه تسقط إن تسرّبت البيانات — ولا يجوز أن يخفي أحدهما فشل الآخر.

**وسبب وجودها.** بُنيت التغطية والمواعيد والأقسام والتذكير على لوحة المدير،
واختُبرت بحسابه وحده. وحسابٌ واحد لا يُثبت عزلاً: الشاشة قد تكون محميّة
والبياناتُ التي تغذّيها مكشوفة من مسارٍ آخر. فالأدوار الأربعة تُجرَّب هنا على
كل مسارٍ مسّه العمل، والمعلّم بينها — فهو الحدّ الذي يجب ألا يُعبر.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.coverage import pending_documenters, school_staff_queryset
from reports.models import (
    Delegation,
    Department,
    DepartmentMembership,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ManagerSurfaceRoleAccessTests(TestCase):
    """المسارات التي مسّها العمل، مجرَّبةً بالأدوار الأربعة."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(
            name="مدرسة فحص الأدوار",
            code="role-probe",
            current_academic_year="1448-1449",
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة الأدوار", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        cls.report_type = ReportType.objects.create(
            school=cls.school, code="rp", name="تقرير"
        )

        roles = SchoolMembership.RoleType
        cls.accounts = {}
        for index, role in enumerate(
            [roles.MANAGER, roles.DEPUTY, roles.ADMIN_STAFF, roles.TEACHER]
        ):
            teacher = Teacher.objects.create_user(
                phone=f"50055500{index}",
                name=f"حساب {role}",
                password="probe-pass",
                is_staff=(role == roles.MANAGER),
            )
            SchoolMembership.objects.create(
                school=cls.school, teacher=teacher, role_type=role, is_active=True
            )
            cls.accounts[role] = teacher

        # وكيلٌ مفوَّضٌ صراحةً: هذا هو المسار الذي تقصده المنصة حين تقول
        # «الوكيل يراجع ويوصي» — لا الوكيل العاري من كل نطاق.
        cls.delegated_deputy = Teacher.objects.create_user(
            phone="500555050", name="وكيل مفوَّض", password="probe-pass"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.delegated_deputy,
            role_type=roles.DEPUTY,
            is_active=True,
        )
        now = timezone.now()
        Delegation.objects.create(
            school=cls.school,
            delegator=cls.accounts[roles.MANAGER],
            delegate=cls.delegated_deputy,
            capabilities=["view_school_dashboard", "review_reports", "handle_requests"],
            reason="فحص الوصول",
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(days=7),
        )

        # معلّمٌ لم يوثّق — هو ما تكشفه قائمة التذكير، فوجوده شرط الفحص.
        cls.silent = Teacher.objects.create_user(
            phone="500555099", name="معلم صامت", password="x"
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.silent, role_type=roles.TEACHER, is_active=True
        )
        Report.objects.create(
            school=cls.school,
            teacher=cls.accounts[roles.TEACHER],
            category=cls.report_type,
            title="تقرير",
            report_date=timezone.localdate(),
        )

    def _client_for(self, role):
        self.client.force_login(self.accounts[role])
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        return self.client

    # ── اللوحة نفسها ──────────────────────────────────────────────────────

    def test_only_the_manager_opens_the_manager_dashboard(self):
        roles = SchoolMembership.RoleType
        allowed = {roles.MANAGER}

        for role in self.accounts:
            with self.subTest(role=str(role)):
                response = self._client_for(role).get(reverse("reports:admin_dashboard"))
                if role in allowed:
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertNotEqual(response.status_code, 200)

    def test_the_dashboard_feed_is_gated_like_the_page_it_feeds(self):
        """الشاشة محميّة لا تكفي إن كان مصدرها مفتوحاً.

        ‎/api/dashboard/school/‎ يحمل التغطية والاتجاه والأقسام كاملةً بصيغة
        ‎JSON‎. ولو حُرست الصفحة وتُرك مصدرها لَقُرئ ما فيها بسطرٍ واحد.
        """
        roles = SchoolMembership.RoleType

        for role in self.accounts:
            with self.subTest(role=str(role)):
                response = self._client_for(role).get(
                    reverse("reports:api_admin_dashboard_data")
                )
                if role == roles.MANAGER:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("coverage", response.json())
                else:
                    self.assertNotEqual(response.status_code, 200)

    # ── التذكير بالتوثيق ──────────────────────────────────────────────────

    def test_the_reminder_prefill_never_reaches_a_teacher(self):
        """قائمة من لم يوثّق ليست معلومةً لكل من يُرسل إشعاراً.

        شاشة الإشعار مفتوحةٌ لمن هو أوسع من المدير، والتعبئة المسبقة تُلحق بها
        كشفاً بأسماء المتأخّرين. فإن مرّت لمعلّم، صار كلُّ معلّمٍ يعرف من تأخّر
        من زملائه — وهي معلومةٌ إشرافية لا زمالة.
        """
        roles = SchoolMembership.RoleType
        url = reverse("reports:notifications_create") + "?remind=coverage"

        response = self._client_for(roles.TEACHER).get(url)

        if response.status_code == 200:
            context = getattr(response, "context", None) or {}
            self.assertIsNone(
                context.get("reminder_context"),
                "معلّمٌ حصل على قائمة من لم يوثّق من زملائه.",
            )

    def test_the_manager_still_gets_the_prefill(self):
        roles = SchoolMembership.RoleType
        url = reverse("reports:notifications_create") + "?remind=coverage"

        response = self._client_for(roles.MANAGER).get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context.get("reminder_context"))
        selected = set(response.context["form"].initial.get("teachers") or [])
        self.assertIn(self.silent.pk, selected)

    # ── بقيّة مسارات المدير التي مسّها العمل ───────────────────────────────

    def test_school_wide_screens_stay_shut_to_teachers(self):
        """شاشاتٌ تعرض المدرسة كلها لا فردَها.

        كلٌّ منها يعرض بيانات زملاء المعلّم: تقاريرهم، وأرقام جوالاتهم،
        وطلباتهم. فالمعلّم يُردّ عنها كلّها.
        """
        roles = SchoolMembership.RoleType
        routes = (
            "reports:admin_reports",
            "reports:manage_teachers",
            "reports:manager_school_tickets",
        )

        for route in routes:
            with self.subTest(route=route):
                response = self._client_for(roles.TEACHER).get(reverse(route))
                self.assertNotEqual(
                    response.status_code,
                    200,
                    f"المعلّم فتح {route} وهي شاشة مدرسةٍ لا شاشة فرد.",
                )


    # ── الوكيل المفوَّض ───────────────────────────────────────────────────

    def _client_for_delegated_deputy(self):
        self.client.force_login(self.delegated_deputy)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        return self.client

    def test_the_delegated_deputy_gets_a_scoped_dashboard_not_the_managers(self):
        """‎view_school_dashboard‎ يفتح شاشة النطاق لا لوحة المدير.

        وهو تمييزٌ مقصود: لوحة المدير تحمل الاشتراك والفوترة والمقاعد والمساحة
        — بيانات المدرسة كمنشأة لا مؤشرات عمل. ففتحُها للوكيل يعطيه أكثر ممّا
        مُنح، فأُفردت له شاشةٌ تقول «أين تقف الأمور» ضمن نطاقه.
        """
        client = self._client_for_delegated_deputy()

        self.assertEqual(client.get(reverse("reports:staff_dashboard")).status_code, 200)
        self.assertNotEqual(client.get(reverse("reports:admin_dashboard")).status_code, 200)

    def test_delegated_capabilities_open_exactly_what_they_name(self):
        # ‎review_reports‎ و‎handle_requests‎ مُنحتا، فتُفتح شاشتاهما. وما لم
        # يُمنح يبقى مغلقاً — التفويض ليس مفتاحاً عاماً.
        client = self._client_for_delegated_deputy()

        self.assertEqual(client.get(reverse("reports:admin_reports")).status_code, 200)
        self.assertEqual(client.get(reverse("reports:manager_school_tickets")).status_code, 200)
        self.assertNotEqual(client.get(reverse("reports:manage_teachers")).status_code, 200)
        self.assertNotEqual(client.get(reverse("reports:school_audit_logs")).status_code, 200)

    def test_a_bare_deputy_without_delegation_opens_nothing(self):
        """«الوكيل ليس مديراً مصغَّراً» — الصلاحية من النطاق لا من حمل الدور.

        وهذا نصُّ ``is_school_deputy`` في المشروع، ويُثبَّت هنا لئلا يتحوّل
        يوماً إلى منحةٍ ضمنية بحسن نيّة.
        """
        roles = SchoolMembership.RoleType
        client = self._client_for(roles.DEPUTY)

        for route in ("reports:admin_dashboard", "reports:admin_reports", "reports:staff_dashboard"):
            with self.subTest(route=route):
                self.assertNotEqual(client.get(reverse(route)).status_code, 200)

    def test_even_a_delegated_deputy_is_kept_out_of_the_reminder_list(self):
        # التذكير يكشف أسماء المتأخّرين، ولم تُمنح صلاحيةٌ باسمه. فما لم يُسمَّ
        # في التفويض لا يُستنتج منه.
        client = self._client_for_delegated_deputy()

        response = client.get(reverse("reports:notifications_create") + "?remind=coverage")

        if response.status_code == 200:
            context = getattr(response, "context", None) or {}
            self.assertIsNone(context.get("reminder_context"))

    def test_the_reminder_route_is_not_merely_redirecting_for_the_manager(self):
        """حارسٌ على الحارس.

        ‎test_the_reminder_prefill_never_reaches_a_teacher‎ يمرّ أيضاً لو صار
        المسار يُعيد التوجيه للجميع — بما فيهم المدير. فيُثبَّت هنا أن المسار
        حيٌّ فعلاً، وإلا لَحرس اختبارُ التسريب باباً مغلقاً على الكل.
        """
        roles = SchoolMembership.RoleType
        response = self._client_for(roles.MANAGER).get(
            reverse("reports:notifications_create") + "?remind=coverage"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context.get("reminder_context"))


@override_settings(ALLOWED_HOSTS=["testserver"])
class DeputyScopedCoverageTests(TestCase):
    """تغطية التوثيق داخل نطاق الوكيل — لا خارجه.

    البطاقة تكشف أسماء من لم يوثّق. وهي مشروعةٌ للوكيل داخل أقسامه لأنها عملُه،
    وغيرُ مشروعةٍ خارجها لأنه لا يُشرف. فالفحص هنا ليس «هل تظهر البطاقة» بل
    «هل تتوقّف عند حدّ النطاق».
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(
            name="مدرسة نطاق التغطية", code="scope-cov", current_academic_year="1448-1449"
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة النطاق", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        cls.report_type = ReportType.objects.create(
            school=cls.school, code="sc", name="تقرير"
        )

        roles = SchoolMembership.RoleType
        cls.manager = Teacher.objects.create_user(
            phone="500777001", name="مدير النطاق", password="x", is_staff=True
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.manager, role_type=roles.MANAGER, is_active=True
        )

        cls.deputy = Teacher.objects.create_user(
            phone="500777002", name="وكيل النطاق", password="x"
        )
        deputy_membership = SchoolMembership.objects.create(
            school=cls.school, teacher=cls.deputy, role_type=roles.DEPUTY, is_active=True
        )

        cls.inside = Department.objects.create(
            school=cls.school, name="داخل النطاق", slug="scope-in", is_active=True
        )
        cls.outside = Department.objects.create(
            school=cls.school, name="خارج النطاق", slug="scope-out", is_active=True
        )

        def staff(phone, name, department):
            teacher = Teacher.objects.create_user(phone=phone, name=name, password="x")
            SchoolMembership.objects.create(
                school=cls.school, teacher=teacher, role_type=roles.TEACHER, is_active=True
            )
            DepartmentMembership.objects.create(department=department, teacher=teacher)
            return teacher

        cls.inside_documented = staff("500777010", "داخل وثّق", cls.inside)
        cls.inside_silent = staff("500777011", "داخل صامت", cls.inside)
        cls.outside_silent = staff("500777020", "خارج صامت", cls.outside)

        Report.objects.create(
            school=cls.school, teacher=cls.inside_documented, category=cls.report_type,
            title="تقرير", report_date=timezone.localdate(),
        )

        scope = StaffScope.objects.create(membership=deputy_membership)
        scope.capabilities = ["view_school_dashboard"]
        scope.save()
        scope.departments.set([cls.inside])

        now = timezone.now()
        Delegation.objects.create(
            school=cls.school, delegator=cls.manager, delegate=cls.deputy,
            capabilities=["view_school_dashboard"], reason="فحص النطاق",
            starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=7),
        )

    def _deputy_client(self):
        self.client.force_login(self.deputy)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        return self.client

    def test_the_card_counts_only_the_supervised_departments(self):
        response = self._deputy_client().get(reverse("reports:staff_dashboard"))

        coverage = response.context["coverage"]
        self.assertEqual(coverage["total"], 2)      # داخل النطاق وحدهما
        self.assertEqual(coverage["covered"], 1)
        self.assertEqual(coverage["pending"], 1)
        self.assertEqual(coverage["percent"], 50)

    def test_a_silent_colleague_outside_the_scope_is_never_named(self):
        # هذا هو الحدّ: الاسم خارج الإشراف معلومةٌ ليست له.
        response = self._deputy_client().get(reverse("reports:staff_dashboard"))

        names = {person.name for person in response.context["coverage"]["pending_preview"]}
        self.assertIn("داخل صامت", names)
        self.assertNotIn("خارج صامت", names)
        self.assertNotContains(response, "خارج صامت")

    def test_an_unscoped_deputy_gets_no_card_rather_than_the_whole_school(self):
        """نطاقٌ فارغ يعني «لا أحد» لا «الجميع».

        وهو الخطأ الذي يقلب حارساً إلى باب: لو قُرئت المجموعة الفارغة «بلا
        تقييد» لَرأى وكيلٌ لم يُضبط نطاقُه بعدُ المدرسةَ كاملة.
        """
        roles = SchoolMembership.RoleType
        bare = Teacher.objects.create_user(phone="500777030", name="وكيل بلا نطاق", password="x")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=bare, role_type=roles.DEPUTY, is_active=True
        )
        scope = StaffScope.objects.create(membership=membership)
        scope.capabilities = ["view_school_dashboard"]
        scope.save()
        now = timezone.now()
        Delegation.objects.create(
            school=self.school, delegator=self.manager, delegate=bare,
            capabilities=["view_school_dashboard"], reason="بلا نطاق",
            starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=7),
        )

        self.client.force_login(bare)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        response = self.client.get(reverse("reports:staff_dashboard"))

        self.assertIsNone(response.context["coverage"])
        self.assertNotContains(response, "خارج صامت")

    def test_the_empty_set_is_not_read_as_no_filter(self):
        """الفحص عند المصدر لا عند الشاشة وحدها.

        ``limit_to=set()`` يجب أن يعيد لا أحد. ولو أُهمل الفرق بينه وبين
        ``None`` لَمرّ التسريب من كل مُستدعٍ آخر لهذه الدالة.
        """
        self.assertEqual(school_staff_queryset(self.school, limit_to=set()).count(), 0)
        self.assertEqual(pending_documenters(self.school, limit_to=set()).count(), 0)
        self.assertGreater(school_staff_queryset(self.school, limit_to=None).count(), 0)

    def test_the_screen_stays_read_only(self):
        """«أين تقف الأمور» لا «حرّكها».

        التذكير إرسالٌ، والإرسال ليس من هذه الشاشة. فمن لا يملك الإجراء لا
        يُعرض له زرُّه — وإلا صار الزرّ وعداً يُخلَف عند النقر.
        """
        response = self._deputy_client().get(reverse("reports:staff_dashboard"))

        self.assertNotContains(response, "remind=coverage")
        self.assertNotContains(response, "تذكير بالتوثيق")
