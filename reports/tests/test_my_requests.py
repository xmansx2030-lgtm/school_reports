from pathlib import Path
from urllib.parse import quote

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import School, SchoolMembership, SchoolSubscription, SubscriptionPlan, Teacher, Ticket


@override_settings(ALLOWED_HOSTS=["testserver"])
class MyRequestsViewTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Test School", code="test-school")
        plan = SubscriptionPlan.objects.create(
            name="Test Plan",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.user = Teacher.objects.create_user(
            phone="500000201",
            name="Teacher User",
            password="pass",        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

    def test_my_requests_filters_q_status_and_excludes_platform_tickets(self):
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="الشبكة الداخلية - مكتمل",
            body="الطلب المطابق الذي يجب أن يظهر.",
            status=Ticket.Status.DONE,
        )
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="الشبكة الداخلية - جديد",
            body="يطابق البحث فقط لكن ليس الحالة.",
            status=Ticket.Status.OPEN,
        )
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=True,
            title="الشبكة الداخلية - دعم منصي",
            body="يطابق البحث والحالة لكنه ليس من طلباتي المدرسية.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {"q": "الشبكة الداخلية", "status": Ticket.Status.DONE},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الشبكة الداخلية - مكتمل")
        self.assertNotContains(response, "الشبكة الداخلية - جديد")
        self.assertNotContains(response, "الشبكة الداخلية - دعم منصي")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

    def test_my_requests_filters_do_not_escape_active_school_scope(self):
        other_school = School.objects.create(name="Second School", code="second-school")
        plan = SubscriptionPlan.objects.create(
            name="Second Plan",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=other_school, plan=plan)
        SchoolMembership.objects.create(
            school=other_school,
            teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="الطلب المشترك - المدرسة الحالية",
            body="مطابق للنص والحالة داخل المدرسة النشطة.",
            status=Ticket.Status.DONE,
        )
        Ticket.objects.create(
            creator=self.user,
            school=other_school,
            is_platform=False,
            title="الطلب المشترك - مدرسة أخرى",
            body="مطابق للنص والحالة لكن خارج المدرسة النشطة.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {"q": "الطلب المشترك", "status": Ticket.Status.DONE},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الطلب المشترك - المدرسة الحالية")
        self.assertNotContains(response, "الطلب المشترك - مدرسة أخرى")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

    def test_my_requests_filters_do_not_escape_current_user_scope(self):
        other_user = Teacher.objects.create_user(
            phone="500000202",
            name="Other Teacher",
            password="pass",        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=other_user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="الطلب الشخصي - طلبي",
            body="مطابق للنص والحالة للمستخدم الحالي.",
            status=Ticket.Status.DONE,
        )
        Ticket.objects.create(
            creator=other_user,
            school=self.school,
            is_platform=False,
            title="الطلب الشخصي - طلب مستخدم آخر",
            body="مطابق للنص والحالة لكنه يخص مستخدمًا آخر.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {"q": "الطلب الشخصي", "status": Ticket.Status.DONE},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الطلب الشخصي - طلبي")
        self.assertNotContains(response, "الطلب الشخصي - طلب مستخدم آخر")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

    def test_my_requests_status_filter_excludes_q_matches_with_other_status(self):
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="طلب الشبكة - مكتمل",
            body="هذا الطلب يجب أن يظهر مع status=done.",
            status=Ticket.Status.DONE,
        )
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="طلب الشبكة - جديد",
            body="هذا الطلب يطابق q فقط لكن يجب استبعاده بسبب الحالة.",
            status=Ticket.Status.OPEN,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {"q": "طلب الشبكة", "status": Ticket.Status.DONE},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "طلب الشبكة - مكتمل")
        self.assertNotContains(response, "طلب الشبكة - جديد")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

    def test_my_requests_invalid_status_is_ignored_without_breaking_page(self):
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="الطلب الثابت - مفتوح",
            body="يجب أن يبقى ظاهرًا مع status غير صالح.",
            status=Ticket.Status.OPEN,
        )
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="الطلب الثابت - مكتمل",
            body="يجب أن يبقى ظاهرًا أيضًا مع status غير صالح.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {"q": "الطلب الثابت", "status": "bad-status"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الطلب الثابت - مفتوح")
        self.assertContains(response, "الطلب الثابت - مكتمل")
        self.assertEqual(response.context["page_obj"].paginator.count, 2)

    def test_my_requests_empty_status_is_ignored_without_breaking_page(self):
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="الطلب الفارغ - مفتوح",
            body="يجب أن يبقى ظاهرًا مع status فارغ.",
            status=Ticket.Status.OPEN,
        )
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="الطلب الفارغ - مرفوض",
            body="يجب أن يبقى ظاهرًا أيضًا مع status فارغ.",
            status=Ticket.Status.REJECTED,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {"q": "الطلب الفارغ", "status": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الطلب الفارغ - مفتوح")
        self.assertContains(response, "الطلب الفارغ - مرفوض")
        self.assertEqual(response.context["page_obj"].paginator.count, 2)

    def test_my_requests_non_numeric_page_returns_first_page_stably(self):
        for index in range(13):
            Ticket.objects.create(
                creator=self.user,
                school=self.school,
                is_platform=False,
                title=f"ترقيم الطلبات - مطابق {index}",
                body="طلب مطابق لاختبار paginator مع page غير رقمية.",
                status=Ticket.Status.DONE,
            )

        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="ترقيم الطلبات - حالة مختلفة",
            body="يجب استبعاده لأن الحالة مختلفة.",
            status=Ticket.Status.OPEN,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {
                "q": "ترقيم الطلبات",
                "status": Ticket.Status.DONE,
                "order": "created_at",
                "page": "abc",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].number, 1)
        self.assertEqual(response.context["page_obj"].paginator.count, 13)

    def test_my_requests_too_high_page_returns_last_page_stably(self):
        for index in range(13):
            Ticket.objects.create(
                creator=self.user,
                school=self.school,
                is_platform=False,
                title=f"الصفحة الأخيرة - مطابق {index}",
                body="طلب مطابق لاختبار paginator مع صفحة أعلى من آخر صفحة.",
                status=Ticket.Status.DONE,
            )

        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="الصفحة الأخيرة - حالة مختلفة",
            body="يجب استبعاده لأن الحالة مختلفة.",
            status=Ticket.Status.REJECTED,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {
                "q": "الصفحة الأخيرة",
                "status": Ticket.Status.DONE,
                "order": "created_at",
                "page": 999,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual(response.context["page_obj"].paginator.num_pages, 2)
        self.assertEqual(response.context["page_obj"].paginator.count, 13)

    def test_my_requests_pagination_links_keep_q_status_and_order(self):
        for index in range(13):
            Ticket.objects.create(
                creator=self.user,
                school=self.school,
                is_platform=False,
                title=f"pager-test request {index}",
                body="Matching request for pagination link preservation.",
                status=Ticket.Status.DONE,
            )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {
                "q": "pager-test",
                "status": Ticket.Status.DONE,
                "order": "created_at",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].number, 1)
        self.assertContains(
            response,
            "?q=pager-test&status=done&order=created_at&view=list&page=2",
        )

    def test_my_requests_pagination_links_keep_current_view_mode(self):
        for index in range(13):
            Ticket.objects.create(
                creator=self.user,
                school=self.school,
                is_platform=False,
                title=f"view-mode request {index}",
                body="Matching request for pagination view preservation.",
                status=Ticket.Status.DONE,
            )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {
                "q": "view-mode",
                "status": Ticket.Status.DONE,
                "order": "created_at",
                "view": "table",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].number, 1)
        self.assertContains(
            response,
            "?q=view-mode&status=done&order=created_at&view=table&page=2",
        )

    def test_my_requests_clear_button_resets_filters_and_keeps_current_view(self):
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="زر المسح - طلب مطابق",
            body="طلب لاختبار رابط زر المسح.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {
                "q": "زر المسح",
                "status": Ticket.Status.DONE,
                "order": "created_at",
                "view": "table",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '?view=table"')
        self.assertNotContains(response, '?q=زر المسح&status=done&order=created_at&view=table"')

    def test_my_requests_clear_button_is_hidden_without_active_filters_or_order(self):
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="بدون فلاتر - طلب",
            body="طلب لاختبار عدم ظهور زر المسح.",
            status=Ticket.Status.OPEN,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(reverse("reports:my_requests"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, ">مسح<", html=False)

    def test_my_requests_clear_button_appears_with_order_only(self):
        Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="ترتيب فقط - طلب",
            body="طلب لاختبار ظهور زر المسح مع order فقط.",
            status=Ticket.Status.OPEN,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {"order": "created_at"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مسح")
        # المقصود أن زر المسح يعود إلى العرض بلا مرشِّحات. وقائمةُ أصنافه
        # تفصيلٌ عرضي — تثبيتُها حرفياً كان يُفشل الاختبار عند أي تغيير تنسيقي
        # لا علاقة له بالسلوك.
        self.assertContains(response, 'href="?view=list"')

    def test_my_requests_invalid_order_falls_back_to_default_safely(self):
        older_ticket = Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="ترتيب غير صالح - أقدم",
            body="طلب أقدم لاختبار الرجوع للترتيب الافتراضي.",
            status=Ticket.Status.OPEN,
        )
        newer_ticket = Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="ترتيب غير صالح - أحدث",
            body="طلب أحدث لاختبار الرجوع للترتيب الافتراضي.",
            status=Ticket.Status.OPEN,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {"q": "ترتيب غير صالح", "order": "bad-order"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ترتيب غير صالح - أقدم")
        self.assertContains(response, "ترتيب غير صالح - أحدث")
        self.assertEqual(response.context["page_obj"].paginator.count, 2)
        self.assertEqual(response.context["tickets"].object_list[0].pk, newer_ticket.pk)
        self.assertEqual(response.context["tickets"].object_list[1].pk, older_ticket.pk)

    def test_my_requests_links_urlencode_arabic_q_in_view_and_pagination(self):
        raw_q = "طلب عربي & خاص"
        encoded_q = quote(raw_q, safe="")

        for index in range(13):
            Ticket.objects.create(
                creator=self.user,
                school=self.school,
                is_platform=False,
                title=f"{raw_q} {index}",
                body="طلب مطابق لاختبار ترميز q في الروابط.",
                status=Ticket.Status.DONE,
            )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:my_requests"),
            {
                "q": raw_q,
                "status": Ticket.Status.DONE,
                "order": "created_at",
                "view": "table",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"?q={encoded_q}&status=done&order=created_at&view=list",
        )
        self.assertContains(
            response,
            f"?q={encoded_q}&status=done&order=created_at&view=table&page=2",
        )

    def test_list_pilot_keeps_status_and_detail_in_card_and_table_modes(self):
        ticket = Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="طلب اختبار الواجهة",
            body="محتوى الطلب",
            status=Ticket.Status.IN_PROGRESS,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        for view_mode in ("list", "table"):
            with self.subTest(view_mode=view_mode):
                response = self.client.get(
                    reverse("reports:my_requests"), {"view": view_mode}
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "طلب اختبار الواجهة")
                self.assertContains(response, "قيد المعالجة")
                self.assertContains(
                    response, reverse("reports:ticket_detail", args=[ticket.pk])
                )
                self.assertContains(response, "css/my-requests.css")
                template_path = (
                    Path(__file__).resolve().parents[1]
                    / "templates"
                    / "reports"
                    / "my_requests.html"
                )
                self.assertNotIn("<style", template_path.read_text(encoding="utf-8"))

        table_response = self.client.get(
            reverse("reports:my_requests"), {"view": "table"}
        )
        self.assertContains(table_response, 'class="requests-table-wrap"')
        self.assertContains(table_response, 'class="requests-table-mobile requests-cards"')

    def test_list_pilot_distinguishes_empty_results_from_no_requests(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        empty = self.client.get(reverse("reports:my_requests"))
        self.assertContains(empty, "لا توجد طلبات بعد")
        self.assertContains(empty, reverse("reports:request_create"))

        filtered = self.client.get(
            reverse("reports:my_requests"), {"q": "مفقود", "view": "table"}
        )
        self.assertContains(filtered, "لا توجد طلبات مطابقة")
        self.assertContains(filtered, 'href="?view=table"')
        self.assertNotContains(filtered, 'class="twq-table requests-table"')

    def test_request_status_has_same_semantic_tone_in_list_and_detail(self):
        ticket = Ticket.objects.create(
            creator=self.user,
            school=self.school,
            is_platform=False,
            title="طلب توحيد الحالة",
            body="تفاصيل الطلب",
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        cases = (
            (Ticket.Status.OPEN, "info", "جديد"),
            (Ticket.Status.IN_PROGRESS, "warning", "قيد المعالجة"),
            (Ticket.Status.DONE, "success", "مكتمل"),
            (Ticket.Status.REJECTED, "danger", "مرفوض"),
        )
        for status, tone, label in cases:
            ticket.status = status
            ticket.save(update_fields=["status"])
            for view_mode in ("list", "table"):
                with self.subTest(status=status, view=view_mode):
                    response = self.client.get(
                        reverse("reports:my_requests"), {"view": view_mode}
                    )
                    self.assertContains(response, f"twq-status twq-status--{tone}")
                    self.assertContains(response, label)

            with self.subTest(status=status, view="detail"):
                response = self.client.get(
                    reverse("reports:ticket_detail", args=[ticket.pk])
                )
                self.assertContains(response, f"twq-status twq-status--{tone}")
                self.assertContains(response, label)
