from __future__ import annotations

import json
import struct
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    SITE_URL="https://tawtheeq.example",
    PWA_INSTALL_ENABLED=True,
)
class PwaInstallExperienceTests(TestCase):
    @staticmethod
    def _source(relative_path: str) -> str:
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    @staticmethod
    def _png_size(relative_path: str) -> tuple[int, int]:
        payload = (Path(settings.BASE_DIR) / relative_path).read_bytes()
        if payload[:8] != b"\x89PNG\r\n\x1a\n":
            raise AssertionError(f"Not a PNG file: {relative_path}")
        return struct.unpack(">II", payload[16:24])

    def test_public_mobile_entry_pages_render_the_shared_installer(self):
        for route_name in (
            "reports:landing",
            "reports:login",
            "reports:user_guide",
            "reports:register_school",
        ):
            response = self.client.get(reverse(route_name))

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'id="pwaInstallPrompt"')
            self.assertContains(response, 'id="pwaInstallAction"')
            self.assertContains(response, 'id="pwaInstallAnnouncement"')
            self.assertContains(response, 'aria-live="polite"')
            self.assertContains(response, 'data-auto-prompt="false"')
            self.assertContains(response, "css/pwa-install.css")
            self.assertContains(response, "js/pwa-install.js")
            self.assertContains(response, 'rel="manifest"')
            self.assertContains(response, 'rel="apple-touch-icon"')
            self.assertContains(response, 'rel="apple-touch-startup-image"', count=24)
            self.assertContains(response, "img/pwa/apple-touch-icon-180.png")

    def test_a_visitor_is_never_auto_prompted_to_install(self):
        """طلبُ التثبيت قبل امتلاك حساب يقود إلى تطبيقٍ يفتح على شاشة دخول."""
        for route_name in ("reports:landing", "reports:login", "reports:user_guide"):
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))

                self.assertContains(response, 'data-auto-prompt="false"')

    def test_a_visitor_still_gets_a_way_to_install_when_they_want_one(self):
        """قِيس بالمتصفّح: الزائر على الجوال لم يكن يرى بطاقةً ولا زراً — أي أن
        من أراد التثبيت وجب أن يعرف قائمة المتصفّح بنفسه."""
        for route_name in ("reports:landing", "reports:user_guide"):
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))

                self.assertContains(response, "data-pwa-install-trigger")
                # البطاقة ونصّها مكتوبان للجوال، فالزرّ يختفي على سطح المكتب.
                self.assertContains(response, "data-pwa-install-mobile-only")

    def test_the_mobile_only_trigger_is_hidden_off_mobile_by_the_installer(self):
        script = self._source("static/js/pwa-install.js")

        self.assertIn('trigger.hasAttribute("data-pwa-install-mobile-only")', script)
        self.assertIn("if (!isMobile && trigger.hasAttribute", script)

    @override_settings(PWA_INSTALL_ENABLED=False)
    def test_the_visitor_trigger_disappears_with_the_feature_switch(self):
        response = self.client.get(reverse("reports:landing"))

        self.assertNotContains(response, "data-pwa-install-trigger")

    @override_settings(PWA_INSTALL_ENABLED=False)
    def test_install_metadata_is_not_advertised_when_pwa_install_is_disabled(self):
        response = self.client.get(reverse("reports:landing"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'rel="manifest"')
        self.assertNotContains(response, 'id="pwaInstallPrompt"')
        self.assertNotContains(response, "js/pwa-install.js")

    def test_all_interactive_standalone_templates_include_the_installer(self):
        templates = (
            "reports/templates/base.html",
            "reports/templates/reports/landing.html",
            "reports/templates/reports/login.html",
            "reports/templates/reports/register_school.html",
            "reports/templates/reports/registration_success.html",
            "reports/templates/reports/maintenance_mode.html",
            "reports/templates/reports/user_guide.html",
        )

        for template_path in templates:
            source = self._source(template_path)
            self.assertIn("reports/partials/pwa_install_head.html", source)
            self.assertIn("reports/partials/pwa_install.html", source)

    def test_installer_covers_native_android_and_manual_ios_paths(self):
        script = self._source("static/js/pwa-install.js")

        self.assertIn('window.addEventListener("beforeinstallprompt"', script)
        self.assertIn('window.addEventListener("appinstalled"', script)
        self.assertIn("isIPadOS", script)
        self.assertIn("إضافة إلى الشاشة الرئيسية", script)
        self.assertIn("تثبيت التطبيق", script)
        self.assertIn("window.localStorage", script)
        self.assertIn("window.sessionStorage", script)
        self.assertIn("INSTALLED_KEY", script)
        self.assertIn("SESSION_DISMISSED_KEY", script)
        self.assertIn("navigator.getInstalledRelatedApps", script)
        self.assertIn("navigator.userAgentData", script)
        self.assertIn("pwaInstallAnnouncement", script)
        self.assertIn("يتوفر تثبيت منصة توثيق على هذا الجوال", script)
        self.assertIn('var SW_URL = "/sw.js?v=12"', script)
        self.assertIn('updateViaCache: "none"', script)
        self.assertIn("TASK_COMPLETE_KEY", script)
        self.assertIn("hasCompletedTask()", script)
        self.assertIn('window.addEventListener("tawtheeq:task-complete"', script)
        self.assertIn('window.dispatchEvent(new CustomEvent("tawtheeq:pwa-update"', script)
        self.assertIn("autoPromptAllowed", script)
        self.assertIn("isIOS ? AUTO_IOS_DELAY_MS : AUTO_FALLBACK_DELAY_MS", script)
        self.assertIn("TawtheeqPWA", script)
        self.assertIn("data-pwa-install-trigger", script)
        self.assertIn("closeMobileDrawer", script)
        self.assertNotIn('event.key !== "Tab"', script)
        self.assertNotIn('document.body.style.overflow = "hidden"', script)
        self.assertNotIn("}, 900);", script)

    def test_installer_benefits_are_readable_useful_and_brand_driven(self):
        template = self._source("reports/templates/reports/partials/pwa_install.html")
        styles = self._source("static/css/pwa-install.css")

        for benefit in ("وصول بنقرة", "بدون كتابة الرابط", "تجربة كتطبيق"):
            self.assertIn(benefit, template)
        self.assertIn('role="status"', template)
        self.assertIn('aria-atomic="true"', template)
        self.assertIn("--pwa-brand: var(--brand", styles)
        self.assertIn("--pwa-accent: var(--accent", styles)
        self.assertIn("font-size: .82rem", styles)
        self.assertIn('.pwa-install__benefits span::before', styles)

    def test_automatic_prompt_is_scoped_to_authenticated_pages(self):
        template = self._source("reports/templates/reports/partials/pwa_install.html")

        self.assertIn("data-auto-prompt", template)
        self.assertIn("request.user.is_authenticated", template)
        self.assertIn("true", template)
        self.assertIn("false", template)

    def test_manifest_has_mobile_install_metadata(self):
        manifest = json.loads(self._source("static/manifest.json"))

        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/home/?source=pwa")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["lang"], "ar")
        self.assertEqual(manifest["dir"], "rtl")
        self.assertEqual(manifest["background_color"], "#F3F7F4")
        self.assertFalse(manifest["prefer_related_applications"])
        self.assertEqual(
            manifest["related_applications"],
            [{"platform": "webapp", "url": "/static/manifest.json"}],
        )
        self.assertNotEqual(manifest.get("orientation"), "portrait")
        self.assertEqual(
            {icon["sizes"] for icon in manifest["icons"]},
            {"192x192", "512x512"},
        )
        self.assertEqual({icon["purpose"] for icon in manifest["icons"]}, {"any", "maskable"})

        for icon in manifest["icons"]:
            relative_path = icon["src"].lstrip("/")
            declared = tuple(int(value) for value in icon["sizes"].split("x"))
            self.assertEqual(self._png_size(relative_path), declared)

    def test_installed_app_header_respects_the_status_bar_safe_area(self):
        # القاعدة كانت مكتوبة في ``<style>`` داخل ``base.html``؛ ونُقلت إلى
        # ``app-shell.css`` مع بقية هيكل التطبيق. والفحص يتبع القاعدة إلى
        # موضعها الجديد — فالمقصود سلوكُ الترويسة لا ملفُّها.
        shell = self._source("static/css/app-shell.css")

        self.assertIn(
            "@media (display-mode: standalone), (display-mode: fullscreen)",
            shell,
        )
        self.assertIn("height: calc(72px + var(--safe-top));", shell)
        self.assertIn("padding-top: var(--safe-top);", shell)

    def test_base_template_carries_no_stylesheet_of_its_own(self):
        """القالب يصف البنية، والأنماط في ملفاتها.

        كان ``base.html`` يحمل 928 سطراً من CSS داخل ``<style>``: تُعاد على كل
        طلب بلا كاش متصفح، ولا تصلها أي أداة تحليل، ويُجهل وجودُها أصلاً لأن
        الملف قالبٌ لا صفحةَ أنماط.
        """
        template = self._source("reports/templates/base.html")

        self.assertNotIn("<style", template)

    def test_service_worker_uses_private_safe_offline_strategy(self):
        worker = self._source("static/sw.js")
        offline = self._source("static/offline.html")

        self.assertIn('const CACHE_NAME = "tawtheeq-v12"', worker)
        self.assertIn('const OFFLINE_URL = "/static/offline.html"', worker)
        self.assertIn("navigationPreload.enable()", worker)
        self.assertIn('startsWith("/api/")', worker)
        self.assertIn("Promise.allSettled", worker)
        self.assertNotIn("Default: cache-first", worker)
        self.assertNotIn("cache.put(event.request", worker)
        self.assertIn("أنت الآن دون اتصال", offline)
        self.assertIn('name="viewport"', offline)
        self.assertIn("cairo-arabic.woff2", worker)
        self.assertIn("cairo-arabic.woff2", offline)

    def test_service_worker_response_prevents_browser_and_cdn_caching(self):
        response = self.client.get(reverse("service_worker"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["CDN-Cache-Control"], "no-store")
        self.assertEqual(response["Cloudflare-CDN-Cache-Control"], "no-store")
        self.assertEqual(response["Service-Worker-Allowed"], "/")

    def test_generated_splash_assets_cover_phones_and_tablets_both_orientations(self):
        splash_dir = Path(settings.BASE_DIR) / "static" / "img" / "pwa"
        splash_files = sorted(splash_dir.glob("splash-*.png"))

        self.assertEqual(len(splash_files), 24)
        self.assertTrue(any("iphone" in path.name for path in splash_files))
        self.assertTrue(any("ipad" in path.name for path in splash_files))
        self.assertEqual(
            {path.name.rsplit("-", 1)[-1] for path in splash_files},
            {"portrait.png", "landscape.png"},
        )
        for splash_file in splash_files:
            width, height = self._png_size(str(splash_file.relative_to(settings.BASE_DIR)))
            if splash_file.name.endswith("-portrait.png"):
                self.assertGreater(height, width)
            else:
                self.assertGreater(width, height)
