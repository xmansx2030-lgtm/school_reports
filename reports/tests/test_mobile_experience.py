# -*- coding: utf-8 -*-
"""تجربة الجوال: ما قاسه المتصفّح على شاشة هاتف، مثبَّتاً هنا.

كل حالة في هذا الملف وُلدت من قياسٍ فعلي بمحرّك عرضٍ حقيقي على مقاس iPhone
(‏393×852) لا من قراءة CSS:

* شريط التنبيه كان يبدأ عند ‎y=24 والترويسة تمتد إلى ‎y=45 — أي أنه يغطّي زر
  القائمة. وتنبيه التحذير لا يختفي تلقائياً، فيبقى الحاجز حتى يُغلق يدوياً.
* زر إغلاق التنبيه قِيس ‎33×44 بينما الحدّ الأدنى للمس ‎44×44.
* رابط الهوية في الترويسة قِيس ‎38px ارتفاعاً، وأثبت اختبار النقر أن نقطةً
  على بعد ‎3px فوقه تصيب الحاوية لا الرابط — على ‎125 صفحة.
* شارة الإشعار كانت أيقونةً ملوّنة كاملة، وأندرويد لا يبقي منها إلا قناة
  الشفافية فتظهر مربّعاً رمادياً في شريط الحالة.

أعِد المسح بعد أي تعديل هنا:
    node .claude/skills/dark-mode-audit/scripts/mobile_sweep.cjs --base <url>
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings

TOUCH_MIN_PX = 44


def _source(relative_path: str) -> str:
    return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")


class NotificationBadgeAssetTests(SimpleTestCase):
    """شارة الإشعار صورةٌ يعيد النظام تلوينها، فلا تصلح أيقونةً ملوّنة."""

    BADGES = ("static/img/pwa/badge-96.png", "static/img/pwa/badge-72.png")

    def test_badges_exist_at_the_documented_sizes(self):
        for relative_path in self.BADGES:
            with self.subTest(badge=relative_path):
                payload = (Path(settings.BASE_DIR) / relative_path).read_bytes()
                self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", payload[16:24])
                expected = int(relative_path.split("-")[-1].split(".")[0])
                self.assertEqual((width, height), (expected, expected))

    def test_badge_is_a_transparent_silhouette_not_a_filled_block(self):
        """لو كانت الشارة مصمتة لظهرت مربّعاً في شريط الحالة."""
        from PIL import Image

        badge = Image.open(Path(settings.BASE_DIR) / "static/img/pwa/badge-96.png").convert("RGBA")
        alpha = badge.getchannel("A")
        low, high = alpha.getextrema()

        self.assertEqual(low, 0, "الشارة بلا مناطق شفافة — ستُرسم مربّعاً مصمتاً")
        self.assertEqual(high, 255, "الشارة بلا مناطق معتمة — لن يظهر منها شيء")
        opaque = sum(1 for value in alpha.get_flattened_data() if value > 128)
        coverage = opaque / (badge.width * badge.height)
        self.assertGreater(coverage, 0.05, "الرسم أصغر من أن يُرى")
        self.assertLess(coverage, 0.60, "الرسم يملأ المربّع فلا يُقرأ شكلاً")

    def test_push_payload_uses_the_monochrome_badge(self):
        source = _source("reports/web_push.py")

        self.assertIn('"badge": "/static/img/pwa/badge-96.png"', source)
        self.assertNotIn('"badge": "/static/img/pwa/icon-192.png"', source)

    def test_service_worker_falls_back_to_the_badge_and_precaches_it(self):
        source = _source("static/sw.js")

        self.assertIn('payload.badge || "/static/img/pwa/badge-96.png"', source)
        self.assertIn('"/static/img/pwa/badge-96.png"', source)
        # مفتاح التخزين يجب أن يطابق الطلب: الطلب بلا استعلام، فلا يُخزَّن بـ``?v=``
        self.assertIn('"/static/manifest.json"', source)
        self.assertNotIn('"/static/manifest.json?v=', source)


class MobilePwaWorkflowTests(SimpleTestCase):
    def test_authenticated_shell_has_role_aware_bottom_navigation_and_runtime_status(self):
        template = _source("reports/templates/base.html")
        styles = _source("static/css/mobile-professional.css")

        self.assertIn('class="mobile-tabbar"', template)
        self.assertIn('data-mobile-tabbar-more', template)
        self.assertIn('id="pwaNetworkStatus"', template)
        self.assertIn('id="pwaUpdateReady"', template)
        self.assertIn("grid-template-columns: repeat(5", styles)
        self.assertIn("env(safe-area-inset-bottom)", styles)

    def test_shared_draft_manager_keeps_fields_and_image_blobs_until_success(self):
        script = _source("static/js/pwa-form-draft.js")
        add_template = _source("reports/templates/reports/add_report.html")
        edit_template = _source("reports/templates/reports/edit_report.html")

        self.assertIn('indexedDB.open(DB_NAME', script)
        self.assertIn('files[field.name]', script)
        self.assertIn('new DataTransfer()', script)
        self.assertIn('tawtheeq:submit-success', script)
        self.assertIn('data-pwa-draft-key=', add_template)
        self.assertIn('data-pwa-draft-key=', edit_template)
        self.assertNotIn('form.addEventListener("submit", function(){\n      try{ localStorage.removeItem(KEY)', add_template)

    def test_report_evidence_picker_exposes_camera_and_gallery(self):
        # بطاقة الشاهد صارت جزئيةً مستقلة يشترك فيها العرضُ من الخادم وقالبُ
        # البطاقة المضافة بالجافاسكربت، فلا تُكتب مرّتين.
        template = _source("reports/templates/reports/partials/report_evidence_card.html")
        formset = _source("reports/templates/reports/partials/report_evidence_formset.html")
        script = _source("static/js/report-evidence-editor.js")
        styles = _source("static/css/report-evidence.css")

        self.assertIn('data-image-source="camera"', template)
        self.assertIn('data-image-source="gallery"', template)
        self.assertIn('for="{{ evidence_form.image.id_for_label }}"', template)
        # الجزئية الواحدة تُدرَج في الموضعين — وإلا افترقت البطاقتان.
        self.assertEqual(formset.count("reports/partials/report_evidence_card.html"), 2)
        self.assertIn('input.setAttribute("capture", "environment")', script)
        self.assertIn('input.removeAttribute("capture")', script)
        self.assertIn("new FileReader()", script)
        self.assertIn("reader.readAsDataURL(file)", script)
        # The generic preview rules set ``display`` on both the placeholder
        # and the image.  Without an explicit hidden rule they both consume a
        # full frame, pushing the selected image below the clipped viewport.
        self.assertIn('.ree-preview [hidden] { display: none !important; }', styles)


class ToastLayerTests(SimpleTestCase):
    """التنبيه يعلو المحتوى ولا يعلو أدوات التنقّل."""

    def test_toasts_are_offset_below_the_header_on_phones(self):
        source = _source("static/css/app-shell.css")

        self.assertIn("top: calc(var(--header-h, 56px) + 8px);", source)
        self.assertNotIn("top: calc(12px + var(--safe-top));", source)

    def test_header_height_is_published_for_the_floating_layers(self):
        source = _source("reports/templates/base.html")

        self.assertIn("--header-h", source)
        self.assertIn("ResizeObserver", source)

    def test_toast_close_button_meets_the_touch_minimum(self):
        source = _source("static/css/app-shell.css")
        block = source.split(".msg-close {", 1)[1].split("}", 1)[0]

        self.assertIn(f"min-width: {TOUCH_MIN_PX}px", block)
        self.assertIn(f"min-height: {TOUCH_MIN_PX}px", block)

    def test_toast_variants_sit_on_an_opaque_surface(self):
        """الشارة تجلس على بطاقة؛ التنبيه يطفو فوق النص.

        كان ``.msg.warning`` يشترك مع ``.badge.warning`` في قاعدة واحدة صبغتها
        ‎15%‎ شفافية، فقِيس على الجوال ‎rgba(185,151,91,0.15)‎ وظهر نص الصفحة
        من خلال التنبيه.
        """
        for stylesheet in ("static/css/royal-theme.css", "static/css/app.css", "static/css/dark-mode.css"):
            source = _source(stylesheet)
            for variant in (".msg.success", ".msg.warning", ".msg.error"):
                with self.subTest(stylesheet=stylesheet, variant=variant):
                    self.assertNotIn(
                        f"{variant} {{\n  background: rgba",
                        source,
                        "التنبيه عاد إلى خلفية شفافة تُقرأ الصفحة من خلالها",
                    )
            self.assertIn("background-color: var(--surface) !important;", source)

    def test_no_later_rule_makes_a_toast_translucent_again(self):
        """القاعدة الأولى صُحّحت، ثم أعادته قاعدةٌ متأخرة شفافاً في الوضع الداكن.

        قِيس بالمتصفّح: ``.msg.error`` رجع إلى ‎rgba(180,35,47,0.16)‎ لأن قاعدة
        ``.msg.error, .error, .form-errors`` بعدها كانت تجمع التنبيه مع حقول
        النموذج. الجمع نفسه هو الخطأ.
        """
        source = _source("static/css/dark-mode.css")

        for grouped in (
            'html[data-theme="dark"] .msg.error,\nhtml[data-theme="dark"] .error,',
            'html[data-theme="dark"] .msg.success,\nhtml[data-theme="dark"] .alert-success',
        ):
            self.assertNotIn(grouped, source, "التنبيه عاد مجموعاً مع عناصر تجلس على سطح مصمت")

    def test_badges_keep_their_translucent_tint(self):
        """الإصلاح يخص التنبيه وحده؛ الشارة داخل البطاقة تبقى كما صُمّمت."""
        source = _source("static/css/royal-theme.css")

        self.assertIn(".badge.warning {\n  background: rgba(185, 151, 91, .15) !important;", source)

    def test_header_brand_is_a_full_touch_target(self):
        source = _source("static/css/app-shell.css")
        block = source.split(".hdr-brand {", 1)[1].split("}", 1)[0]

        self.assertIn(f"min-height: {TOUCH_MIN_PX}px", block)


@override_settings(PWA_INSTALL_ENABLED=True)
class ManifestTests(SimpleTestCase):
    """بيان التطبيق كما يقرأه نظام التشغيل عند التثبيت."""

    @staticmethod
    def _manifest() -> dict:
        return json.loads(_source("static/manifest.json"))

    def test_manifest_is_valid_and_standalone_arabic(self):
        manifest = self._manifest()

        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["dir"], "rtl")
        self.assertEqual(manifest["lang"], "ar")
        self.assertTrue(manifest["name"].strip())
        self.assertIn("display_override", manifest)

    def test_manifest_offers_shortcuts_to_the_daily_tasks(self):
        """الضغط المطوّل على الأيقونة أقصر طريقٍ للمستخدم الذي لا يملك إلا جوالاً."""
        shortcuts = self._manifest().get("shortcuts") or []

        self.assertGreaterEqual(len(shortcuts), 3)
        for shortcut in shortcuts:
            with self.subTest(shortcut=shortcut.get("name")):
                self.assertTrue(shortcut.get("name", "").strip())
                self.assertTrue(shortcut.get("url", "").startswith("/"))

    def test_shortcut_targets_are_real_routes(self):
        from django.urls import resolve

        for shortcut in self._manifest().get("shortcuts") or []:
            with self.subTest(url=shortcut["url"]):
                # يرفع ``Resolver404`` إن كان المسار وهماً — وهو ما يجعل الاختصار
                # يفتح صفحة خطأ من شاشة البداية.
                resolve(shortcut["url"])

    def test_icons_include_a_maskable_pair(self):
        purposes = {
            (icon.get("purpose") or "any")
            for icon in self._manifest()["icons"]
        }

        self.assertIn("maskable", purposes)

    def test_every_icon_file_exists_at_its_declared_size(self):
        for icon in self._manifest()["icons"]:
            with self.subTest(icon=icon["src"]):
                relative = icon["src"].lstrip("/")
                payload = (Path(settings.BASE_DIR) / relative).read_bytes()
                self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", payload[16:24])
                declared = icon["sizes"].split("x")[0]
                self.assertEqual(str(width), declared)
                self.assertEqual(width, height)


class AssistantNavigationTests(SimpleTestCase):
    """روابط المساعد كانت تنقل المستخدم بعيداً وتمسح عمله ومحادثته.

    الأداة مضمّنة في ``base.html`` أي في كل صفحة داخلية بما فيها نماذج إدخال
    التقارير، وسجلّ المحادثة كان مصفوفةً في الذاكرة. فنقرةٌ على «دليل المستخدم»
    وسط تعبئة تقرير كانت تُفقد النموذج والمحادثة معاً.
    """

    def test_sources_open_beside_the_page_not_instead_of_it(self):
        script = _source("static/js/mansour-assistant.js")

        self.assertIn('link.target = "_blank"', script)
        self.assertIn('link.rel = "noopener noreferrer"', script)
        self.assertIn("يفتح في تبويب جديد", script)

    def test_a_same_page_anchor_scrolls_instead_of_opening_a_tab(self):
        """«الباقات والأسعار» يشير إلى ``/#pricing`` وهو مرساة في الصفحة نفسها."""
        script = _source("static/js/mansour-assistant.js")

        self.assertIn("function isSamePageAnchor", script)
        self.assertIn('scrollIntoView({ behavior: "smooth"', script)

    def test_the_conversation_survives_navigation(self):
        script = _source("static/js/mansour-assistant.js")

        self.assertIn("window.sessionStorage.setItem(storageKey", script)
        self.assertIn("function restoreConversation", script)
        # ``localStorage`` يُبقي المحادثة بعد إغلاق المتصفّح على جهاز مشترك،
        # فالتخزين هنا مقصور على الجلسة. (الاسم يرد في تعليقٍ يشرح ذلك، فالفحص
        # على الاستعمال لا على الذكر.)
        self.assertNotIn("localStorage.setItem", script)
        self.assertNotIn("window.localStorage", script)

    def test_a_new_session_does_not_inherit_the_previous_conversation(self):
        script = _source("static/js/mansour-assistant.js")

        self.assertIn('root.getAttribute("data-chat-scope")', script)
        self.assertIn("function dropForeignConversations", script)

    def test_the_widget_receives_an_opaque_session_scope(self):
        template = _source("reports/templates/reports/partials/mansour_assistant.html")

        self.assertIn('data-chat-scope="{{ MANSOUR_CHAT_SCOPE|default:\'anon\' }}"', template)


class AssistantChatScopeTests(SimpleTestCase):
    """المفتاح يجب أن يتغيّر مع الجلسة، وألا يكشف مفتاحها."""

    @staticmethod
    def _scope(session_key):
        from types import SimpleNamespace

        from reports.ai_features import mansour_chat_scope

        request = SimpleNamespace(session=SimpleNamespace(session_key=session_key))
        return mansour_chat_scope(request)

    def test_scope_is_stable_for_one_session(self):
        self.assertEqual(self._scope("session-alpha"), self._scope("session-alpha"))

    def test_scope_changes_when_the_session_cycles_on_login(self):
        self.assertNotEqual(self._scope("session-alpha"), self._scope("session-beta"))

    def test_scope_never_leaks_the_session_key(self):
        scope = self._scope("super-secret-session-key")

        self.assertNotIn("super-secret", scope)
        self.assertEqual(len(scope), 16)

    def test_anonymous_visitors_get_a_shared_public_scope(self):
        self.assertEqual(self._scope(None), "anon")
        self.assertEqual(self._scope(""), "anon")
