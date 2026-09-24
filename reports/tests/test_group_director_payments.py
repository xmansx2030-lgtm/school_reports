# -*- coding: utf-8 -*-
"""دفع المدير التنفيذي نيابةً عن إحدى مدارس مجموعته.

**لماذا فُتح ما كان مغلقاً.** الاشتراك يُحسب على سعة مدرسةٍ بعينها ومساحتها،
فكان الشراء محصوراً بمديرها. لكن في المجموعات التي تُدار مركزياً تكون الميزانية
بيد المدير التنفيذي أصلاً، ومطالبةُ كل مدير بالدفع من جيبه تعطيلٌ لا انضباط.

**والخطر الذي يُحرَس هنا** ليس أن يعجز التنفيذي عن الدفع، بل أن يدفع فلا يعلم
مديرُ المدرسة — فيشتري البند نفسه مرة ثانية. ولذلك تُختبر ثلاث ضمانات معاً: أن
السجل يحمل صفة الدافع، وأن المدير يُشعَر فور إنشاء الطلب لا عند اعتماده فقط،
وأن صفحته تحمل شارةً دائمة بمن يدفع عنها.

ويُحرَس العزل في الاتجاهين: لا يدفع عن مدرسةٍ خارج مجموعته، ولا يفتح تمريرُ
معرّفٍ عشوائي باباً على مدرسةٍ لا يقودها.
"""
from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    Notification,
    Payment,
    School,
    SchoolGroup,
    SchoolGroupMembership,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.tests.billing_test_helpers import valid_receipt


def _receipt():
    return valid_receipt()


