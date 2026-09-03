"""صقل تجربة مدير المدرسة: ما يُقرأ، وما يُقرأ صحيحاً.

هذه الوحدة أخت ``test_manager_experience`` لا بديلٌ عنها: تلك تحرس *ما يفعله*
المدير (الصلاحيات والمسارات والبيانات)، وهذه تحرس *ما يراه* — الحرف واللون
والتاريخ والصيغة.

هذه الاختبارات وُلدت من فحصٍ ميداني على المنصة الحيّة بحساب مدير مدرسة، فلا
تُثبّت سلوكاً افتُرض بل عيوباً رُئيت بالعين وقيست بالأداة:

* ‎«١٤٤٨ هـ هـ»‎ — علامة الحقبة مضاعَفة، لأن ثلاثة قوالب بنَت مُنسّق ‎Intl‎
  بيدها ثم ألحقت «هـ» يدوياً، و‎Intl‎ يُخرجها أصلاً ضمن النتيجة.
* تاريخ ميلادي في صندوق الطلبات وحده، بين شاشاتٍ كلّها هجرية.
* أرقامٌ بنسبة تباين ‎1.01‎ في قائمة ملف الأداء القيادي — أي نصٌّ غائب لا باهت.
* شعار المنصة مُسطَّح بـ‎brightness(0) invert(1)‎ فصار مربعاً أبيض بلا معالم.

القاعدة التي تحرسها: العيب الذي رآه المستخدم مرةً لا يعود بلا أن يسقط بناءٌ.
"""

from __future__ import annotations

import re
from pathlib import Path

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from django.urls import reverse

from reports.coverage import pending_documenters
from reports.models import (
    Assignment,
    Department,
    DepartmentMembership,
    Meeting,
    Notification,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    Ticket,
)
from reports.templatetags.arabic_tags import arabic_count
from reports.views.schools import (
    _build_school_dashboard_payload,
    _dashboard_period_start,
    _department_activity,
    _previous_period_window,
    _school_agenda,
    _trend,
)


