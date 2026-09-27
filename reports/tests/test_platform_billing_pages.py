from datetime import datetime

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import Payment, School, SchoolSubscription, SubscriptionPlan, Teacher
from personal.models import PersonalPlan, PersonalSubscription, PersonalWorkspace


@override_settings(ALLOWED_HOSTS=["testserver"])
class PlatformBillingPagesTests(TestCase):
    def setUp(self):
        self.admin = Teacher.objects.create_superuser(
            phone="599000001",
            name="Platform Admin",
            password="pass",
        )
        self.plan = SubscriptionPlan.objects.create(
            name="Annual",
            price=1200,
            days_duration=365,
            max_teachers=0,
        )
        self.school = School.objects.create(
            name="Revenue School",
            code="revenue-school",
        )
        self.subscription = SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
        )
        Payment.objects.create(
            school=self.school,
            subscription=self.subscription,
            requested_plan=self.plan,
            amount=1200,
            payment_date=timezone.localdate(),
            status=Payment.Status.APPROVED,
            notes="Approved payment",
            created_by=self.admin,
        )
        Payment.objects.create(
            school=self.school,
            subscription=self.subscription,
            requested_plan=self.plan,
            amount=1200,
            payment_date=timezone.localdate(),
            status=Payment.Status.PENDING,
            notes="Pending payment",
            created_by=self.admin,
        )
        self.client.force_login(self.admin)

    def test_platform_subscriptions_list_renders_commercial_summary(self):
        response = self.client.get(reverse("reports:platform_subscriptions_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "قيمة الاشتراكات السارية")
        self.assertContains(response, "محفظة الاشتراكات")
        self.assertContains(response, self.school.name)

    def test_platform_subscriptions_distinguishes_school_and_personal_types(self):
        teacher = Teacher.objects.create_user(
            phone="0557999001", name="معلمة فردية", password="Personal#2026"
        )
        workspace = PersonalWorkspace.objects.create(
            owner=teacher, school_name="اسم مدرسة للتعريف"
        )
        personal_plan, _ = PersonalPlan.objects.get_or_create(
            code="personal_free", defaults={"name": "أساسية", "price": 0}
        )
        PersonalSubscription.objects.create(workspace=workspace, plan=personal_plan)

        response = self.client.get(reverse("reports:platform_subscriptions_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-label="نوع الاشتراك"', count=2)
        self.assertContains(response, "معلم/معلمة فردي")
        self.assertContains(response, "معلمة فردية")
        self.assertContains(response, "للتعريف فقط")
        self.assertEqual(response.context["stats_total"], 1)
        self.assertEqual(response.context["personal_results_count"], 1)

    def test_platform_payments_list_renders_financial_summary_and_search(self):
        response = self.client.get(
            reverse("reports:platform_payments_list"),
            data={"q": "Revenue", "status": "active"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "صافي الإيراد")
        self.assertContains(response, "عمليات التحصيل")
        self.assertContains(response, self.school.name)

    def test_pending_tab_filters_to_pending_only(self):
        response = self.client.get(
            reverse("reports:platform_payments_list"),
            data={"status": "pending"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "قيد المراجعة")
        # setUp فيه عملية معلّقة واحدة فقط → التبويب يعرض صفًا واحدًا
        self.assertContains(response, "1 عملية في هذا التبويب")

    def test_bank_review_deadline_skips_saudi_weekend(self):
        payment = Payment.objects.get(status=Payment.Status.PENDING)
        thursday = timezone.make_aware(datetime(2026, 8, 13, 10, 0))
        Payment.objects.filter(pk=payment.pk).update(created_at=thursday)
        payment.refresh_from_db()

        self.assertEqual(payment.bank_review_due_date.isoformat(), "2026-08-16")

    def test_platform_settings_page_shows_storage_overview(self):
        response = self.client.get(reverse("reports:platform_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الحد المجاني لكل مدرسة")
        self.assertContains(response, "إجمالي تخزين المنصة")

    def test_platform_plans_page_shows_pricing_policy_and_annual_savings(self):
        call_command("sync_default_pricing", "--deactivate-other-plans")

        response = self.client.get(reverse("reports:platform_plans_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "سياسة التسعير المعتمدة")
        self.assertContains(response, "ما يعادل")
        self.assertContains(response, "وفّر 498 ريال")
        self.assertContains(response, "399 ريال سنوياً")
        # Selling is by teacher count, so this page maintains the reference
        # anchors the calculator interpolates from — not cards the customer sees.
        self.assertContains(response, "الأسعار المرجعية التي تُغذّي الحاسبة")
        self.assertContains(response, "يُدخل فيها عدد المعلمين ويختار المدة")
        self.assertNotContains(response, "هذه هي الباقات نفسها الظاهرة في الصفحة الرئيسية")
        self.assertEqual(response.context["stats"]["active_count"], 10)
        self.assertEqual(response.context["stats"]["monthly_count"], 3)
        self.assertEqual(response.context["stats"]["capacity_count"], 3)
        self.assertEqual(len(response.context["renewal_catalog"]), 3)
        self.assertEqual(
            [len(group["options"]) for group in response.context["renewal_catalog"]],
            [3, 3, 3],
        )
        self.assertEqual(len(response.context["other_plans"]), 2)
        self.assertTrue(any(plan.price == 0 for plan in response.context["other_plans"]))
        self.assertTrue(any(not plan.is_active for plan in response.context["other_plans"]))

    def test_platform_detail_pages_render_decision_blocks(self):
        subscription_response = self.client.get(
            reverse("reports:platform_subscription_detail", args=[self.subscription.id])
        )
        payment = Payment.objects.filter(status=Payment.Status.PENDING).first()
        payment_response = self.client.get(
            reverse("reports:platform_payment_detail", args=[payment.id])
        )

        self.assertEqual(subscription_response.status_code, 200)
        self.assertContains(subscription_response, "ملخص مالي سريع")
        self.assertEqual(payment_response.status_code, 200)
        self.assertContains(payment_response, "ملخص القرار المالي")
