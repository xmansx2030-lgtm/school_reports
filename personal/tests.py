from datetime import date, timedelta
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import Report, School, SchoolMembership, SchoolSubscription, SubscriptionPlan, Teacher
from reports.services_data_rights import build_personal_data_export

from .models import PersonalEvidence, PersonalPayment, PersonalPlan, PersonalReport, PersonalSubscription, PersonalWorkspace
from .services import ensure_personal_subscription


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalWorkspaceJourneyTests(TestCase):
    def setUp(self):
        self.teacher = Teacher.objects.create_user(
            phone="0557000001", name="معلم أول", password="Personal#2026"  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(
            owner=self.teacher, school_name="مدرسة الأفق", principal_name="مدير الأفق"
        )

    def report_payload(self, **changes):
        data = {
            "title": "برنامج القراءة", "category": "نشاط", "report_date": "2026-09-24",
            "academic_year": "1447-1448", "description": "تنفيذ البرنامج في الصف",
            "goals": "رفع مهارة القراءة", "implementation": "ورش أسبوعية",
            "results": "تحسن ملحوظ", "recommendations": "المتابعة",
            "status": "complete",
        }
        data.update(changes)
        return data

    def test_valid_registration_activates_free_personal_subscription_without_school(self):
        response = self.client.post(reverse("personal:register"), {
            "name": "معلمة جديدة", "phone": "+966 55 700 0002",
            "gender": Teacher.Gender.FEMALE,
            "email": "new@example.com", "school_name": "مدرسة أخرى",
            "principal_name": "مديرة المدرسة", "password": "Personal#2026",
            "password_confirm": "Personal#2026", "accept_policies": "on",
        })
        self.assertRedirects(response, reverse("personal:dashboard"), fetch_redirect_response=False)
        new_user = Teacher.objects.get(phone="0557000002")
        self.assertEqual(new_user.gender, Teacher.Gender.FEMALE)
        self.assertEqual(new_user.personal_workspace.school_name, "مدرسة أخرى")
        self.assertContains(self.client.get(reverse("personal:dashboard")), "مساحة المعلمة المهنية")
        self.assertContains(self.client.get(reverse("personal:dashboard")), "مرحبًا بكِ")
        subscription = PersonalSubscription.objects.get(workspace__owner=new_user)
        self.assertEqual(subscription.plan.price, 0)
        self.assertTrue(subscription.is_active)
        self.assertTrue(subscription.is_current)
        self.assertFalse(SchoolMembership.objects.filter(teacher=new_user).exists())
        self.assertEqual(School.objects.count(), 0)
        self.assertEqual(SchoolSubscription.objects.count(), 0)

    def test_registration_requires_gender(self):
        response = self.client.post(reverse("personal:register"), {
            "name": "حساب بلا تحديد", "phone": "0557000004", "email": "gender@example.com",
            "school_name": "مدرسة مستقلة", "principal_name": "", "password": "Personal#2026",
            "password_confirm": "Personal#2026", "accept_policies": "on",
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("gender", response.context["form"].errors)
        self.assertFalse(Teacher.objects.filter(phone="0557000004").exists())

    def test_teacher_can_change_personal_gender_from_workspace_setup(self):
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("personal:setup"), {
            "school_name": self.workspace.school_name,
            "principal_name": self.workspace.principal_name,
            "school_stage": self.workspace.school_stage,
            "specialization": self.workspace.specialization,
            "email": self.teacher.email,
            "gender": Teacher.Gender.FEMALE,
        })

        self.assertRedirects(response, reverse("personal:dashboard"), fetch_redirect_response=False)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.gender, Teacher.Gender.FEMALE)
        self.assertContains(self.client.get(reverse("personal:dashboard")), "مساحة المعلمة المهنية")

    def test_registration_explains_existing_account_for_current_school_member(self):
        school = School.objects.create(name="مدرسة الانطلاق", code="school-member-test", stage="primary", gender="boys")
        plan = SubscriptionPlan.objects.create(name="اشتراك قائم", price=100, days_duration=365)
        SchoolSubscription.objects.create(school=school, plan=plan)
        SchoolMembership.objects.create(
            school=school, teacher=self.teacher, role_type=SchoolMembership.RoleType.TEACHER,
            is_active=True,
        )

        response = self.client.post(reverse("personal:register"), {
            "name": self.teacher.name, "phone": self.teacher.phone,
            "gender": Teacher.Gender.MALE,
            "email": "new-address@example.com", "school_name": school.name,
            "principal_name": "مدير المدرسة", "password": "Personal#2026",
            "password_confirm": "Personal#2026", "accept_policies": "on",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "هذا الرقم مرتبط بحساب مدرسي اشتراكه ساري")
        self.assertContains(response, reverse("reports:login"))
        self.assertFalse(PersonalWorkspace.objects.filter(owner__email="new-address@example.com").exists())

    def test_current_school_member_is_redirected_from_paid_personal_checkout(self):
        school = School.objects.create(name="مدرسة مشتركة", code="school-checkout-test", stage="primary", gender="boys")
        plan = SubscriptionPlan.objects.create(name="اشتراك المدرسة", price=100, days_duration=365)
        SchoolSubscription.objects.create(school=school, plan=plan)
        SchoolMembership.objects.create(
            school=school, teacher=self.teacher, role_type=SchoolMembership.RoleType.TEACHER,
            is_active=True,
        )
        personal_plan = PersonalPlan.objects.create(
            code="school_member_checkout_test", name="باقة شخصية", price="49.00", duration_days=30,
        )
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("personal:checkout_start", args=[personal_plan.pk]))

        self.assertRedirects(response, reverse("reports:home"), fetch_redirect_response=False)
        self.assertFalse(PersonalPayment.objects.exists())
        self.assertContains(self.client.get(reverse("reports:home")), "اشتراكها ساري")

    @patch("personal.billing.create_moyasar_invoice")
    def test_expired_school_membership_does_not_block_personal_checkout(self, create_invoice):
        school = School.objects.create(name="مدرسة منتهية", code="school-expired-test", stage="primary", gender="boys")
        plan = SubscriptionPlan.objects.create(name="اشتراك منتهٍ", price=100, days_duration=365)
        subscription = SchoolSubscription.objects.create(school=school, plan=plan)
        SchoolSubscription.objects.filter(pk=subscription.pk).update(
            end_date=timezone.localdate() - timedelta(days=1)
        )
        SchoolMembership.objects.create(
            school=school, teacher=self.teacher, role_type=SchoolMembership.RoleType.TEACHER,
            is_active=True,
        )
        self.teacher.email = "teacher@example.com"
        self.teacher.save(update_fields=["email"])
        personal_plan = PersonalPlan.objects.create(
            code="expired_school_checkout_test", name="باقة شخصية", price="49.00", duration_days=30,
        )
        create_invoice.return_value = {
            "id": "inv-expired-school-test", "url": "https://checkout.moyasar.com/invoices/test",
            "status": "initiated",
        }
        self.client.force_login(self.teacher)

        with override_settings(MOYASAR_ENABLED=True):
            response = self.client.post(reverse("personal:checkout_start", args=[personal_plan.pk]))

        self.assertRedirects(
            response, "https://checkout.moyasar.com/invoices/test?lang=ar", fetch_redirect_response=False
        )
        self.assertTrue(PersonalPayment.objects.filter(workspace=self.workspace).exists())

    @override_settings(MOYASAR_ENABLED=True)
    def test_personal_checkout_shows_only_moyasar_card_brands(self):
        self.teacher.email = "teacher@example.com"
        self.teacher.save(update_fields=["email"])
        plan = PersonalPlan.objects.create(
            code="personal_checkout_brand_test",
            name="باقة المعلم",
            price="49.00",
            duration_days=30,
        )
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("personal:checkout_start", args=[plan.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ميسر")
        self.assertNotContains(response, "مُيسّر")
        self.assertContains(response, "صفحة الدفع الآمنة")
        self.assertContains(response, "متابعة دفع 49 ريال بأمان")
        for asset in (
            "img/payment/mada.svg",
            "img/payment/visa.svg",
            "img/payment/mastercard.svg",
        ):
            self.assertContains(response, asset)
        self.assertNotContains(response, "tamara-wordmark-ar.png")
        self.assertNotContains(response, "apple-pay.svg")
        self.assertNotContains(response, "samsung-pay.svg")

    def test_personal_report_and_portfolio_do_not_enter_school_reporting(self):
        self.teacher.gender = Teacher.Gender.FEMALE
        self.teacher.save(update_fields=["gender"])
        self.client.force_login(self.teacher)
        response = self.client.post(reverse("personal:report_create"), self.report_payload())
        report = PersonalReport.objects.get(workspace=self.workspace)
        self.assertRedirects(response, reverse("personal:report_detail", args=[report.pk]), fetch_redirect_response=False)
        self.assertEqual(report.teacher_name, "معلم أول")
        self.assertEqual(report.school_name, "مدرسة الأفق")
        self.assertEqual(report.principal_name, "مدير الأفق")
        self.assertEqual(Report.objects.count(), 0)

        evidence_response = self.client.post(reverse("personal:evidence_create"), {
            "title": "نتيجة البرنامج", "academic_year": "1447-1448",
            "report": report.pk, "source_url": "https://example.org/evidence",
        })
        self.assertRedirects(evidence_response, reverse("personal:evidence"), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("personal:portfolio")), "برنامج القراءة")
        annual_print = self.client.get(reverse("personal:portfolio_print") + "?year=1447-1448")
        self.assertContains(annual_print, "ملف المعلمة الشخصي")
        self.assertContains(annual_print, "مدير الأفق")
        self.assertContains(annual_print, "ورش أسبوعية")
        self.assertContains(annual_print, "المتابعة")
        report_print = self.client.get(reverse("personal:report_print", args=[report.pk]))
        self.assertContains(report_print, "إعداد المعلمة")
        self.assertContains(report_print, "لا يمثل المستند اعتمادًا")
        exported = build_personal_data_export(self.teacher)["sections"]["personal_workspace"]
        self.assertEqual(exported["reports"][0]["title"], "برنامج القراءة")
        self.assertEqual(exported["evidence"][0]["title"], "نتيجة البرنامج")

        self.workspace.school_name = "مدرسة لاحقة"
        self.workspace.principal_name = "مدير لاحق"
        self.workspace.save()
        report.refresh_from_db()
        self.assertEqual(report.school_name, "مدرسة الأفق")
        self.assertEqual(report.principal_name, "مدير الأفق")
        updated_print = self.client.get(reverse("personal:report_print", args=[report.pk]))
        self.assertContains(updated_print, "مدير الأفق")
        self.assertNotContains(updated_print, "مدير لاحق")

    def test_another_user_cannot_read_mutate_or_attach_to_private_work(self):
        report = PersonalReport.objects.create(
            workspace=self.workspace, title="خاص", report_date=date.today(),
            academic_year="1447-1448", description="خاص بالمعلم", teacher_name="معلم أول",
            school_name="مدرسة الأفق", principal_name="مدير الأفق",
        )
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            evidence = PersonalEvidence.objects.create(
                workspace=self.workspace, report=report, title="ملف خاص",
                academic_year="1447-1448",
                file=SimpleUploadedFile("proof.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
                file_size=14,
            )
            self.client.force_login(self.teacher)
            download = self.client.get(reverse("personal:evidence_download", args=[evidence.pk]))
            self.assertEqual(download.status_code, 200)
            self.assertIn("attachment", download["Content-Disposition"])
            download.close()
            outsider = Teacher.objects.create_user(
                phone="0557000003", name="معلم ثان", password="Personal#2026"  # noqa: S106
            )
            PersonalWorkspace.objects.create(owner=outsider, school_name="مدرسة ثانية")
            self.client.force_login(outsider)
            for name in ("report_detail", "report_edit", "report_print"):
                self.assertEqual(self.client.get(reverse(f"personal:{name}", args=[report.pk])).status_code, 404)
            self.assertEqual(self.client.get(reverse("personal:evidence_download", args=[evidence.pk])).status_code, 404)
            self.assertEqual(self.client.post(reverse("personal:report_delete", args=[report.pk])).status_code, 404)
            self.assertEqual(self.client.post(reverse("personal:evidence_delete", args=[evidence.pk])).status_code, 404)
            forged = self.client.post(reverse("personal:evidence_create"), {
                "title": "مزور", "academic_year": "1447-1448",
                "report": report.pk, "source_url": "https://example.org/forged",
            })
            self.assertEqual(forged.status_code, 200)
            self.assertFalse(PersonalEvidence.objects.filter(workspace=outsider.personal_workspace).exists())
            self.client.force_login(self.teacher)
            storage = evidence.file.storage
            filename = evidence.file.name
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("personal:evidence_delete", args=[evidence.pk]))
            self.assertFalse(storage.exists(filename))

    def test_expired_school_membership_does_not_lock_existing_personal_workspace(self):
        school = School.objects.create(name="مدرسة الأفق", code="horizon", stage="primary", gender="boys")
        plan = SubscriptionPlan.objects.create(name="مدرسة منتهية", price=0, days_duration=30)
        subscription = SchoolSubscription.objects.create(
            school=school, plan=plan, start_date=timezone.localdate(),
            end_date=timezone.localdate(), is_active=True,
        )
        SchoolMembership.objects.create(
            school=school, teacher=self.teacher, role_type=SchoolMembership.RoleType.TEACHER,
            is_active=True,
        )
        SchoolSubscription.objects.filter(pk=subscription.pk).update(
            start_date=timezone.localdate() - timedelta(days=50),
            end_date=timezone.localdate() - timedelta(days=20),
        )
        response = self.client.post(reverse("reports:login"), {
            "phone": self.teacher.phone, "password": "Personal#2026",
        })
        self.assertRedirects(response, reverse("personal:dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("personal:dashboard")).status_code, 200)
        self.assertNotEqual(self.client.get(reverse("reports:home")).status_code, 200)

    def test_expired_school_manager_keeps_renewal_route_unless_personal_is_requested(self):
        school = School.objects.create(name="مدرسة المدير", code="manager-school", stage="primary", gender="boys")
        plan = SubscriptionPlan.objects.create(name="خطة منتهية", price=0, days_duration=30)
        subscription = SchoolSubscription.objects.create(
            school=school, plan=plan,
            start_date=timezone.localdate() - timedelta(days=50),
            end_date=timezone.localdate() - timedelta(days=20), is_active=True,
        )
        SchoolSubscription.objects.filter(pk=subscription.pk).update(
            start_date=timezone.localdate() - timedelta(days=50),
            end_date=timezone.localdate() - timedelta(days=20),
        )
        SchoolMembership.objects.create(
            school=school, teacher=self.teacher,
            role_type=SchoolMembership.RoleType.MANAGER, is_active=True,
        )
        credentials = {"phone": self.teacher.phone, "password": "Personal#2026"}
        response = self.client.post(reverse("reports:login"), credentials)
        self.assertRedirects(response, reverse("reports:subscription_expired"), fetch_redirect_response=False)

        self.client.logout()
        response = self.client.post(
            reverse("reports:login") + "?next=" + reverse("personal:dashboard"), credentials
        )
        self.assertRedirects(response, reverse("personal:dashboard"), fetch_redirect_response=False)

    def test_landing_promotes_personal_path_separately_from_school_pricing(self):
        response = self.client.get(reverse("reports:landing"))
        self.assertContains(response, reverse("personal:register"))
        self.assertContains(response, "حتى إن لم تشترك مدرسته")
        self.assertContains(response, "اسم مدرستك ومديرها للتعريف")

    def test_paid_landing_card_guides_anonymous_teacher_to_selected_plan(self):
        plan = PersonalPlan.objects.create(
            code="landing_paid_test", name="باقة الإنجاز", price="49.00", duration_days=90,
        )
        with override_settings(MOYASAR_ENABLED=True, LANDING_PRICING_CACHE_TTL_SECONDS=0):
            landing = self.client.get(reverse("reports:landing"))
        self.assertEqual(landing.status_code, 200)
        checkout_url = reverse("personal:checkout_start", args=[plan.pk])
        self.assertIn(checkout_url, landing.content.decode())

        checkout = self.client.get(checkout_url)
        register_url = reverse("personal:register") + f"?plan={plan.pk}"
        self.assertRedirects(checkout, register_url, fetch_redirect_response=False)
        registration = self.client.get(register_url)
        self.assertContains(registration, plan.name)
        self.assertContains(registration, "بعد إنشاء الحساب")
        self.assertContains(
            registration,
            reverse("reports:login") + f"?next={checkout_url}",
        )

    @patch("personal.billing.create_moyasar_invoice")
    def test_paid_registration_starts_moyasar_checkout_without_creating_school_records(self, create_invoice):
        plan = PersonalPlan.objects.create(
            code="teacher_plus_test", name="المعلم بلس", price="49.00", duration_days=90,
        )
        create_invoice.return_value = {
            "id": "inv-personal-test", "url": "https://checkout.moyasar.com/invoices/test",
            "status": "initiated",
        }
        with override_settings(MOYASAR_ENABLED=True, RATELIMIT_ENABLE=False):
            response = self.client.post(
                reverse("personal:register") + f"?plan={plan.pk}",
                {
                    "name": "معلم جديد", "phone": "0557000012", "gender": Teacher.Gender.MALE,
                    "email": "paid@example.com",
                    "school_name": "مدرسة مستقلة", "principal_name": "مدير المدرسة",
                    "password": "Tawtheeq!River_8042", "password_confirm": "Tawtheeq!River_8042",
                    "accept_policies": "on",
                },
            )
        self.assertRedirects(
            response, reverse("personal:checkout_start", args=[plan.pk]), fetch_redirect_response=False
        )
        with override_settings(MOYASAR_ENABLED=True, RATELIMIT_ENABLE=False):
            response = self.client.post(reverse("personal:checkout_start", args=[plan.pk]))
        self.assertRedirects(response, "https://checkout.moyasar.com/invoices/test?lang=ar", fetch_redirect_response=False)
        payment = PersonalPayment.objects.get(workspace__owner__phone="0557000012")
        self.assertEqual(payment.workspace.owner.gender, Teacher.Gender.MALE)
        self.assertEqual(payment.status, PersonalPayment.Status.PENDING)
        self.assertEqual(payment.gateway_invoice_id, "inv-personal-test")
        self.assertEqual(PersonalSubscription.objects.get(workspace=payment.workspace).plan.price, 0)
        self.assertFalse(SchoolMembership.objects.filter(teacher=payment.workspace.owner).exists())
        self.assertEqual(School.objects.count(), 0)
        kwargs = create_invoice.call_args.kwargs
        self.assertEqual(kwargs["amount"], plan.price)
        self.assertIn("personal_payment_ref", kwargs["metadata"])

    @patch("reports.utils.run_task_safe")
    def test_verified_paid_invoice_activates_once_and_queues_receipt(self, queue_task):
        plan = PersonalPlan.objects.create(
            code="teacher_pro_test", name="المعلم برو", price="75.00", duration_days=180,
        )
        subscription, _created = PersonalSubscription.objects.get_or_create(
            workspace=self.workspace,
            defaults={"plan": PersonalPlan.objects.get(code="personal_free")},
        )
        payment = PersonalPayment.objects.create(
            workspace=self.workspace, plan=plan, plan_name=plan.name, duration_days=180,
            amount=plan.price, customer_name=self.teacher.name, customer_email="teacher@example.com",
            school_name=self.workspace.school_name, gateway_invoice_id="inv-verified",
        )
        paid_invoice = {
            "id": "inv-verified", "status": "paid", "currency": "SAR", "amount": 7500,
            "metadata": {
                "personal_payment_ref": str(payment.pk),
                "personal_workspace_id": str(self.workspace.pk),
            },
            "payments": [{"id": "pay-verified", "status": "paid"}],
        }
        with self.captureOnCommitCallbacks(execute=True):
            from .billing import apply_paid_personal_invoice
            apply_paid_personal_invoice(payment.pk, paid_invoice)
        payment.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(payment.status, PersonalPayment.Status.PAID)
        self.assertIsNotNone(payment.activated_at)
        self.assertEqual(subscription.plan_id, plan.pk)
        self.assertEqual((subscription.end_date - subscription.start_date).days, 179)
        queue_task.assert_called_once()

        with self.captureOnCommitCallbacks(execute=True):
            apply_paid_personal_invoice(payment.pk, paid_invoice)
        self.assertEqual(queue_task.call_count, 2)
        subscription.refresh_from_db()
        self.assertEqual((subscription.end_date - subscription.start_date).days, 179)

    def test_paid_invoice_must_match_amount_currency_and_personal_workspace(self):
        plan = PersonalPlan.objects.create(
            code="teacher_validated_test", name="المعلم", price="49.00", duration_days=30,
        )
        payment = PersonalPayment.objects.create(
            workspace=self.workspace, plan=plan, plan_name=plan.name, duration_days=30,
            amount=plan.price, customer_name=self.teacher.name, customer_email="teacher@example.com",
            gateway_invoice_id="inv-validate",
        )
        from .billing import PersonalPaymentError, apply_paid_personal_invoice
        valid = {
            "id": "inv-validate", "status": "paid", "currency": "SAR", "amount": 4900,
            "metadata": {
                "personal_payment_ref": str(payment.pk),
                "personal_workspace_id": str(self.workspace.pk),
            },
        }
        for changes in ({"amount": 1}, {"currency": "USD"}, {"metadata": {}}):
            with self.subTest(changes=changes), self.assertRaises(PersonalPaymentError):
                apply_paid_personal_invoice(payment.pk, {**valid, **changes})
        payment.refresh_from_db()
        self.assertEqual(payment.status, PersonalPayment.Status.PENDING)

    @patch("reports.utils.run_task_safe")
    @patch("personal.billing.fetch_moyasar_invoice")
    def test_gateway_return_verifies_payment_then_exposes_invoice_only_to_owner(self, fetch_invoice, _queue_task):
        plan = PersonalPlan.objects.create(
            code="gateway_return_test", name="باقة التحقق", price="49.00", duration_days=30,
        )
        subscription = PersonalSubscription.objects.create(
            workspace=self.workspace, plan=PersonalPlan.objects.get(code="personal_free")
        )
        payment = PersonalPayment.objects.create(
            workspace=self.workspace, plan=plan, plan_name=plan.name,
            duration_days=plan.duration_days, amount=plan.price,
            customer_name=self.teacher.name, customer_email="teacher@example.com",
            gateway_invoice_id="inv-return-test",
        )
        invoice_url = reverse("personal:payment_invoice", args=[payment.pk])
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(invoice_url).status_code, 404)
        fetch_invoice.return_value = {
            "id": payment.gateway_invoice_id, "status": "paid", "currency": "SAR",
            "amount": 4900,
            "metadata": {
                "personal_payment_ref": str(payment.pk),
                "personal_workspace_id": str(self.workspace.pk),
            },
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.get(reverse("personal:moyasar_return", args=[payment.pk]))
        self.assertRedirects(response, reverse("personal:billing"), fetch_redirect_response=False)
        fetch_invoice.assert_called_once_with("inv-return-test")
        payment.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(payment.status, PersonalPayment.Status.PAID)
        self.assertTrue(subscription.is_current)
        self.assertEqual(subscription.plan_id, plan.pk)
        invoice = self.client.get(invoice_url)
        self.assertEqual(invoice.status_code, 200)
        self.assertEqual(invoice["Content-Type"], "application/pdf")
        self.assertIn("no-store", invoice["Cache-Control"])
        invoice.close()

        end_date = subscription.end_date
        self.client.logout()
        with self.captureOnCommitCallbacks(execute=True):
            callback = self.client.post(reverse("personal:moyasar_callback", args=[payment.pk]))
        self.assertEqual(callback.status_code, 200)
        self.assertTrue(callback.json()["activated"])
        subscription.refresh_from_db()
        self.assertEqual(subscription.end_date, end_date)

        outsider = Teacher.objects.create_user(
            phone="0557000040", name="معلم آخر", password="Personal#2026"  # noqa: S106
        )
        PersonalWorkspace.objects.create(owner=outsider, school_name="مدرسة أخرى")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(invoice_url).status_code, 404)

    @patch("personal.billing.fetch_moyasar_invoice")
    def test_failed_gateway_return_keeps_free_plan_and_disables_invoice(self, fetch_invoice):
        plan = PersonalPlan.objects.create(
            code="gateway_failure_test", name="باقة التجربة", price="49.00", duration_days=30,
        )
        subscription = PersonalSubscription.objects.create(
            workspace=self.workspace, plan=PersonalPlan.objects.get(code="personal_free")
        )
        payment = PersonalPayment.objects.create(
            workspace=self.workspace, plan=plan, plan_name=plan.name,
            duration_days=plan.duration_days, amount=plan.price,
            customer_name=self.teacher.name, customer_email="teacher@example.com",
            gateway_invoice_id="inv-failed-test",
        )
        fetch_invoice.return_value = {"id": payment.gateway_invoice_id, "status": "failed"}
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("personal:moyasar_return", args=[payment.pk]))
        self.assertRedirects(response, reverse("personal:billing"), fetch_redirect_response=False)
        payment.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(payment.status, PersonalPayment.Status.FAILED)
        self.assertFalse(payment.activated_at)
        self.assertEqual(subscription.plan.code, "personal_free")
        self.assertEqual(
            self.client.get(reverse("personal:payment_invoice", args=[payment.pk])).status_code,
            404,
        )

    @patch("reports.utils.run_task_safe")
    def test_renewal_keeps_paid_workspace_current_and_retains_remaining_days(self, _queue_task):
        from .billing import apply_paid_personal_invoice

        first_plan = PersonalPlan.objects.create(
            code="renewal_first_test", name="باقة أولى", price="49.00", duration_days=30,
        )
        next_plan = PersonalPlan.objects.create(
            code="renewal_next_test", name="باقة ثانية", price="75.00", duration_days=60,
        )
        subscription = PersonalSubscription.objects.create(workspace=self.workspace, plan=first_plan)
        today = timezone.localdate()
        old_start = today - timedelta(days=10)
        old_end = today + timedelta(days=19)
        PersonalSubscription.objects.filter(pk=subscription.pk).update(
            start_date=old_start, end_date=old_end
        )

        for index, plan in enumerate((first_plan, next_plan), start=1):
            payment = PersonalPayment.objects.create(
                workspace=self.workspace, plan=plan, plan_name=plan.name,
                duration_days=plan.duration_days, amount=plan.price,
                customer_name=self.teacher.name, customer_email="teacher@example.com",
                gateway_invoice_id=f"inv-renewal-{index}",
            )
            invoice = {
                "id": payment.gateway_invoice_id, "status": "paid", "currency": "SAR",
                "amount": int(plan.price * 100),
                "metadata": {
                    "personal_payment_ref": str(payment.pk),
                    "personal_workspace_id": str(self.workspace.pk),
                },
            }
            apply_paid_personal_invoice(payment.pk, invoice)
            subscription.refresh_from_db()
            self.assertTrue(subscription.is_current)
            self.assertEqual(subscription.plan_id, plan.pk)
            self.assertEqual(
                subscription.end_date,
                old_end + timedelta(days=first_plan.duration_days + (next_plan.duration_days if index == 2 else 0)),
            )
            self.assertEqual(subscription.start_date, old_start if index == 1 else today)

            apply_paid_personal_invoice(payment.pk, invoice)
            subscription.refresh_from_db()
            self.assertTrue(subscription.is_current)
            self.assertEqual(
                subscription.end_date,
                old_end + timedelta(days=first_plan.duration_days + (next_plan.duration_days if index == 2 else 0)),
            )


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalPlanManagementTests(TestCase):
    def setUp(self):
        self.owner = Teacher.objects.create_superuser(
            phone="0557000090", name="مالك المنصة", password="Personal#2026"  # noqa: S106
        )
        self.teacher = Teacher.objects.create_user(
            phone="0557000091", name="معلمة", password="Personal#2026"  # noqa: S106
        )
        self.plan = PersonalPlan.objects.get(code="personal_free")

    def _save_base_plan(self, *, price, duration_days, active=True, published=True):
        payload = {
            "name": self.plan.name, "description": self.plan.description,
            "price": price, "duration_days": duration_days,
            "max_reports": self.plan.max_reports, "max_evidence": self.plan.max_evidence,
            "storage_limit_mb": self.plan.storage_limit_mb,
            "display_order": self.plan.display_order,
        }
        if active:
            payload["is_active"] = "on"
        if published:
            payload["is_published"] = "on"
        self.client.force_login(self.owner)
        return self.client.post(
            reverse("reports:platform_personal_plan_edit", args=[self.plan.pk]), payload
        )

    def test_owner_can_price_base_plan_without_granting_it_to_new_accounts(self):
        old_workspace = PersonalWorkspace.objects.create(
            owner=self.teacher, school_name="مدرسة سابقة"
        )
        old_subscription = ensure_personal_subscription(old_workspace)
        self.assertTrue(old_subscription.is_current)

        response = self._save_base_plan(price="49.00", duration_days=30)
        self.assertRedirects(response, reverse("reports:platform_personal_plans"))
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.price, 49)
        self.assertEqual(self.plan.duration_days, 30)
        old_subscription.refresh_from_db()
        self.assertTrue(old_subscription.is_current)
        self.assertIsNone(old_subscription.end_date)

        newcomer = Teacher.objects.create_user(
            phone="0557000092", name="معلم جديد", password="Personal#2026"  # noqa: S106
        )
        workspace = PersonalWorkspace.objects.create(owner=newcomer, school_name="مدرسة جديدة")
        subscription = ensure_personal_subscription(workspace)
        self.assertFalse(subscription.is_current)
        self.assertIsNone(subscription.end_date)

        self.client.force_login(newcomer)
        self.assertContains(self.client.get(reverse("personal:billing")), "بانتظار التفعيل")
        self.assertRedirects(
            self.client.get(reverse("personal:report_create")),
            reverse("personal:dashboard"), fetch_redirect_response=False,
        )
        self.client.logout()
        with override_settings(MOYASAR_ENABLED=True, LANDING_PRICING_CACHE_TTL_SECONDS=0):
            landing = self.client.get(reverse("reports:landing"))
        self.assertContains(landing, "الباقة الأساسية")
        self.assertContains(landing, reverse("personal:checkout_start", args=[self.plan.pk]))

    def test_owner_can_limit_or_unpublish_free_base_plan(self):
        response = self._save_base_plan(price="0.00", duration_days=30, active=False, published=False)
        self.assertRedirects(response, reverse("reports:platform_personal_plans"))
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.is_active)
        self.assertFalse(self.plan.is_published)
        self.assertEqual(self.plan.duration_days, 30)

        workspace = PersonalWorkspace.objects.create(owner=self.teacher, school_name="مدرسة المعلمة")
        subscription = ensure_personal_subscription(workspace)
        self.assertFalse(subscription.is_current)
        self.assertIsNone(subscription.end_date)
        self.client.logout()
        with override_settings(LANDING_PRICING_CACHE_TTL_SECONDS=0):
            landing = self.client.get(reverse("reports:landing"))
        self.assertNotContains(landing, self.plan.name)

    def test_teacher_cannot_edit_teacher_plans(self):
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("reports:platform_personal_plan_edit", args=[self.plan.pk]),
            {"name": "غير مصرح"},
        )
        self.assertNotEqual(response.status_code, 200)
        self.plan.refresh_from_db()
        self.assertNotEqual(self.plan.name, "غير مصرح")

    def test_paid_base_waits_for_verified_gateway_payment(self):
        self._save_base_plan(price="49.00", duration_days=30)
        self.plan.refresh_from_db()
        self.client.logout()
        register_url = reverse("personal:register") + f"?plan={self.plan.pk}"
        checkout_url = reverse("personal:checkout_start", args=[self.plan.pk])
        response = self.client.post(register_url, {
            "name": "معلمة جديدة", "phone": "0557000093", "email": "new-paid@example.com",
            "gender": Teacher.Gender.FEMALE,
            "school_name": "مدرسة جديدة", "principal_name": "مديرة المدرسة",
            "password": "Personal#2026", "password_confirm": "Personal#2026",  # noqa: S106
            "accept_policies": "on",
        })
        self.assertRedirects(response, checkout_url, fetch_redirect_response=False)
        subscription = PersonalSubscription.objects.get(workspace__owner__phone="0557000093")
        self.assertFalse(subscription.is_current)
        self.assertFalse(subscription.is_active)

        with override_settings(MOYASAR_ENABLED=True), patch(
            "personal.billing.create_moyasar_invoice"
        ) as create_invoice, patch("personal.billing.fetch_moyasar_invoice") as fetch_invoice, patch(
            "reports.utils.run_task_safe"
        ):
            create_invoice.return_value = {
                "id": "inv-paid-base", "url": "https://checkout.moyasar.com/invoices/paid-base",
                "status": "initiated",
            }
            response = self.client.post(checkout_url)
            self.assertEqual(response.status_code, 302)
            payment = PersonalPayment.objects.get(workspace=subscription.workspace)
            self.assertEqual(payment.status, PersonalPayment.Status.PENDING)
            subscription.refresh_from_db()
            self.assertFalse(subscription.is_current)

            callback_url = reverse("personal:moyasar_callback", args=[payment.pk])
            fetch_invoice.return_value = {"status": "pending"}
            response = self.client.post(callback_url)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["activated"])
            subscription.refresh_from_db()
            self.assertFalse(subscription.is_current)

            fetch_invoice.return_value = {
                "id": payment.gateway_invoice_id, "status": "paid", "currency": "SAR",
                "amount": 4900,
                "metadata": {
                    "personal_payment_ref": str(payment.pk),
                    "personal_workspace_id": str(subscription.workspace_id),
                },
            }
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(callback_url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["activated"])

        subscription.refresh_from_db()
        payment.refresh_from_db()
        self.assertTrue(subscription.is_current)
        self.assertEqual(subscription.plan_id, self.plan.pk)
        self.assertEqual((subscription.end_date - subscription.start_date).days, 29)
        self.assertEqual(payment.status, PersonalPayment.Status.PAID)
