import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class NotificationAlertExperienceTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project_root = Path(settings.BASE_DIR)

    def _frontend_sources(self):
        roots = (
            self.project_root / "reports" / "templates",
            self.project_root / "static" / "js",
        )
        for root in roots:
            for pattern in ("*.html", "*.js"):
                for path in root.rglob(pattern):
                    yield path, path.read_text(encoding="utf-8")

    def test_frontend_has_no_native_alert_confirm_or_prompt_calls(self):
        native_dialog = re.compile(
            r"(?<![\w.])(?:window\.)?(?:alert|confirm|prompt)\s*\(",
            re.IGNORECASE,
        )
        offenders = [
            str(path.relative_to(self.project_root))
            for path, source in self._frontend_sources()
            if native_dialog.search(source)
        ]
        self.assertEqual(offenders, [])

    def test_templates_have_no_inline_event_attributes(self):
        inline_event = re.compile(
            r"\son(?:click|submit|change|input)\s*=",
            re.IGNORECASE,
        )
        offenders = [
            str(path.relative_to(self.project_root))
            for path, source in self._frontend_sources()
            if path.suffix == ".html" and inline_event.search(source)
        ]
        self.assertEqual(offenders, [])

    def test_unified_confirm_handles_submitter_and_keyboard_focus(self):
        source = (self.project_root / "reports" / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("submitter.matches('[data-confirm]')", source)
        self.assertIn("source.getAttribute('data-reason-prompt')", source)
        self.assertIn("source.getAttribute('data-refund-prompt')", source)
        self.assertIn("document.activeElement === btnOk", source)
        self.assertIn("input.disabled = currentMode !== 'prompt'", source)
        self.assertNotIn("if(e.key === 'Enter') close(true)", source)

    def test_install_prompt_and_home_notice_are_non_modal(self):
        pwa = (
            self.project_root
            / "reports"
            / "templates"
            / "reports"
            / "partials"
            / "pwa_install.html"
        ).read_text(encoding="utf-8")
        home = (
            self.project_root / "reports" / "templates" / "reports" / "home.html"
        ).read_text(encoding="utf-8")
        self.assertIn('role="region"', pwa)
        self.assertNotIn('aria-modal="true"', pwa)
        self.assertIn('id="homeNotification" role="region"', home)
        self.assertNotIn("data-mark-url", home)

        pwa_script = (
            self.project_root / "static" / "js" / "pwa-install.js"
        ).read_text(encoding="utf-8")
        self.assertIn("var SESSION_DISMISSED_KEY", pwa_script)
        self.assertIn("rememberSessionDismissal", pwa_script)

    def test_flash_messages_use_one_rtl_safe_floating_layer(self):
        base = (
            self.project_root / "reports" / "templates" / "base.html"
        ).read_text(encoding="utf-8")
        styles = (self.project_root / "static" / "css" / "design-system.css").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="messages-container"', base)
        self.assertIn('aria-label="رسائل النظام"', base)
        self.assertIn("window.showAppToast", base)
        self.assertIn("data-flash-message", base)

        block = styles.split("body.page .messages {", 1)[1].split("}", 1)[0]
        self.assertIn("inset-inline-start", block)
        self.assertIn("inset-inline-end: auto;", block)
        self.assertIn("inset-block-start: calc(var(--header-bottom", block)
        self.assertIn("pointer-events: none;", block)
        self.assertNotIn("inset-inline-end: max(var(--space-4)", styles)
        self.assertNotIn("inset-block-end: calc(4.5rem + env(safe-area-inset-bottom))", styles)

        skip_block = styles.split("body.page .skip-link {", 1)[1].split("}", 1)[0]
        progress_block = styles.split("body.page .progress-bar {", 1)[1].split("}", 1)[0]
        self.assertIn("position: absolute;", skip_block)
        self.assertIn("transform: translateY(-140%);", skip_block)
        self.assertIn("position: fixed;", progress_block)

    def test_legacy_message_rules_do_not_force_one_visual_treatment(self):
        for relative_path in ("static/css/app.css", "static/css/royal-theme.css"):
            with self.subTest(stylesheet=relative_path):
                source = (self.project_root / relative_path).read_text(encoding="utf-8")
                block = source.rsplit(".msg {", 1)[1].split("}", 1)[0]
                self.assertIn("border-inline-start: 4px solid var(--accent);", block)
                self.assertNotIn("!important", block)
                self.assertNotIn("border-right-width", block)

    def test_internal_pages_do_not_render_django_messages_a_second_time(self):
        for relative_path in (
            "reports/templates/reports/edit_report.html",
            "reports/templates/reports/edit_teacher.html",
        ):
            with self.subTest(template=relative_path):
                source = (self.project_root / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("for message in messages", source)
                self.assertNotIn("for m in messages", source)
                self.assertNotIn('id="flash"', source)

    def test_page_specific_toasts_delegate_to_the_unified_layer(self):
        subscription = (
            self.project_root
            / "reports"
            / "templates"
            / "reports"
            / "my_subscription.html"
        ).read_text(encoding="utf-8")
        meeting_copy = (
            self.project_root
            / "reports"
            / "templates"
            / "reports"
            / "partials"
            / "_meeting_copy_link.html"
        ).read_text(encoding="utf-8")

        self.assertIn("window.showAppToast(msg, type)", subscription)
        self.assertNotIn("toastContainer", subscription)
        self.assertIn("window.showAppToast(message, 'success')", meeting_copy)
        self.assertNotIn("mtg-copy-toast", meeting_copy)

    def test_standalone_auth_and_registration_alerts_are_accessible(self):
        login = (
            self.project_root / "reports" / "templates" / "reports" / "login.html"
        ).read_text(encoding="utf-8")
        register = (
            self.project_root
            / "reports"
            / "templates"
            / "reports"
            / "register_school.html"
        ).read_text(encoding="utf-8")
        standalone = (
            self.project_root / "static" / "css" / "standalone-system.css"
        ).read_text(encoding="utf-8")

        self.assertNotIn("Auto-hide messages", login)
        self.assertIn('role="alert"', login)
        self.assertIn('role="region" aria-label="رسائل النظام"', register)
        self.assertIn("body.auth-page .messages-area", standalone)
        self.assertIn("body.registration-page .messages li", standalone)