@override_settings(ALLOWED_HOSTS=["testserver"])
class GroupDirectorPaysOnBehalfTests(TestCase):
    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(
            name="باقة سنوية", price=1200, days_duration=365, max_teachers=25
        )
        self.group = SchoolGroup.objects.create(name="مجموعة الأمل", code="amal")
        self.other_group = SchoolGroup.objects.create(name="مجموعة أخرى", code="other")

        self.school = School.objects.create(
            name="مدرسة المجموعة", code="in-group", group=self.group
        )
        SchoolSubscription.objects.create(school=self.school, plan=self.plan)

        self.outsider = School.objects.create(
            name="مدرسة خارجية", code="outsider", group=self.other_group
        )
        SchoolSubscription.objects.create(school=self.outsider, plan=self.plan)

        self.manager = Teacher.objects.create_user(
            phone="500330011", name="مدير المدرسة", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

        self.director = Teacher.objects.create_user(
            phone="500330022", name="المدير التنفيذي", password="pass"
        )
        SchoolGroupMembership.objects.create(
            group=self.group,
            user=self.director,
            role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
        )

        self.page = reverse("reports:my_subscription")
        self.pay = reverse("reports:payment_create")

    def _order(self, school, **extra):
        query = {"school": school.pk} if school is not None else {}
        page = self.client.get(self.page, query)
        submission_key = (
            page.context["checkout_submission_key"]
            if page.status_code == 200
            else "invalid-out-of-scope-key"
        )
        data = {
            "unified": "1",
            "include_subscription": "1",
            "plan_id": self.plan.pk,
            "checkout_submission_key": submission_key,
            "receipt_image": _receipt(),
        }
        if school is not None:
            data["on_behalf_school"] = school.pk
        data.update(extra)
        return self.client.post(self.pay, data)

    # ── فتح الصفحة ─────────────────────────────────────────────────
    def test_the_director_opens_a_group_school_s_purchase_page(self):
        self.client.force_login(self.director)
        response = self.client.get(self.page, {"school": self.school.pk})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["acting_on_behalf"])
        self.assertEqual(response.context["school"], self.school)
        self.assertContains(response, "أنت تدفع نيابةً عن")
        self.assertContains(
            response, f'name="on_behalf_school" value="{self.school.pk}"'
        )

    def test_a_school_outside_his_group_stays_shut(self):
        self.client.force_login(self.director)
        response = self.client.get(self.page, {"school": self.outsider.pk})

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("subscription", response["Location"])

    def test_a_plain_teacher_cannot_borrow_the_door(self):
        """المعرّف في المسار ليس صلاحية: تُقرأ من العضويات لا من الطلب."""
        stranger = Teacher.objects.create_user(
            phone="500330033", name="معلم", password="pass"
        )
        self.client.force_login(stranger)

        self.assertEqual(
            self.client.get(self.page, {"school": self.school.pk}).status_code, 302
        )
        self._order(self.school)
        self.assertFalse(Payment.objects.exists())

    # ── الدفع ──────────────────────────────────────────────────────
    def test_the_order_is_recorded_against_the_school_and_stamped(self):
        self.client.force_login(self.director)
        response = self._order(self.school)

        payment = Payment.objects.get()
        self.assertEqual(payment.school, self.school)
        self.assertEqual(payment.payer_kind, Payment.PayerKind.GROUP_DIRECTOR)
        self.assertEqual(payment.payer_group, self.group)
        self.assertEqual(payment.created_by, self.director)
        # يعود إلى سياق المدرسة نفسها، لا إلى صفحةٍ تردّه
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{self.page}?school={self.school.pk}")

    def test_he_cannot_charge_a_school_outside_his_group(self):
        self.client.force_login(self.director)
        self._order(self.outsider)

        self.assertFalse(Payment.objects.filter(school=self.outsider).exists())

    def test_the_manager_is_told_the_moment_the_order_is_created(self):
        """الإشعار عند الاعتماد وحده يترك فجوةً يشتري فيها المدير مرة ثانية."""
        self.client.force_login(self.director)
        self._order(self.school)

        notification = Notification.objects.filter(school=self.school).last()
        self.assertIsNotNone(notification)
        self.assertIn("المدير التنفيذي", notification.message)
        self.assertIn(self.group.name, notification.message)

    # ── ما يراه مدير المدرسة ───────────────────────────────────────
    def test_his_page_carries_a_standing_badge_naming_the_payer(self):
        self.client.force_login(self.director)
        self._order(self.school)

        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        response = self.client.get(self.page)

        self.assertFalse(response.context["acting_on_behalf"])
        self.assertIsNotNone(response.context["group_payer_payment"])
        self.assertContains(response, "اشتراك هذه المدرسة تدفعه")
        self.assertContains(response, self.group.name)
        self.assertContains(response, "دفعتها")

    def test_a_school_its_group_never_paid_for_gets_no_badge(self):
        """الشارة تقول «مجموعتك تدفع»، فلا تُعرض قبل أن تدفع فعلاً."""
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        response = self.client.get(self.page)

        self.assertIsNone(response.context["group_payer_payment"])
        self.assertNotContains(response, "اشتراك هذه المدرسة تدفعه")

    def test_the_manager_paying_for_himself_is_not_stamped_as_the_group(self):
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        self._order(None)

        payment = Payment.objects.get()
        self.assertEqual(payment.payer_kind, Payment.PayerKind.SCHOOL)
        self.assertIsNone(payment.payer_group)

    def test_a_manager_who_also_leads_the_group_pays_as_its_manager(self):
        """الصفة الأقرب تغلب: من يدير المدرسة لا يدفع «نيابةً» عنها.

        قيادة المجموعة واحدة (``group`` فريد في ``SchoolGroupMembership``)،
        فتُنقَل إلى مدير المدرسة بدل إضافة قائدٍ ثانٍ.
        """
        SchoolGroupMembership.objects.filter(group=self.group).delete()
        SchoolGroupMembership.objects.create(
            group=self.group,
            user=self.manager,
            role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
        )
        self.client.force_login(self.manager)
        self._order(self.school)

        payment = Payment.objects.get()
        self.assertEqual(payment.payer_kind, Payment.PayerKind.SCHOOL)
        self.assertIsNone(payment.payer_group)
