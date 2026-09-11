from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import AuditLog, CustomerComplaint, Teacher


@override_settings(ALLOWED_HOSTS=["testserver"])
class PlatformComplaintsTests(TestCase):
    def setUp(self):
        self.admin = Teacher.objects.create_superuser(
            phone="599700001",
            name="مدير النظام",
            password="pass",
        )
        self.regular_user = Teacher.objects.create_user(
            phone="599700002",
            name="مستخدم عادي",
            password="pass",
        )
        self.new_complaint = CustomerComplaint.objects.create(
            name="عميل أول",
            email="first@example.com",
            phone="0500000001",
            order_reference="ORDER-101",
            subject="تعذر التفعيل",
            message="تفاصيل الشكوى الأولى",
        )
        self.resolved_complaint = CustomerComplaint.objects.create(
            name="عميل ثان",
            email="second@example.com",
            phone="0500000002",
            order_reference="ORDER-202",
            subject="ملاحظة على الاشتراك",
            message="تفاصيل الشكوى الثانية",
            status=CustomerComplaint.Status.RESOLVED,
        )

    def test_list_requires_superuser(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(reverse("reports:platform_complaints_list"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("reports:platform_login"), response.url)

    def test_list_has_counts_search_and_status_filter(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("reports:platform_complaints_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tab_counts"]["all"], 2)
        self.assertEqual(response.context["tab_counts"]["new"], 1)
        self.assertEqual(response.context["tab_counts"]["resolved"], 1)
        self.assertContains(response, self.new_complaint.reference)
        self.assertContains(
            response,
            reverse("reports:platform_complaint_detail", args=[self.new_complaint.pk]),
        )

        filtered = self.client.get(
            reverse("reports:platform_complaints_list"),
            {"status": CustomerComplaint.Status.RESOLVED, "q": "ORDER-202"},
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(list(filtered.context["complaints"]), [self.resolved_complaint])
        self.assertEqual(filtered.context["tab_counts"]["all"], 1)

    def test_detail_updates_status_notes_and_writes_audit_history(self):
        self.client.force_login(self.admin)
        detail_url = reverse(
            "reports:platform_complaint_detail",
            args=[self.new_complaint.pk],
        )
        response = self.client.post(
            detail_url,
            {
                "status": CustomerComplaint.Status.RESOLVED,
                "internal_notes": "تم التواصل مع العميل وإغلاق السبب.",
            },
            REMOTE_ADDR="198.51.100.22",
            HTTP_X_FORWARDED_FOR="203.0.113.15",
        )

        self.assertRedirects(response, detail_url)
        self.new_complaint.refresh_from_db()
        self.assertEqual(
            self.new_complaint.status,
            CustomerComplaint.Status.RESOLVED,
        )
        self.assertIsNotNone(self.new_complaint.resolved_at)
        self.assertEqual(
            self.new_complaint.internal_notes,
            "تم التواصل مع العميل وإغلاق السبب.",
        )

        audit = AuditLog.objects.get(
            model_name="CustomerComplaint",
            object_id=self.new_complaint.pk,
        )
        self.assertEqual(audit.teacher, self.admin)
        self.assertEqual(audit.ip_address, "198.51.100.22")
        self.assertEqual(audit.changes["status"]["from"], CustomerComplaint.Status.NEW)
        self.assertEqual(
            audit.changes["status"]["to"],
            CustomerComplaint.Status.RESOLVED,
        )
        self.assertTrue(audit.changes["internal_notes_updated"])

        detail = self.client.get(detail_url)
        self.assertContains(detail, "سجل الإجراءات")
        self.assertContains(detail, "تمت المعالجة")
        self.assertContains(detail, "تم تحديث ملاحظات المعالجة")

    def test_platform_dashboard_links_to_pending_complaints(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("reports:platform_admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["complaints_pending"], 1)
        self.assertContains(
            response,
            reverse("reports:platform_complaints_list"),
        )
        self.assertContains(response, "شكاوى تحتاج متابعة")
