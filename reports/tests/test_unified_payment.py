
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    ArchiveStorageOption,
    Payment,
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


def _png_bytes():
    # أصغر PNG صالح (1x1) لتمرير مدقّق الصور
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000154a24f6f0000000049454e44ae426082"
    )


def _receipt():
    return SimpleUploadedFile("receipt.png", _png_bytes(), content_type="image/png")


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class UnifiedPaymentTests(TestCase):
    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(
            name="باقة سنوية", price=1200, days_duration=365, max_teachers=0
        )
        self.school = School.objects.create(name="مدرسة الاختبار", code="unified-school")
        self.subscription = SchoolSubscription.objects.create(
            school=self.school, plan=self.plan
        )
        self.manager = Teacher.objects.create_user(
            phone="500111222", name="مدير", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.storage_option = ArchiveStorageOption.objects.create(
            storage_gb=50, price=99, is_active=True
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def _post(self, data):
        data.setdefault("receipt_image", _receipt())
        return self.client.post(reverse("reports:payment_create"), data, follow=False)

    def test_requires_at_least_one_item(self):
        resp = self._post({"unified": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Payment.objects.filter(school=self.school).count(), 0)

    def test_subscription_plus_addon_one_receipt(self):
        resp = self._post({
            "unified": "1",
            "include_subscription": "1",
            "plan_id": str(self.plan.id),
            "include_archive_addon": "1",
        })
        self.assertEqual(resp.status_code, 302)
        payments = Payment.objects.filter(school=self.school).order_by("purpose")
        self.assertEqual(payments.count(), 2)
        purposes = set(payments.values_list("purpose", flat=True))
        self.assertEqual(
            purposes,
            {Payment.Purpose.SUBSCRIPTION, Payment.Purpose.ARCHIVE_ADDON},
        )
        # كل السجلات قيد المراجعة ولها نفس صورة الإيصال (ملف مشترك واحد)
        self.assertTrue(all(p.status == Payment.Status.PENDING for p in payments))
        receipt_names = {p.receipt_image.name for p in payments}
        self.assertEqual(len(receipt_names), 1)

    def test_bank_transfer_sets_pending_status_and_one_business_day_notice(self):
        response = self.client.post(
            reverse("reports:payment_create"),
            {
                "unified": "1",
                "include_subscription": "1",
                "plan_id": str(self.plan.id),
                "receipt_image": _receipt(),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        message_text = " ".join(
            str(message) for message in get_messages(response.wsgi_request)
        )
        self.assertIn("خلال يوم عمل واحد كحد أقصى", message_text)
        payment = Payment.objects.get(school=self.school)
        self.assertEqual(payment.payment_method, Payment.Method.BANK_TRANSFER)
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_storage_can_be_bought_without_the_archive_addon(self):
        """Storage is its own product. Requiring the yearly-archive add-on first
        forced schools that only needed room for report photos to buy a product
        they did not want."""
        resp = self._post({
            "unified": "1",
            "include_archive_storage": "1",
            "archive_storage_option_id": str(self.storage_option.id),
        })

        self.assertEqual(resp.status_code, 302)
        payment = Payment.objects.get(
            school=self.school, purpose=Payment.Purpose.WORK_STORAGE
        )
        self.assertEqual(payment.amount, self.storage_option.price)
        self.assertEqual(payment.archive_storage_gb, self.storage_option.storage_gb)

    @override_settings(RATELIMIT_ENABLE=False)
    def test_inactive_plan_cannot_be_submitted_manually(self):
        inactive_plan = SubscriptionPlan.objects.create(
            name="باقة متوقفة",
            price=250,
            days_duration=90,
            max_teachers=10,
            is_active=False,
        )

        response = self.client.post(
            reverse("reports:payment_create"),
            {
                "unified": "1",
                "include_subscription": "1",
                "plan_id": str(inactive_plan.id),
                "receipt_image": _receipt(),
            },
            follow=False,
            REMOTE_ADDR="127.0.0.2",
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Payment.objects.filter(
                school=self.school,
                requested_plan=inactive_plan,
            ).exists()
        )

    def test_storage_allowed_with_active_addon(self):
        SchoolArchiveAddon.objects.create(
            school=self.school,
            is_enabled=True,
            start_date=timezone.localdate(),
            storage_limit_gb=50,
        )
        resp = self._post({
            "unified": "1",
            "include_archive_storage": "1",
            "archive_storage_option_id": str(self.storage_option.id),
        })
        self.assertEqual(resp.status_code, 302)
        p = Payment.objects.get(
            school=self.school, purpose=Payment.Purpose.WORK_STORAGE
        )
        self.assertEqual(p.archive_storage_gb, 50)
        self.assertEqual(int(p.amount), 99)

    def test_multi_item_order_shares_batch_ref(self):
        self._post({
            "unified": "1",
            "include_subscription": "1",
            "plan_id": str(self.plan.id),
            "include_archive_addon": "1",
        })
        refs = set(
            Payment.objects.filter(school=self.school).values_list("batch_ref", flat=True)
        )
        self.assertEqual(len(refs), 1)
        self.assertTrue(next(iter(refs)))  # non-empty

    def test_subscription_page_shows_only_published_paid_renewal_plans(self):
        published_plan = SubscriptionPlan.objects.create(
            name="باقة 25 مستخدم | 6 أشهر",
            price=699,
            days_duration=180,
            max_teachers=25,
            description="تشغيل كامل للمدرسة\nدعم فني",
        )
        SubscriptionPlan.objects.create(
            name="باقة قديمة غير منشورة",
            price=300,
            days_duration=90,
            max_teachers=10,
            is_active=False,
        )
        SubscriptionPlan.objects.create(
            name="التجربة المجانية",
            price=0,
            days_duration=14,
            max_teachers=5,
        )

        response = self.client.get(reverse("reports:my_subscription"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, published_plan.name)
        self.assertNotContains(response, "باقة قديمة غير منشورة")
        self.assertNotContains(response, "التجربة المجانية")
        self.assertContains(response, 'id="schoolSubscriptionPlans"')
        self.assertContains(response, 'id="archiveSubscriptionService"')
        self.assertContains(response, 'id="storageService"')
        self.assertContains(response, "زيادة مساحة عمل المدرسة")
        # المساحتان تُشتريان من بندين منفصلين، لأنهما تُفرضان بحدّين منفصلين.
        self.assertContains(response, 'id="archiveSpaceService"')
        self.assertContains(response, "زيادة مساحة الأرشفة السنوية")
        self.assertContains(response, 'id="orderEmptyState"')
        self.assertContains(response, 'data-summary-for="subscription"')
        self.assertContains(response, 'data-summary-for="addon"')
        self.assertContains(response, 'data-summary-for="storage"')
        self.assertContains(response, 'data-summary-for="archiveSpace"')
        self.assertContains(
            response,
            'id="submitBtn" disabled aria-disabled="true"',
        )
        self.assertContains(
            response,
            'src="/static/js/subscription-checkout.js?v=20260821.1"',
        )
        self.assertContains(response, "document.readyState === 'loading'")
        self.assertContains(response, "initSubscriptionPage()")

        offered_ids = {
            option["plan"].id
            for group in response.context["renewal_catalog"]
            for option in group["options"]
        }
        self.assertEqual(offered_ids, {self.plan.id, published_plan.id})

    def test_all_renewal_plan_choices_are_rendered_without_collapsing_catalog(self):
        SubscriptionPlan.objects.create(
            name="باقة سنوية 50 مستخدم",
            price=1800,
            days_duration=365,
            max_teachers=50,
        )

        response = self.client.get(reverse("reports:my_subscription"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="renewal-catalog"')
        self.assertContains(response, "عدد مستخدمين غير محدود")
        self.assertContains(response, "حتى 50 مستخدم")
        self.assertNotContains(response, 'id="subBody"')


@override_settings(ALLOWED_HOSTS=["testserver"])
class BatchApprovalTests(TestCase):
    def setUp(self):
        self.admin = Teacher.objects.create_superuser(
            phone="599888777", name="Platform Admin", password="pass"
        )
        self.plan = SubscriptionPlan.objects.create(
            name="باقة", price=1200, days_duration=365, max_teachers=0
        )
        self.school = School.objects.create(name="مدرسة", code="batch-school")
        # طلب موحّد: اشتراك + أرشفة، قيد المراجعة، بنفس batch_ref
        self.p_sub = Payment.objects.create(
            school=self.school,
            requested_plan=self.plan,
            purpose=Payment.Purpose.SUBSCRIPTION,
            amount=1200,
            status=Payment.Status.PENDING,
            batch_ref="abc12345",
            created_by=self.admin,
        )
        self.p_addon = Payment.objects.create(
            school=self.school,
            purpose=Payment.Purpose.ARCHIVE_ADDON,
            amount=399,
            status=Payment.Status.PENDING,
            batch_ref="abc12345",
            created_by=self.admin,
        )
        self.client.force_login(self.admin)

    def test_approve_batch_activates_all_items(self):
        resp = self.client.post(
            reverse("reports:platform_payment_detail", args=[self.p_sub.id]),
            {"action": "approve_batch"},
        )
        self.assertEqual(resp.status_code, 302)
        self.p_sub.refresh_from_db()
        self.p_addon.refresh_from_db()
        self.assertEqual(self.p_sub.status, Payment.Status.APPROVED)
        self.assertEqual(self.p_addon.status, Payment.Status.APPROVED)
        # الاشتراك فُعّل + الأرشفة فُعّلت
        self.assertTrue(SchoolSubscription.objects.filter(school=self.school).exists())
        addon = SchoolArchiveAddon.objects.get(school=self.school)
        self.assertTrue(addon.is_active)

    def test_annual_leadership_plan_grants_included_archive_without_extra_payment(self):
        leadership_plan = SubscriptionPlan.objects.create(
            name="قيادة | سنوي",
            price=2990,
            days_duration=365,
            max_teachers=100,
            included_archive_storage_gb=50,
        )
        payment = Payment.objects.create(
            school=self.school,
            requested_plan=leadership_plan,
            purpose=Payment.Purpose.SUBSCRIPTION,
            amount=2990,
            status=Payment.Status.PENDING,
            created_by=self.admin,
        )

        response = self.client.post(
            reverse("reports:platform_payment_detail", args=[payment.id]),
            {"status": Payment.Status.APPROVED},
        )

        self.assertEqual(response.status_code, 302)
        subscription = SchoolSubscription.objects.get(school=self.school)
        addon = SchoolArchiveAddon.objects.get(school=self.school)
        self.assertEqual(subscription.plan, leadership_plan)
        self.assertTrue(addon.is_active)
        self.assertEqual(addon.storage_limit_gb, 50)
        self.assertEqual(addon.end_date, subscription.end_date)