def _source(relative_path: str) -> str:
    return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """يُسقط التعليقات قبل الفحص.

    التوثيق هنا يقتبس الخطأ ليشرحه — ولو فحصنا الملف كاملاً لأدان الشرحُ نفسَه.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", without_blocks, flags=re.MULTILINE)


# القوالب التي تعرض صدىً هجرياً تحت حقل تاريخٍ ميلادي.
HIJRI_ECHO_TEMPLATES = (
    "reports/templates/reports/add_report.html",
    "reports/templates/reports/edit_report.html",
    "reports/templates/reports/assignment_create.html",
)


class HijriEchoTests(SimpleTestCase):
    """صدى التاريخ الهجري: مُنسّقٌ واحد، وعلامة حقبةٍ واحدة."""

    def test_the_shared_helper_never_appends_its_own_era_marker(self):
        helper = _source("static/js/hijri-date.js")
        code = _code_only(helper)

        # ‎Intl‎ يُخرج «هـ» ضمن ‎formatToParts‎ كجزء ‎era‎؛ فإلحاقها نصاً يضاعفها.
        self.assertNotIn('+ " هـ"', code)
        self.assertNotIn("+ ' هـ'", code)
        self.assertIn("islamic-umalqura", code)
        self.assertIn("global.TawtheeqHijri", code)

    def test_the_helper_matches_the_servers_latin_digits(self):
        # الخادم يكتب «1448/03/21» عبر hijri_utils؛ فلو أخرج المتصفّح «١٤٤٨»
        # لرأى المدير نظامَي ترقيمٍ لنفس التاريخ في الشاشة الواحدة.
        self.assertIn("nu-latn", _source("static/js/hijri-date.js"))

    def test_no_template_builds_its_own_hijri_formatter(self):
        for template_path in HIJRI_ECHO_TEMPLATES:
            with self.subTest(template=template_path):
                source = _source(template_path)
                self.assertNotIn("islamic-umalqura", source)
                self.assertIn("TawtheeqHijri", source)
                self.assertIn("js/hijri-date.js", source)

    def test_no_template_doubles_the_era_marker(self):
        doubled = re.compile(r"\.format\([^)]*\)\s*\+\s*['\"] هـ")
        for template_path in HIJRI_ECHO_TEMPLATES:
            with self.subTest(template=template_path):
                self.assertIsNone(doubled.search(_source(template_path)))

    def test_the_helper_loads_before_the_inline_script_that_calls_it(self):
        # ‎defer‎ يؤجّل التنفيذ إلى ما بعد تحليل الصفحة، والسكربت المضمّن يعمل
        # وقت التحليل — فلو أُجّل المساعد لَناداه المضمّن قبل وجوده.
        for template_path in HIJRI_ECHO_TEMPLATES:
            with self.subTest(template=template_path):
                source = _source(template_path)
                tag = re.search(r"<script[^>]*js/hijri-date\.js[^>]*>", source)
                self.assertIsNotNone(tag)
                self.assertNotIn("defer", tag.group(0))
                # نقيس أول *استدعاء* فعلي، لا أول ذكرٍ للاسم في تعليق.
                self.assertLess(tag.start(), source.index("window.TawtheeqHijri"))


class HijriDisplayConsistencyTests(SimpleTestCase):
    """التواريخ المعروضة هجرية في كل شاشات المدير — بلا استثناء منسي."""

    def test_the_request_inbox_shows_hijri_like_every_other_screen(self):
        source = _source("reports/templates/reports/tickets_inbox.html")

        self.assertIn("{% load hijri_tags %}", source)
        self.assertIn("{{ t.created_at|hijri }} هـ", source)
        # الميلادي يبقى في سمة ‎datetime‎ للآلة، لا في النص المقروء.
        self.assertNotIn('{{ t.created_at|date:"Y-m-d H:i" }}', source)
        self.assertIn("<time datetime=", source)


class ArabicCountAgreementTests(SimpleTestCase):
    """تمييز العدد: خمس صيغ، لا صيغتان.

    اللوحة كانت تكتب «3 عنصر يحتاج متابعة» و«1 طلبات مكتملة» — لأن ‎pluralize‎
    مصنوعٌ للغةٍ لها مفردٌ وجمع، والعربية تُفرد في ‎11‎ وتَجمع في ‎3‎.
    """

    ITEM = "عنصر,عنصران,عناصر,عنصراً"

    def test_it_follows_the_cldr_arabic_plural_rules(self):
        expected = {
            0: "لا عناصر",
            1: "عنصر واحد",
            2: "عنصران",
            3: "3 عناصر",
            10: "10 عناصر",
            11: "11 عنصراً",
            15: "15 عنصراً",
            99: "99 عنصراً",
            100: "100 عنصر",
            101: "101 عنصر",
            103: "103 عناصر",
            111: "111 عنصراً",
        }
        for count, text in expected.items():
            with self.subTest(count=count):
                self.assertEqual(arabic_count(count, self.ITEM), text)

    def test_a_missing_form_is_derived_rather_than_crashing(self):
        # القوالب لا يجب أن تنكسر لأن أحدهم مرّر صيغتين بدل أربع.
        self.assertEqual(arabic_count(2, "طلب"), "طلبان")
        self.assertEqual(arabic_count(5, "طلب,طلبان,طلبات"), "5 طلبات")

    def test_unreadable_values_degrade_instead_of_raising(self):
        for value in (None, "", "غير رقم", []):
            with self.subTest(value=value):
                self.assertIsInstance(arabic_count(value, self.ITEM), str)

    def test_the_dashboard_uses_the_filter_instead_of_a_frozen_word(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn("{% load arabic_tags %}", source)
        self.assertNotIn("<span>عنصر يحتاج متابعة</span>", source)
        self.assertNotIn("طلبات مكتملة من أصل", source)
        self.assertIn("|arabic_plural:", source)

    def test_live_updates_repaint_the_word_not_only_the_number(self):
        # الأرقام تتغيّر بتغيير الفترة بلا إعادة تحميل؛ فلو بقيت الكلمة ثابتة
        # لعاد الخطأ نفسه بعد أول نقرة — أي أن الإصلاح لا يصمد.
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn("js/arabic-count.js", source)
        self.assertIn('paintCount("managerAttentionLabel"', source)
        self.assertIn('paintCount("schoolTicketsDone"', source)

    def test_the_javascript_twin_states_the_same_cldr_boundaries(self):
        # النسختان تُقرآن معاً: إن انحرفت إحداهما اختلف الخادم عن المتصفّح.
        javascript = _source("static/js/arabic-count.js")
        for boundary in ("remainder >= 3 && remainder <= 10", "remainder >= 11 && remainder <= 99"):
            self.assertIn(boundary, javascript)
        self.assertIn("TawtheeqArabic", javascript)


class HeroPulseLabelTests(SimpleTestCase):
    """مؤشّرات الترويسة: عنوانٌ من كلمتين لا يُقصّ إلى كلمةٍ ونصف."""

    def test_the_label_can_neither_be_ellipsised_nor_clipped(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        rule = re.search(
            r"\.manager-hero__pulse span \{(.*?)\}", source, flags=re.DOTALL
        )
        self.assertIsNotNone(rule)
        body = rule.group(1)

        # ‎ellipsis‎ بترَ «ملفات اعتماد»، و‎line-clamp‎ قصَّ ذيل سطرها الثاني.
        self.assertNotIn("text-overflow", body)
        self.assertNotIn("line-clamp", body)
        self.assertNotIn("nowrap", body)
        self.assertNotIn("overflow: hidden", body)


def _contrast(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class CountBadgeContrastTests(SimpleTestCase):
    """شارة العدّ في الترويسة — تظهر في كل صفحة، فخطؤها يتكرر بعددها."""

    def test_the_badge_has_its_own_pair_instead_of_borrowing_danger(self):
        # ‎--danger‎ لونُ حدودٍ وأيقونات؛ حَمْلُ نصٍّ فوقه هو أصل الخطأ.
        shell = _source("static/css/app-shell.css")
        self.assertIn("var(--badge-count-bg", shell)
        self.assertIn("var(--badge-count-ink", shell)

    def test_both_themes_clear_wcag_aa_for_small_bold_text(self):
        pairs = {
            "light": ("#ffffff", "#c62828"),
            "dark": ("#3d0a0c", "#ff8c91"),
        }
        tokens = _source("static/css/tokens.css")
        dark = _source("static/css/dark-mode.css")

        for theme, (ink, background) in pairs.items():
            with self.subTest(theme=theme):
                source = tokens if theme == "light" else dark
                self.assertIn(f"--badge-count-bg: {background};", source)
                self.assertIn(f"--badge-count-ink: {ink};", source)
                self.assertGreaterEqual(_contrast(ink, background), 4.5)


class LeadershipPortfolioReadabilityTests(SimpleTestCase):
    """قائمة ملف الأداء القيادي: أرقامٌ تُرى، وشعارٌ له معالم."""

    def test_the_dark_layer_reaches_the_list_shell_not_only_the_workspace(self):
        css = _source("static/css/dark-mode.css")

        # ‎.lp-shell‎ يعرّف ‎--lp-ink‎ محلياً؛ فما لم تُعَد تعريفتُه على الغلاف
        # نفسه بقيت الأرقام ‎#17211c‎ فوق بطاقةٍ ‎#0d241d‎.
        self.assertIn('html[data-theme="dark"] .lp-shell', css)
        self.assertIn('html[data-theme="dark"] .lp-status', css)

    def test_the_brand_mark_is_not_flattened_into_a_white_square(self):
        # الشعار رقعةٌ خضراء عليها وثيقةٌ بيضاء وعلامةٌ ذهبية. و‎brightness(0)‎
        # ثم ‎invert(1)‎ يُبيّض الطبقات الثلاث معاً، فلا يبقى إلا مربّع أبيض.
        for template_path in (
            "reports/templates/reports/leadership_portfolio_list.html",
            "reports/templates/reports/leadership_portfolio_detail.html",
        ):
            with self.subTest(template=template_path):
                self.assertNotIn("brightness(0) invert(1)", _source(template_path))

    def test_person_names_are_isolated_from_the_surrounding_direction(self):
        # اسمٌ لاتيني داخل جملةٍ عربية يفقد موضعه بلا ‎<bdi>‎.
        self.assertIn(
            "<bdi>{{ portfolio.manager_name }}</bdi>",
            _source("reports/templates/reports/leadership_portfolio_detail.html"),
        )
        self.assertIn(
            "<bdi>{{ item.manager_name }}</bdi>",
            _source("reports/templates/reports/leadership_portfolio_list.html"),
        )


class DarkLayerCoverageTests(SimpleTestCase):
    """ما اكتُشف بالقياس على مسارات المدير الاثنين والثلاثين.

    كل بندٍ هنا كان رقماً مقيساً لا ظنّاً: ‎1.08‎ لرقم خطوة الاشتراك، ‎2.09‎
    لرقم خطوة المستلمين، ‎2.73‎ لشارة سجل الدخول، ‎2.81‎ لنصّ رافع الشواهد.
    والقاعدة الجامعة بينها واحدة: ملفٌّ أو قالبٌ كتب لونه بيده لوضعٍ فاتح،
    فقلبت الطبقةُ العامة خلفيتَه دون حبره.
    """

    def test_every_family_found_by_measurement_now_has_a_dark_rule(self):
        css = _source("static/css/dark-mode.css")
        for selector in (
            ".lp-shell",            # أرقام ملف الأداء القيادي — كانت 1.01
            ".subx-journey__number",  # رقم خطوة الاشتراك — كان 1.08
            ".recipient-step-number",  # رقم خطوة المستلمين — كان 2.09
            ".b-login",             # شارات سجل العمليات — كانت 2.73
            ".b-create",
            ".b-delete",
            ".inbox-wrap .dept-chip",  # شارة القسم — كانت 2.01
            ".inbox-wrap .s-done",
            ".ar-section-option small",  # وصف بند التقرير — كان 3.71
            ".ar-required-mark",    # شارة «مطلوب» — كانت 4.34
        ):
            with self.subTest(selector=selector):
                self.assertIn(f'html[data-theme="dark"] {selector}', css)

    def test_the_evidence_editor_finally_has_a_dark_layer(self):
        # كان هذا الملف بلا سطرٍ داكنٍ واحد، وهو محرّر الشواهد في أكثر
        # شاشةٍ يفتحها المدير — فيبقى مربّع المعاينة رقعةً بيضاء وسط الليل.
        css = _source("static/css/report-evidence.css")
        self.assertIn('html[data-theme="dark"] .report-evidence-editor', css)
        self.assertIn('html[data-theme="dark"] .ree-preview', css)
        self.assertIn('html[data-theme="dark"] .ree-placeholder', css)

    def test_extracted_styles_are_mirrored_instead_of_left_behind(self):
        # ‎extracted.css‎ يجمع ما انتُزع من سمات ‎style=‎ المتناثرة، وكان
        # أربعةَ عشرَ لوناً من سبعةَ عشرَ فيه دون الحدّ في الوضع الداكن.
        css = _source("static/css/dark-mode.css")
        self.assertIn('html[data-theme="dark"] .xs-add-report-1', css)
        self.assertIn('html[data-theme="dark"] .xs-send-circular-4', css)

    def test_the_circular_palette_is_redefined_for_dark(self):
        # القالب يعرّف لوحته على ‎:root‎؛ و‎html[data-theme="dark"]‎ أعلى
        # تخصيصاً منها فيسبقها — وهذا ما يجعل الإصلاح ممكناً بلا لمس القالب.
        css = _source("static/css/dark-mode.css")
        self.assertIn("--c-text-muted: #abc0b6;", css)
        self.assertIn("--c-primary: #42ca88;", css)


class HijriFilterBridgeTests(SimpleTestCase):
    """الجسر بين ما يُقرأ وما يُكتب.

    الجدول يعرض «1448/03/21 هـ»، والفلتر ‎<input type="date">‎ ميلادي. وكانت
    صفحة «إضافة تقرير» وحدها تبني الجسر بيدها، فبقي حيث كُتب ولم يبلغ الفلاتر
    — فيرى المدير تاريخاً هجرياً ثم يُطلب منه أن يبحث عنه بالميلادي.
    """

    def test_the_helper_wires_any_marked_date_input_on_its_own(self):
        helper = _source("static/js/hijri-date.js")
        self.assertIn('input[type="date"][data-hijri-echo]', helper)
        self.assertIn("DOMContentLoaded", helper)
        # حقلٌ يُضاف غداً يرث السلوك بسمةٍ واحدة، لا بنسخ الوصل.
        self.assertIn("wireAll", helper)

    def test_it_never_wires_the_same_field_twice(self):
        # ‎wireAll‎ قد يُنادى مرتين (تحميل ثم إدراج ديناميكي)، وصدىً مكرّر
        # تحت الحقل أسوأ من غيابه.
        self.assertIn("dataset.hijriWired", _source("static/js/hijri-date.js"))

    def test_the_reports_filter_carries_the_bridge(self):
        source = _source("reports/templates/reports/admin_reports.html")
        self.assertIn("js/hijri-date.js", source)
        self.assertEqual(source.count("data-hijri-echo"), 2)   # من تاريخ، إلى تاريخ
        # والجدول يبقى هجرياً كما كان.
        self.assertIn("{{ r.report_date|hijri }} هـ", source)

    def test_the_echo_is_styled_once_for_every_screen(self):
        css = _source("static/css/app-components.css")
        self.assertIn(".hijri-echo", css)
        self.assertIn(".hijri-echo:empty", css)


class TableActionNamingTests(SimpleTestCase):
    """أزرارٌ أيقونية في جدولٍ من عشرين صفاً.

    ‎title‎ وحده لا يظهر على اللمس إطلاقاً، و‎aria-label="حذف"‎ مكرَّراً عشرين
    مرة يجعل قارئ الشاشة يقول «حذف» بلا أن يقول: حذفُ مَن.
    """

    def test_report_row_actions_name_the_report_they_act_on(self):
        source = _source("reports/templates/reports/admin_reports.html")
        for label in (
            'aria-label="عرض تفاصيل تقرير:',
            'aria-label="طباعة تقرير:',
            'aria-label="تعديل تقرير:',
            'aria-label="مشاركة تقرير:',
            'aria-label="نقل تقرير إلى سلة المحذوفات:',
        ):
            with self.subTest(label=label):
                self.assertIn(label, source)

    def test_decorative_icons_are_hidden_from_the_accessibility_tree(self):
        # أيقونةٌ داخل زرٍّ مُسمّى تُقرأ مرتين إن لم تُخفَ.
        source = _source("reports/templates/reports/admin_reports.html")
        self.assertIn('<i class="fa-solid fa-trash" aria-hidden="true">', source)
        self.assertIn('<i class="fa-solid fa-eye" aria-hidden="true">', source)

    def test_staff_row_actions_name_the_person(self):
        source = _source("reports/templates/reports/manage_teachers.html")
        self.assertIn('aria-label="تعديل بيانات: {{ t.name|default:t.phone }}"', source)
        self.assertIn('aria-label="حذف: {{ t.name|default:t.phone }}"', source)
        self.assertNotIn('aria-label="حذف"', source)

    def test_delete_is_not_louder_than_the_action_beside_it(self):
        # الحذف نادرٌ لا رجعة فيه، والتعديل شائع. فمربّعٌ أحمر مصمت بجوار
        # التعديل يجعل الأخطر أسهلَ إصابةً بالإبهام.
        source = _source("reports/templates/reports/manage_teachers.html")
        self.assertIn(".admin-scope .btn-delete{background:transparent", source)
        self.assertNotIn(".admin-scope .btn-delete{background:#ef4444", source)


class SeatPressureTests(SimpleTestCase):
    """المقاعد تُقال قبل أن تُصطدم، لا بعدها."""

    def test_the_alert_sits_with_the_other_blockers_not_in_a_folded_panel(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn("consumption.seats.needs_attention", source)
        self.assertIn("لا مقاعد متبقية لإضافة منسوبين", source)
        # الصيغة تتبع العدد هنا أيضاً: «مقعد واحد» لا «1 مقاعد».
        self.assertIn('|arabic_count:"مقعد,مقعدان,مقاعد,مقعداً"', source)

    def test_it_reads_the_existing_summary_instead_of_recounting(self):
        # الحساب والمستويات موجودة في services_archive؛ الناقص كان العرض.
        service = _source("reports/services_archive.py")
        self.assertIn('"warning_level": seat_level', service)
        self.assertIn('"needs_attention": seat_level != "ok"', service)

    def test_multi_line_django_comments_use_the_block_tag(self):
        """‎{# … #}‎ لا يمتدّ سطرين في جانغو، فيُطبع ما بعده نصاً ظاهراً للمستخدم.

        وقع هذا الخطأ ثلاث مرات في هذا العمل وحده، وأمسكه هذا الاختبار في
        الثلاث. ولمّا تكرّر ثلاثاً لم يعد خطأً عارضاً في قالب، فوُسِّع الحرس
        إلى القوالب كلها: من كتب تعليقاً متعدّد الأسطر في أيّ شاشة سقط هنا.
        """
        offenders = []
        root = Path(settings.BASE_DIR) / "reports" / "templates"
        for template in sorted(root.rglob("*.html")):
            for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
                if "{#" in line and "#}" not in line:
                    offenders.append(f"{template.relative_to(root)}:{number}")

        self.assertEqual(
            offenders,
            [],
            "تعليقات ‎{#‎ غير مغلقة على سطرها — استعمل ‎{% comment %}‎: " + ", ".join(offenders[:5]),
        )


class AlertActionContrastTests(SimpleTestCase):
    """زرّ التنبيه: حبرٌ مقرونٌ بخلفيته، لا بخلفيةٍ أخرى."""

    def test_the_alert_button_pairs_accent_ink_with_accent(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        rule = re.search(
            r"\.manager-subscription-alert a \{(.*?)\}", source, flags=re.DOTALL
        )
        self.assertIsNotNone(rule)
        body = rule.group(1)
        self.assertIn("color: var(--id-accent-ink)", body)
        self.assertIn("background: var(--id-accent)", body)
        # الاقتران القديم: حبرُ ‎accent‎ فوق ‎green-700‎ — تباين 2.26 في الليل.
        self.assertNotIn("background: var(--id-green-700)", body)


class DocumentationCoverageTests(TestCase):
    """التغطية: الوصلة المفقودة بين «44 معلماً» و«2 تقرير».

    كانت اللوحة تعرف طرفَي المسألة ولا تعرف ما بينهما — ووظيفة المدير ليست
    معرفة العدد بل معرفة الاسم.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(name="مدرسة التغطية", code="coverage-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة التغطية", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.report_type = ReportType.objects.create(
            school=self.school, code="cov", name="تقرير التغطية"
        )
        self.teachers = []
        for index in range(4):
            teacher = Teacher.objects.create_user(
                phone=f"50077700{index}", name=f"معلم {index}", password="x"
            )
            SchoolMembership.objects.create(
                school=self.school,
                teacher=teacher,
                role_type=SchoolMembership.RoleType.TEACHER,
            )
            self.teachers.append(teacher)

    def _report(self, teacher, when):
        report = Report.objects.create(
            school=self.school,
            teacher=teacher,
            category=self.report_type,
            title=f"تقرير {teacher.pk}",
            report_date=when.date(),
        )
        Report.objects.filter(pk=report.pk).update(created_at=when)
        return report

    def test_it_counts_distinct_documenters_not_documents(self):
        # معلّمٌ كتب ثلاثة تقارير مغطّىً واحد، لا ثلاثة.
        now = timezone.now()
        for _ in range(3):
            self._report(self.teachers[0], now)

        coverage = _build_school_dashboard_payload(self.school, "all")["coverage"]

        self.assertEqual(coverage["covered"], 1)
        self.assertEqual(coverage["total"], 4)
        self.assertEqual(coverage["pending"], 3)
        self.assertEqual(coverage["percent"], 25)

    def test_coverage_follows_the_chosen_period(self):
        # من وثّق العام الماضي ولم يوثّق هذا الشهر متأخّرٌ اليوم لا مغطّى،
        # وإلا صار المؤشّر يقول «مغطّى» عمّن لم يكتب منذ سنة.
        now = timezone.now()
        self._report(self.teachers[0], now)
        self._report(self.teachers[1], now - timedelta(days=400))

        this_month = _build_school_dashboard_payload(self.school, "month")["coverage"]
        all_time = _build_school_dashboard_payload(self.school, "all")["coverage"]

        self.assertEqual(this_month["covered"], 1)
        self.assertEqual(all_time["covered"], 2)

    def test_the_names_of_the_pending_are_offered_not_only_their_count(self):
        self._report(self.teachers[0], timezone.now())

        coverage = _build_school_dashboard_payload(self.school, "all")["coverage"]
        names = {person["name"] for person in coverage["pending_preview"]}

        self.assertNotIn("معلم 0", names)
        self.assertEqual(names, {"معلم 1", "معلم 2", "معلم 3"})

    def test_an_empty_school_does_not_divide_by_zero(self):
        SchoolMembership.objects.filter(school=self.school).delete()

        coverage = _build_school_dashboard_payload(self.school, "all")["coverage"]

        self.assertEqual(coverage["total"], 0)
        self.assertEqual(coverage["percent"], 0)


class PeriodTrendTests(TestCase):
    """الاتجاه: الرقم الذي لا يُقارَن لا معنى له."""

    def test_the_previous_window_matches_the_elapsed_span_not_the_whole_month(self):
        # لو قُورن يومان مضيا بشهرٍ كامل قبلهما لقال المؤشّر «انخفاض 90%» في
        # مطلع كل شهر ثم تعافى وحده — إنذارٌ كاذبٌ يُعلّم تجاهل السهم.
        start = _dashboard_period_start("month")
        previous_start, previous_end = _previous_period_window("month")

        self.assertIsNotNone(previous_start)
        self.assertLess(previous_start, start)
        elapsed_now = timezone.now() - start
        self.assertAlmostEqual(
            (previous_end - previous_start).total_seconds(),
            elapsed_now.total_seconds(),
            delta=5,
        )

    def test_all_time_has_no_comparable_window(self):
        # المقارنة بما قبل بداية التاريخ لا معنى لها، والصمت أصدق من صفر.
        self.assertEqual(_previous_period_window("all"), (None, None))

    def test_flat_is_a_third_state_not_a_rounded_rise(self):
        self.assertEqual(_trend(5, 5)["direction"], "flat")
        self.assertEqual(_trend(7, 5)["direction"], "up")
        self.assertEqual(_trend(3, 5)["direction"], "down")

    def test_growth_from_zero_reports_no_percentage(self):
        # «زيادة 100%» من لا شيء رقمٌ مخترع؛ الفرق وحده صادق.
        trend = _trend(4, 0)
        self.assertEqual(trend["delta"], 4)
        self.assertIsNone(trend["percent"])

    def test_percentage_is_reported_when_there_is_a_baseline(self):
        self.assertEqual(_trend(5, 4)["percent"], 25)
        self.assertEqual(_trend(3, 6)["percent"], -50)


class KpiScopeHonestyTests(SimpleTestCase):
    """كل مؤشّر يقول نطاقه بدل الاعتذار بين قوسين."""

    def test_each_kpi_declares_whether_it_follows_the_period(self):
        source = _source("reports/views/schools.py")
        self.assertIn('"kpi_scopes"', source)
        # الطلبات المفتوحة لا تتبع الفترة عمداً: يجب ألا تختفي لأن المدير
        # بدّل نافذة التحليل. والقرار صحيح — الناقص كان إعلانه.
        self.assertIn('"tickets_open": "current"', source)
        self.assertIn('"reports_count": "period"', source)

    def test_the_dashboard_no_longer_apologises_in_a_parenthetical(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertNotIn("وبقية المؤشرات تشمل كل الفترات", source)
        self.assertNotIn('manager-kpi__scope">(كل الفترات)', source)
        self.assertIn("manager-kpi__scope--now", source)

    def test_the_trend_badge_reads_by_shape_and_words_not_colour_alone(self):
        partial = _source("reports/templates/reports/partials/kpi_trend.html")
        self.assertIn("fa-arrow-trend-up", partial)
        self.assertIn("fa-arrow-trend-down", partial)
        self.assertIn("sr-only", partial)
        # بلا مقارنة لا شارة: شارةٌ فارغة تشغل مكاناً وتوحي بعطل.
        self.assertIn("{% if trend %}", partial)

    def test_live_refresh_repaints_coverage_and_trend(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn("paintCoverage(nextPayload.coverage", source)
        self.assertIn('paintTrend("reportsTrend"', source)
        self.assertIn('paintTrend("coverageTrend"', source)

    def test_hidden_beats_the_class_that_gives_it_display(self):
        # ‎[hidden]‎ سمةٌ تخسر أمام محدّد صنف، فتظهر الحالتان معاً.
        css = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn(".manager-coverage__done[hidden]", css)

    def test_the_dark_layer_reaches_the_new_surfaces(self):
        css = _source("static/css/dark-mode.css")
        for selector in (".kpi-trend--up", ".kpi-trend--down", ".manager-coverage__names li"):
            with self.subTest(selector=selector):
                self.assertIn(f'html[data-theme="dark"] {selector}', css)


class DepartmentComparisonTests(TestCase):
    """مقارنة الأقسام: التقرير يُنسب إلى كاتبه، والقائمة تبدأ بالمتأخّر."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(name="مدرسة الأقسام", code="dept-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة الأقسام", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.report_type = ReportType.objects.create(
            school=self.school, code="dept", name="تقرير القسم"
        )
        self.teachers = []
        for index in range(4):
            teacher = Teacher.objects.create_user(
                phone=f"50088800{index}", name=f"عضو {index}", password="x"
            )
            SchoolMembership.objects.create(
                school=self.school,
                teacher=teacher,
                role_type=SchoolMembership.RoleType.TEACHER,
            )
            self.teachers.append(teacher)

        self.strong = Department.objects.create(
            school=self.school, name="القسم النشط", slug="dept-strong", is_active=True
        )
        self.weak = Department.objects.create(
            school=self.school, name="القسم المتأخر", slug="dept-weak", is_active=True
        )
        DepartmentMembership.objects.create(department=self.strong, teacher=self.teachers[0])
        DepartmentMembership.objects.create(department=self.strong, teacher=self.teachers[1])
        DepartmentMembership.objects.create(department=self.weak, teacher=self.teachers[2])
        DepartmentMembership.objects.create(department=self.weak, teacher=self.teachers[3])

    def _report(self, teacher):
        Report.objects.create(
            school=self.school,
            teacher=teacher,
            category=self.report_type,
            title=f"تقرير {teacher.pk}",
            report_date=timezone.localdate(),
        )

    def test_the_laggard_is_listed_first_because_it_is_what_needs_acting_on(self):
        # القائمة تُقرأ من أعلاها، والمدير يتصرّف مع المتأخّر لا مع المتقدّم.
        self._report(self.teachers[0])
        self._report(self.teachers[1])

        departments = _build_school_dashboard_payload(self.school, "all")["departments"]

        self.assertEqual([d["name"] for d in departments], ["القسم المتأخر", "القسم النشط"])
        self.assertEqual(departments[0]["percent"], 0)
        self.assertEqual(departments[1]["percent"], 100)

    def test_a_report_is_credited_to_its_author_not_to_its_type(self):
        # النسبة عبر Department.reporttypes تحتسب النوع المشترك مرتين، فيبدو
        # القسمان أنشطَ مما هما. والمدير يقصد بـ«قسم العلوم» معلّميه.
        self._report(self.teachers[2])

        payload = _build_school_dashboard_payload(self.school, "all")["departments"]
        departments = {d["name"]: d for d in payload}

        self.assertEqual(departments["القسم المتأخر"]["reports"], 1)
        self.assertEqual(departments["القسم النشط"]["reports"], 0)

    def test_an_empty_department_is_left_out_rather_than_topping_the_list(self):
        # صفرٌ لقسمٍ بلا أعضاء فراغٌ تنظيمي لا تأخّر، وإظهاره في الصدارة
        # يُغرق القائمة بما لا إجراء له.
        Department.objects.create(
            school=self.school, name="قسم بلا أعضاء", slug="dept-empty", is_active=True
        )

        payload = _build_school_dashboard_payload(self.school, "all")["departments"]

        self.assertNotIn("قسم بلا أعضاء", [d["name"] for d in payload])

    def test_it_stays_aggregate_regardless_of_department_count(self):
        # ثلاثة استعلامات مهما بلغ عدد الأقسام — لا استعلامٌ لكل قسم.
        for index in range(8):
            department = Department.objects.create(
                school=self.school, name=f"قسم {index}", slug=f"bulk-{index}", is_active=True
            )
            DepartmentMembership.objects.create(department=department, teacher=self.teachers[0])

        reports_qs = Report.objects.filter(school=self.school)
        with self.assertNumQueries(3):
            _department_activity(self.school, reports_qs, {self.teachers[0].pk})


class TicketResponsivenessTests(TestCase):
    """الاستجابة: النسبة تُخفي ما ينبغي أن تكشفه."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(name="مدرسة الاستجابة", code="resp-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة الاستجابة", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500999001", name="مدير الاستجابة", password="x", is_staff=True
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

    def _ticket(self, *, status, created_ago, closed_after=None):
        ticket = Ticket.objects.create(
            school=self.school, creator=self.manager, title="طلب", body="نص", status=status
        )
        created = timezone.now() - created_ago
        updated = created + closed_after if closed_after else created
        Ticket.objects.filter(pk=ticket.pk).update(created_at=created, updated_at=updated)
        return ticket

    def test_two_tickets_with_the_same_status_are_not_the_same_service(self):
        # واحدٌ أُغلق في ساعة وآخر بعد ثلاثة أيام: النسبة تساويهما والزمن لا.
        self._ticket(status=Ticket.Status.DONE, created_ago=timedelta(hours=5), closed_after=timedelta(hours=1))
        self._ticket(status=Ticket.Status.DONE, created_ago=timedelta(days=4), closed_after=timedelta(hours=71))

        payload = _build_school_dashboard_payload(self.school, "all")["responsiveness"]

        self.assertEqual(payload["avg_close_hours"], 36.0)
        # الحدّ يومان: ما دونهما يُقرأ بالساعات لأنها أدقّ، وما فوقهما بالأيام.
        self.assertEqual(payload["avg_close_label"], "36 ساعة")

    def test_hours_read_as_hours_until_two_days_then_as_days(self):
        self._ticket(status=Ticket.Status.DONE, created_ago=timedelta(hours=9), closed_after=timedelta(hours=6))

        payload = _build_school_dashboard_payload(self.school, "all")["responsiveness"]

        self.assertEqual(payload["avg_close_label"], "6 ساعة")

    def test_past_two_days_the_label_switches_to_days(self):
        # «72 ساعة» رقمٌ صحيحٌ لا يُقرأ؛ الذهن يترجمه إلى ثلاثة أيام فليُقل له.
        self._ticket(status=Ticket.Status.DONE, created_ago=timedelta(days=5), closed_after=timedelta(hours=72))

        payload = _build_school_dashboard_payload(self.school, "all")["responsiveness"]

        self.assertEqual(payload["avg_close_label"], "3 يوم")

    def test_the_oldest_open_ticket_is_named_and_reachable(self):
        # الرقم الذي لا يُفتح ملاحظة؛ والذي يُفتح إجراء.
        old = self._ticket(status=Ticket.Status.OPEN, created_ago=timedelta(days=23))
        self._ticket(status=Ticket.Status.OPEN, created_ago=timedelta(days=2))

        payload = _build_school_dashboard_payload(self.school, "all")["responsiveness"]

        self.assertEqual(payload["oldest_open_id"], old.pk)
        self.assertEqual(payload["oldest_open_days"], 23)

    def test_a_school_with_nothing_to_measure_says_so_instead_of_showing_zero(self):
        # «صفر ساعة» تُقرأ كأداءٍ ممتاز، والصواب ألا يُعرض شيء.
        payload = _build_school_dashboard_payload(self.school, "all")["responsiveness"]

        self.assertFalse(payload["has_signal"])
        self.assertIsNone(payload["avg_close_hours"])
        self.assertIsNone(payload["oldest_open_id"])

    def test_a_closed_ticket_does_not_count_as_the_oldest_open_one(self):
        self._ticket(status=Ticket.Status.DONE, created_ago=timedelta(days=90), closed_after=timedelta(hours=2))

        payload = _build_school_dashboard_payload(self.school, "all")["responsiveness"]

        self.assertIsNone(payload["oldest_open_id"])


class VisitSnapshotTests(SimpleTestCase):
    """لقطة الزيارة الميدانية: ورقةٌ تحمل إسنادها.

    الاسم ‎VisitSnapshot‎ اختياراً: في المنصة حارسٌ يمنع ذِكر دورٍ أُزيل منها
    — بالعربية والإنجليزية معاً — وهو لا يفرّق بين إحياء الدور وبين وصفٍ
    تصادف لفظه. فتُجتنَب الكلمة هنا وفي شرحها، لأن كلفة تسميةٍ أخرى صفر
    وكلفة ثقبٍ في الحارس ليست صفراً.
    """

    def test_the_printed_sheet_states_school_year_period_and_date(self):
        # رقمٌ لا يُعرف مداه لا يُحتجّ به، فتحمل الورقة الأربعة معاً.
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn("manager-print-head", source)
        self.assertIn("{{ active_school.current_academic_year", source)
        self.assertIn("الفترة: {{ selected_period_label }}", source)
        self.assertIn("{{ today_hijri }} هـ", source)

    def test_the_snapshot_header_never_shows_on_screen(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn(".manager-print-head { display: none; }", source)

    def test_buttons_are_dropped_and_evidence_is_kept(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        print_block = source[source.index("@media print {"):]
        # ‎#managerActions‎ لم يعد موجوداً: ستّ بطاقاته كانت تكرّر الشريط
        # العلوي حرفاً بحرف، فحُذفت بدل أن تُخفى في الطباعة وحدها.
        for hidden in ("#managerWorkspaces", "#managerResources", ".manager-coverage__actions"):
            with self.subTest(hidden=hidden):
                self.assertIn(hidden, print_block)
        # الرسوم حجّةٌ على الورق لا زينة — تُقصّ ولا تُحذف.
        self.assertIn(".manager-chart__canvas { max-height", print_block)

    def test_the_snapshot_button_sits_beside_the_period_it_captures(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        toolbar = source[source.index('class="manager-analytics__toolbar"'):]
        toolbar = toolbar[: toolbar.index("</div>\n      </div>")]
        self.assertIn('id="printDashboardSnapshot"', toolbar)
        self.assertIn('data-school-period="month"', toolbar)


class DualNumeralTests(SimpleTestCase):
    """العربية لا تُتبع المثنّى برقمه: «طلبان» لا «2 طلبان»."""

    def test_the_completion_sentence_carries_the_count_inside_the_word(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn('data-count-mode="full"', source)
        self.assertIn("|arabic_count:", source)

    def test_the_javascript_twin_honours_the_same_mode(self):
        javascript = _source("static/js/arabic-count.js")
        self.assertIn("data-count-mode", javascript)
        self.assertIn("full ? count(value, spec) : word(value, spec)", javascript)


class PrintFromDarkModeTests(SimpleTestCase):
    """الورق أبيضُ دائماً.

    الطبقة الداكنة تُعطي الحبر ألواناً فاتحة لتُقرأ على خلفيةٍ سوداء، ثم تُسقط
    الطابعة الخلفية — لأن المتصفّحات لا تطبع خلفيات الصفحة افتراضاً — فيبقى
    الحبر الفاتح على بياض الورقة. والمدير يخرج بورقةٍ عناوينُها رمادٌ باهت،
    ولا يعلم أن السبب زرٌّ ضغطه في الليل قبل ساعة.

    والعلّة عامّة لا تخصّ اللوحة: تمسّ كل ما يُطبع — التقارير والمحاضر
    والتكليفات — فيقع علاجها في طبقة الوضع الداكن نفسها.
    """

    def _print_block(self, css: str) -> str:
        start = css.index("@media print {")
        depth = 0
        for index in range(start, len(css)):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    return css[start : index + 1]
        return css[start:]

    def test_the_dark_palette_is_reverted_to_light_when_printing(self):
        block = self._print_block(_source("static/css/dark-mode.css"))

        self.assertIn('html[data-theme="dark"]', block)
        self.assertIn("color-scheme: light", block)
        for token, light_value in (
            ("--surface", "#ffffff"),
            ("--text-main", "#10251f"),
            ("--id-accent-ink", "#ffffff"),
        ):
            with self.subTest(token=token):
                self.assertIn(f"{token}: {light_value};", block)

    def test_chrome_that_is_not_information_costs_no_ink(self):
        block = self._print_block(_source("static/css/dark-mode.css"))

        for selector in (".mansour-launcher", ".bottom-nav", ".drawer", ".hdr-nav"):
            with self.subTest(selector=selector):
                self.assertIn(selector, block)

    def test_decorative_gradients_are_stripped_not_only_background_colours(self):
        # التدرّج ‎background-image‎ ينجو من ‎background-color‎، فتبقى البطاقة
        # خضراءَ داكنة وحبرُها أبيضُ على ورقٍ أبيض.
        source = _source("reports/templates/reports/admin_dashboard.html")
        print_block = source[source.index("@media print {"):]
        self.assertIn("background-image: none !important", print_block)
        self.assertIn(".manager-now,", print_block)


class SchoolAgendaTests(TestCase):
    """المواعيد من مصادرها الأربعة على خطٍّ واحد."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(name="مدرسة المواعيد", code="agenda-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة المواعيد", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500123001", name="مدير المواعيد", password="x", is_staff=True
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

    def test_it_gathers_all_four_sources_onto_one_line(self):
        # أربع وحداتٍ لكلٍّ صفحتها؛ وما لم تُجمع بقي الموعد معروفاً لمن يفتح
        # صفحته ومنسيّاً عند من لا يفتحها.
        now = timezone.now()
        Meeting.objects.create(
            school=self.school, title="اجتماع", scheduled_at=now + timedelta(days=2),
            status=Meeting.Status.SCHEDULED,
        )
        Assignment.objects.create(
            school=self.school, issuer=self.manager, title="تكليف", due_at=now + timedelta(days=3)
        )
        Notification.objects.create(
            school=self.school, created_by=self.manager, title="تعميم", message="نص",
            requires_signature=True, signature_deadline_at=now + timedelta(days=4),
        )

        agenda = _school_agenda(self.school)
        kinds = {item["kind"] for item in agenda["items"]}

        self.assertEqual(kinds, {"meeting", "assignment", "signature"})
        self.assertEqual(agenda["upcoming"], 3)

    def test_what_is_past_stays_visible_and_leads(self):
        # إخفاء المتأخّر بعد أسبوعٍ من فواته يجعل اللوحة أهدأ ممّا ينبغي،
        # والصمت هنا فقدُ أثر لا طمأنينة.
        now = timezone.now()
        Meeting.objects.create(
            school=self.school, title="اجتماع فات", scheduled_at=now - timedelta(days=40),
            status=Meeting.Status.SCHEDULED,
        )
        Meeting.objects.create(
            school=self.school, title="اجتماع قادم", scheduled_at=now + timedelta(days=1),
            status=Meeting.Status.SCHEDULED,
        )

        agenda = _school_agenda(self.school)

        self.assertEqual(agenda["overdue"], 1)
        self.assertEqual(agenda["items"][0]["title"], "اجتماع فات")
        self.assertTrue(agenda["items"][0]["is_overdue"])
        self.assertEqual(agenda["items"][0]["days"], 40)

    def test_nothing_beyond_the_horizon_crowds_the_list(self):
        # الأفق أسبوعان: أطولُ من أن يفاجئ، وأقصرُ من أن يصير قائمةً تُتجاهل.
        Meeting.objects.create(
            school=self.school, title="اجتماع بعيد",
            scheduled_at=timezone.now() + timedelta(days=60),
            status=Meeting.Status.SCHEDULED,
        )

        agenda = _school_agenda(self.school)

        self.assertEqual(agenda["items"], [])
        self.assertEqual(agenda["horizon_days"], 14)

    def test_a_cancelled_assignment_is_not_a_deadline(self):
        Assignment.objects.create(
            school=self.school, issuer=self.manager, title="تكليف مُلغى",
            due_at=timezone.now() + timedelta(days=2),
            cancelled_at=timezone.now(),
        )

        self.assertEqual(_school_agenda(self.school)["items"], [])

    def test_a_held_meeting_is_not_a_deadline(self):
        Meeting.objects.create(
            school=self.school, title="اجتماع عُقد",
            scheduled_at=timezone.now() + timedelta(days=2),
            status=Meeting.Status.HELD,
        )

        self.assertEqual(_school_agenda(self.school)["items"], [])

    def test_dates_are_hijri_like_every_other_screen(self):
        Meeting.objects.create(
            school=self.school, title="اجتماع", scheduled_at=timezone.now() + timedelta(days=1),
            status=Meeting.Status.SCHEDULED,
        )

        item = _school_agenda(self.school)["items"][0]

        self.assertIn("/", item["hijri"])
        self.assertNotIn(str(timezone.now().year), item["hijri"])

    def test_a_school_without_an_active_one_gets_an_empty_calendar(self):
        agenda = _school_agenda(None)

        self.assertEqual(agenda["items"], [])
        self.assertEqual(agenda["overdue"], 0)


class CoverageReminderTests(TestCase):
    """الفعل من حيث رُئي — دون أن يصير الرابط سلاحاً."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(name="مدرسة التذكير", code="remind-school")
        self.other = School.objects.create(name="مدرسة أخرى", code="remind-other")
        for school in (self.school, self.other):
            plan = SubscriptionPlan.objects.create(
                name=f"خطة {school.code}", price=0, days_duration=365, max_teachers=50
            )
            SchoolSubscription.objects.create(school=school, plan=plan)

        self.manager = Teacher.objects.create_user(
            phone="500124001", name="مدير التذكير", password="pass-remind", is_staff=True
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.report_type = ReportType.objects.create(
            school=self.school, code="rem", name="تقرير"
        )

        self.documented = Teacher.objects.create_user(
            phone="500124002", name="من وثّق", password="x"
        )
        self.pending = Teacher.objects.create_user(
            phone="500124003", name="من لم يوثّق", password="x"
        )
        for teacher in (self.documented, self.pending):
            SchoolMembership.objects.create(
                school=self.school, teacher=teacher,
                role_type=SchoolMembership.RoleType.TEACHER,
            )
        self.outsider = Teacher.objects.create_user(
            phone="500124004", name="من مدرسة أخرى", password="x"
        )
        SchoolMembership.objects.create(
            school=self.other, teacher=self.outsider,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        Report.objects.create(
            school=self.school, teacher=self.documented, category=self.report_type,
            title="تقرير", report_date=timezone.localdate(),
        )

    def _login(self):
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_the_form_opens_with_exactly_the_pending_preselected(self):
        # اللوحة عرفت الجواب؛ فلا يُعاد اختيار الخمسة يدوياً من أربعةٍ وأربعين.
        self._login()

        response = self.client.get(reverse("reports:notifications_create") + "?remind=coverage")

        self.assertEqual(response.status_code, 200)
        selected = set(response.context["form"].initial.get("teachers") or [])
        self.assertEqual(selected, {self.pending.pk})
        self.assertNotIn(self.documented.pk, selected)

    def test_it_recomputes_instead_of_trusting_the_url(self):
        # لو قُبلت المعرّفات من الرابط لاستهدف من حرّره من شاء — بما في ذلك
        # منسوبو مدرسةٍ أخرى. فيُمرَّر سببٌ لا قائمة.
        self._login()

        response = self.client.get(
            reverse("reports:notifications_create")
            + f"?remind=coverage&teachers={self.outsider.pk}&teachers={self.documented.pk}"
        )

        selected = set(response.context["form"].initial.get("teachers") or [])
        self.assertEqual(selected, {self.pending.pk})
        self.assertNotIn(self.outsider.pk, selected)

    def test_without_the_reason_nothing_is_preselected(self):
        self._login()

        response = self.client.get(reverse("reports:notifications_create"))

        self.assertIsNone(response.context.get("reminder_context"))
        self.assertFalse(response.context["form"].initial.get("teachers"))

    def test_the_manager_is_told_why_the_form_arrived_filled(self):
        # نموذجٌ يجد المدير حقولَه مملوءةً بلا تفسير يدفعه إلى مراجعة كل اختيار.
        self._login()

        response = self.client.get(reverse("reports:notifications_create") + "?remind=coverage")

        self.assertEqual(response.context["reminder_context"]["count"], 1)
        self.assertContains(response, "ممّن لم يوثّقوا بعد")

    def test_a_school_where_everyone_documented_gets_no_banner(self):
        # لا مستلمين = لا تذكير؛ ونموذجٌ مملوءٌ بلا أحد أسوأ من نموذجٍ فارغ.
        Report.objects.create(
            school=self.school, teacher=self.pending, category=self.report_type,
            title="تقرير", report_date=timezone.localdate(),
        )
        self._login()

        response = self.client.get(reverse("reports:notifications_create") + "?remind=coverage")

        self.assertIsNone(response.context.get("reminder_context"))


class CoverageEmptyStateTests(TestCase):
    """صفرٌ من صفر ليس نجاحاً."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(name="مدرسة بلا فريق", code="empty-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة فارغة", price=0, days_duration=365, max_teachers=10
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)

    def test_a_school_without_staff_is_not_a_school_that_all_documented(self):
        # المدير الجديد يفتح لوحته أول يوم، فكانت تُهنّئه على ما لم يفعله.
        coverage = _build_school_dashboard_payload(self.school, "all")["coverage"]

        self.assertEqual(coverage["total"], 0)
        self.assertFalse(coverage["has_staff"])
        self.assertEqual(coverage["pending"], 0)

    def test_a_staffed_school_reports_that_it_has_staff(self):
        teacher = Teacher.objects.create_user(phone="500125001", name="معلم", password="x")
        SchoolMembership.objects.create(
            school=self.school, teacher=teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        coverage = _build_school_dashboard_payload(self.school, "all")["coverage"]

        self.assertTrue(coverage["has_staff"])

    def test_the_template_branches_on_three_states_not_two(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn('id="coverageEmptyBox"', source)
        self.assertIn("{% if coverage.pending or not coverage.has_staff %}", source)
        self.assertIn("لا منسوبين بعد", source)

    def test_live_refresh_knows_the_third_state_too(self):
        source = _source("reports/templates/reports/admin_dashboard.html")
        self.assertIn("coverage.has_staff !== false", source)
        self.assertIn("emptyBox.hidden = hasStaff", source)


class SharedCoverageSourceTests(TestCase):
    """مصدرٌ واحد للتغطية — لا نسختان تختلفان يوماً."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(name="مدرسة المصدر", code="source-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة المصدر", price=0, days_duration=365, max_teachers=50
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.report_type = ReportType.objects.create(
            school=self.school, code="src", name="تقرير"
        )
        self.teachers = []
        for index in range(3):
            teacher = Teacher.objects.create_user(
                phone=f"50012600{index}", name=f"معلم {index}", password="x"
            )
            SchoolMembership.objects.create(
                school=self.school, teacher=teacher,
                role_type=SchoolMembership.RoleType.TEACHER,
            )
            self.teachers.append(teacher)
        Report.objects.create(
            school=self.school, teacher=self.teachers[0], category=self.report_type,
            title="تقرير", report_date=timezone.localdate(),
        )

    def test_the_dashboard_and_the_reminder_read_the_same_function(self):
        # لو نُسخ الحساب لاختلفت الشاشتان يوماً، فيقرأ المدير في اللوحة خمسةً
        # ويُرسل التذكير إلى ستة ولا يجد ما يفسّر الفرق.
        dashboard = _build_school_dashboard_payload(self.school, "all")["coverage"]
        shared = pending_documenters(self.school).count()

        self.assertEqual(dashboard["pending"], shared)
        self.assertEqual(shared, 2)

    def test_the_view_no_longer_computes_coverage_by_hand(self):
        source = _source("reports/views/schools.py")
        self.assertIn("from ..coverage import documented_teacher_ids, pending_documenters", source)
        self.assertIn("documented_ids = documented_teacher_ids(", source)
