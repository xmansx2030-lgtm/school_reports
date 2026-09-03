from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse


def contrast_ratio(foreground: str, background: str) -> float:
    def relative_luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92
            if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted(
        (relative_luminance(foreground), relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


@override_settings(ALLOWED_HOSTS=["testserver"], SITE_URL="https://tawtheeq.example")
class DarkModeExperienceTests(TestCase):
    @staticmethod
    def _source(relative_path: str) -> str:
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    def test_public_pages_load_the_shared_theme_without_a_light_flash(self):
        for route_name in (
            "reports:landing",
            "reports:login",
            "reports:register_school",
            "reports:user_guide",
        ):
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))
                html = response.content.decode("utf-8")

                self.assertEqual(response.status_code, 200)
                self.assertIn('name="color-scheme" content="light dark"', html)
                self.assertIn("localStorage.getItem('theme')", html)
                self.assertIn("prefers-color-scheme: dark", html)
                self.assertIn("css/dark-mode.css", html)
                self.assertIn("js/theme-manager.js", html)
                self.assertLess(html.index("localStorage.getItem('theme')"), html.index("css/dark-mode.css"))

    def test_all_interactive_standalone_templates_share_the_theme_layer(self):
        templates = (
            "reports/templates/reports/landing.html",
            "reports/templates/reports/login.html",
            "reports/templates/reports/register_school.html",
            "reports/templates/reports/registration_success.html",
            "reports/templates/reports/maintenance_mode.html",
            "reports/templates/reports/password_reset_base.html",
            "reports/templates/reports/user_guide.html",
        )
        for template_path in templates:
            with self.subTest(template_path=template_path):
                source = self._source(template_path)
                self.assertIn('name="color-scheme" content="light dark"', source)
                self.assertIn('include "reports/partials/theme_bootstrap.html"', source)
                self.assertIn("css/dark-mode.css", source)
                self.assertIn("js/theme-manager.js", source)

    def test_shared_application_template_uses_the_single_theme_manager(self):
        source = self._source("reports/templates/base.html")

        self.assertIn('include "reports/partials/theme_bootstrap.html"', source)
        self.assertIn("css/dark-mode.css", source)
        self.assertIn("js/theme-manager.js", source)
        self.assertNotIn("// ===== Theme (data-theme) =====", source)

    def test_theme_manager_persists_and_exposes_accessible_state(self):
        javascript = self._source("static/js/theme-manager.js")

        self.assertIn("window.localStorage.setItem(storageKey, theme)", javascript)
        self.assertIn("max-age=31536000; SameSite=Lax", javascript)
        self.assertIn("prefers-color-scheme: dark", javascript)
        self.assertIn("media.addEventListener('change', followSystem)", javascript)
        self.assertIn("button.setAttribute('aria-pressed'", javascript)
        self.assertIn("تم تفعيل الوضع الليلي", javascript)
        self.assertIn("new CustomEvent('themechange'", javascript)

    def test_dark_styles_cover_core_controls_and_preserve_print_outputs(self):
        css = self._source("static/css/dark-mode.css")

        self.assertIn('html[data-theme="dark"] input', css)
        self.assertIn('html[data-theme="dark"] thead th', css)
        self.assertIn("input:-webkit-autofill", css)
        self.assertIn(".theme-toggle--floating", css)
        self.assertIn("@media print", css)

        for print_template in (
            "reports/templates/reports/report_print.html",
            "reports/templates/reports/ticket_print.html",
            "reports/templates/reports/notification_signatures_print.html",
        ):
            with self.subTest(print_template=print_template):
                source = self._source(print_template)
                self.assertNotIn("theme-manager.js", source)
                self.assertNotIn("dark-mode.css", source)

    def test_dark_styles_cover_shared_and_legacy_component_families(self):
        css = self._source("static/css/dark-mode.css")

        for selector in (
            '.hdr-nav .tab.is-active',
            '.btn-outline',
            '.badge.success',
            '.faq-cta',
            '.legal-card',
            '.pwa-install__card',
            '.mansour-panel',
            '.smart-card',
            '.add-report-page',
            '.req-scope',
            '.manager-subscription-alert',
            '.af-card',
            '.lp-work',
            '.plan-content',
            '.plans-page',
            '.sup-header',
            '.ay-card',
            '.exp-stat',
        ):
            with self.subTest(selector=selector):
                self.assertIn(f'html[data-theme="dark"] {selector}', css)

    def test_dark_palette_text_contrast_meets_wcag_aa(self):
        palette_pairs = {
            "body": ("#edf6f1", "#061512"),
            "muted": ("#abc0b6", "#0d241d"),
            "placeholder": ("#8fa79c", "#102920"),
            "secondary-action": ("#d9f4e5", "#102920"),
            "success": ("#9ce8bd", "#0d241d"),
            "warning": ("#f0d69d", "#0d241d"),
            "danger": ("#ffc5c8", "#0d241d"),
            "info": ("#bae6fd", "#0d241d"),
        }

        for role, (foreground, background) in palette_pairs.items():
            with self.subTest(role=role):
                self.assertGreaterEqual(
                    contrast_ratio(foreground, background),
                    4.5,
                )

    def test_dark_stylesheet_cache_version_is_consistent(self):
        expected_version = "dark-mode.css' %}?v=20260903.10"
        templates = (
            "reports/templates/base.html",
            "reports/templates/reports/landing.html",
            "reports/templates/reports/login.html",
            "reports/templates/reports/register_school.html",
            "reports/templates/reports/registration_success.html",
            "reports/templates/reports/maintenance_mode.html",
            "reports/templates/reports/password_reset_base.html",
            "reports/templates/reports/user_guide.html",
        )

        for template_path in templates:
            with self.subTest(template_path=template_path):
                self.assertIn(expected_version, self._source(template_path))
