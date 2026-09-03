"""الموظف الإداري ومحضّر المختبر: ما يفتحانه، وما يقرآنه.

**دورٌ واحد بمسمّيين.** ``ADMIN_STAFF`` يحمل ``JobTitle.ADMIN_STAFF`` و
``JobTitle.LAB_TECH``، ولكلٍّ مساحته: الإداري له «مركز عمل الموظف الإداري»،
والمحضّر له المختبر وعهدته وتجاربه. وفحصُ أحدهما لا يُغني عن الآخر.

**والنطاق يأتي من ``lab_kind`` لا من حمل المسمّى.** محضّرٌ لم يُسنَد إليه نوع
مختبر يرى قوائم فارغة و‎404‎ على التفاصيل — وهو سلوكٌ متّسق مقصود، لا عطل:
مجموعةٌ فارغة تعني «لا شيء» لا «كل شيء». ويُثبَّت هنا لئلا ينقلب يوماً.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    LabAsset,
    LabExperiment,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class LabTechnicianScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(
            name="مدرسة المختبر", code="lab-tasks", current_academic_year="1448-1449"
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة المختبر", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)

        roles = SchoolMembership.RoleType
        titles = SchoolMembership.JobTitle
        cls.manager = Teacher.objects.create_user(
            phone="500111001", name="مدير", password="x", is_staff=True
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.manager, role_type=roles.MANAGER, is_active=True
        )

        cls.scoped = Teacher.objects.create_user(
            phone="500111002", name="محضّر بنطاق", password="x"
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.scoped, role_type=roles.ADMIN_STAFF,
            job_title=titles.LAB_TECH, lab_kind="science", is_active=True,
        )
        cls.unscoped = Teacher.objects.create_user(
            phone="500111003", name="محضّر بلا نطاق", password="x"
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.unscoped, role_type=roles.ADMIN_STAFF,
            job_title=titles.LAB_TECH, is_active=True,
        )

        cls.asset = LabAsset.objects.create(
            school=cls.school, lab_kind="science", name="زركون-عهدة", code="A1",
            quantity=2, condition="good", recorded_by=cls.scoped, is_active=True,
        )
        cls.experiment = LabExperiment.objects.create(
            school=cls.school, lab_kind="science", recorder=cls.scoped,
            title="زركون-تجربة", experiment_date=timezone.localdate(),
            subject="علوم", class_name="ثاني/1", students_count=20,
            objectives="هدف", procedure="خطوات",
        )

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        return self.client

    def test_a_scoped_technician_sees_and_opens_their_lab(self):
        client = self._login(self.scoped)

        for route, args in (
            ("reports:lab_dashboard", []),
            ("reports:lab_assets", []),
            ("reports:lab_experiments", []),
            ("reports:lab_asset_detail", [self.asset.pk]),
            ("reports:lab_experiment_detail", [self.experiment.pk]),
            ("reports:lab_assets_print", []),
        ):
            with self.subTest(route=route):
                self.assertEqual(client.get(reverse(route, args=args)).status_code, 200)

        self.assertContains(client.get(reverse("reports:lab_assets")), "زركون-عهدة")
        self.assertContains(client.get(reverse("reports:lab_experiments")), "زركون-تجربة")

    def test_an_unscoped_technician_sees_nothing_rather_than_everything(self):
        """نطاقٌ لم يُضبط بعد لا يُقرأ صلاحيةً على المختبر كلّه.

        والقائمة الفارغة والـ‎404‎ متّسقتان: ما لا يظهر في الكشف لا يُفتح
        بالرابط. والتناقض بينهما هو ما يُقلق، لا اجتماعُهما.
        """
        client = self._login(self.unscoped)

        listing = client.get(reverse("reports:lab_assets"))
        self.assertEqual(listing.status_code, 200)
        self.assertNotContains(listing, "زركون-عهدة")

        self.assertEqual(
            client.get(reverse("reports:lab_asset_detail", args=[self.asset.pk])).status_code,
            404,
        )
        self.assertEqual(
            client.get(
                reverse("reports:lab_experiment_detail", args=[self.experiment.pk])
            ).status_code,
            404,
        )

    def test_the_lab_stays_shut_to_a_teacher(self):
        teacher = Teacher.objects.create_user(
            phone="500111004", name="معلّم", password="x"
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=teacher,
            role_type=SchoolMembership.RoleType.TEACHER, is_active=True,
        )
        client = self._login(teacher)

        for route in ("reports:lab_dashboard", "reports:lab_assets", "reports:lab_experiments"):
            with self.subTest(route=route):
                self.assertNotEqual(client.get(reverse(route)).status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class AdminStaffWorkspaceTests(TestCase):
    """مركز عمل الموظف الإداري: بطاقاتٌ تؤدي إلى وجهاتها."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(
            name="مدرسة الإداري", code="adm-tasks", current_academic_year="1448-1449"
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة الإداري", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        roles = SchoolMembership.RoleType
        cls.manager = Teacher.objects.create_user(
            phone="500112001", name="مدير", password="x", is_staff=True
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.manager, role_type=roles.MANAGER, is_active=True
        )
        cls.staff = Teacher.objects.create_user(
            phone="500112002", name="موظف إداري", password="x"
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.staff, role_type=roles.ADMIN_STAFF,
            job_title=SchoolMembership.JobTitle.ADMIN_STAFF, is_active=True,
        )

    def _login(self):
        self.client.force_login(self.staff)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        return self.client

    def test_every_card_on_the_workspace_leads_somewhere(self):
        """بطاقةٌ تُعرض ثم تُحوّل عند النقر وعدٌ يُخلَف.

        وهو مبدأ الشاشة نفسه: «أدوات المراجعة لا تظهر إلا عند منحها فعلاً».
        فما عُرض يجب أن يُفتح.
        """
        client = self._login()

        for route in (
            "reports:assigned_to_me",
            "reports:document_archive",
            "reports:meeting_list",
            "reports:initiative_list",
            "reports:my_assignments",
            "reports:add_report",
        ):
            with self.subTest(route=route):
                response = client.get(reverse(route))
                self.assertEqual(
                    response.status_code, 200, f"{route} عُرض في اللوحة ولم يُفتح."
                )

    def test_the_manager_screens_stay_shut(self):
        client = self._login()

        for route in ("reports:admin_dashboard", "reports:manage_teachers", "reports:admin_reports"):
            with self.subTest(route=route):
                self.assertNotEqual(client.get(reverse(route)).status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ReportLabelReadsHijriTests(TestCase):
    """وصف التقرير يُقرأ حيث يُعرض.

    ``Report.__str__`` ليس للسجلّات وحدها: هو ما يظهر في كل قائمة اختيارٍ
    تعرض تقريراً — منها ربطُ تقرير بتجربة مختبر، وهي شاشة المحضّر. وكان يطبع
    ``report_date`` خاماً فيقرأ تقويمين في سطرٍ واحد.
    """

    def test_the_label_carries_a_hijri_date(self):
        school = School.objects.create(name="مدرسة الوصف", code="label-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة الوصف", price=0, days_duration=365, max_teachers=10
        )
        SchoolSubscription.objects.create(school=school, plan=plan)
        teacher = Teacher.objects.create_user(phone="500113001", name="معلّم", password="x")
        SchoolMembership.objects.create(
            school=school, teacher=teacher,
            role_type=SchoolMembership.RoleType.TEACHER, is_active=True,
        )
        report_type = ReportType.objects.create(school=school, code="lb", name="تقرير")
        report = Report.objects.create(
            school=school, teacher=teacher, category=report_type,
            title="عنوان", report_date=timezone.localdate(),
        )

        label = str(report)

        self.assertIn("هـ", label)
        self.assertNotIn(str(timezone.localdate().year), label)

    def test_a_report_without_a_date_does_not_show_empty_brackets(self):
        # «عنوان - تصنيف - اسم ()» أقبح من غياب التاريخ.
        school = School.objects.create(name="مدرسة بلا تاريخ", code="nodate-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة بلا تاريخ", price=0, days_duration=365, max_teachers=10
        )
        SchoolSubscription.objects.create(school=school, plan=plan)
        teacher = Teacher.objects.create_user(phone="500113002", name="معلّم", password="x")
        report_type = ReportType.objects.create(school=school, code="nd", name="تقرير")
        report = Report(
            school=school, teacher=teacher, category=report_type,
            title="بلا تاريخ", report_date=None,
        )

        self.assertNotIn("()", str(report))
