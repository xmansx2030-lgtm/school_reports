import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


ROOT = Path(settings.BASE_DIR)


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


class DesignSystemContractTests(SimpleTestCase):
    def test_base_loads_the_foundation_once_and_after_legacy_layers(self):
        template = source("reports/templates/base.html")

        self.assertEqual(template.count("css/design-system.css"), 1)
        self.assertEqual(template.count("js/ui-runtime.js"), 1)
        self.assertGreater(template.index("css/design-system.css"), template.index("css/dark-mode.css"))
        self.assertGreater(template.index("css/tokens.css"), template.index("css/dark-mode.css"))
        self.assertIn('dir="rtl"', template)
        self.assertIn('id="mainContent"', template)

    def test_base_children_do_not_nest_main_landmarks(self):
        templates_root = ROOT / "reports/templates/reports"
        offenders = []
        for path in templates_root.rglob("*.html"):
            template = path.read_text(encoding="utf-8")
            if re.search(r"{%\s*extends\s+['\"]base\.html['\"]", template) and re.search(
                r"<main\b", template, flags=re.IGNORECASE
            ):
                offenders.append(str(path.relative_to(ROOT)))

        self.assertEqual(offenders, [])

    def test_tokens_cover_the_semantic_and_layout_contract(self):
        tokens = source("static/css/tokens.css")
        required_tokens = {
            "--primary",
            "--secondary",
            "--accent",
            "--success",
            "--warning",
            "--danger",
            "--info",
            "--canvas",
            "--surface",
            "--surface-elevated",
            "--text-primary",
            "--text-secondary",
            "--text-muted",
            "--border",
            "--space-1",
            "--space-16",
            "--radius-md",
            "--shadow-sm",
            "--control-md",
            "--tap-target",
            "--duration-normal",
        }

        for token in required_tokens:
            with self.subTest(token=token):
                self.assertIn(f"{token}:", tokens)
        self.assertIn('html[data-theme="dark"]', tokens)
        self.assertIn("prefers-reduced-motion", tokens)

    def test_new_foundation_uses_no_important_overrides(self):
        for relative_path in (
            "static/css/tokens.css",
            "static/css/design-system.css",
            "static/css/standalone-system.css",
        ):
            with self.subTest(path=relative_path):
                css_without_comments = re.sub(r"/\*.*?\*/", "", source(relative_path), flags=re.DOTALL)
                self.assertNotIn("!important", css_without_comments)

    def test_foundation_covers_responsive_rtl_a11y_and_installed_pwa(self):
        css = source("static/css/design-system.css")

        for contract in (
            ":focus-visible",
            "inset-inline-start",
            "border-inline-start",
            "@media (max-width: 48rem)",
            "@media (display-mode: standalone)",
            "env(safe-area-inset-bottom)",
            "@media (prefers-reduced-motion: reduce)",
            "min-height: var(--control-md)",
            "min-height: var(--tap-target)",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, css)

    def test_interactive_standalone_pages_load_the_shared_foundation(self):
        templates = {
            "reports/templates/reports/login.html": "auth-page",
            "reports/templates/reports/register_school.html": "registration-page",
            "reports/templates/reports/registration_success.html": "registration-success-page",
            "reports/templates/reports/password_reset_base.html": "recovery-page",
            "reports/templates/reports/maintenance_mode.html": "maintenance-page",
            "reports/templates/reports/user_guide.html": "guide-page",
        }

        for relative_path, body_class in templates.items():
            page = source(relative_path)
            with self.subTest(path=relative_path):
                self.assertIn("css/tokens.css", page)
                self.assertIn("css/standalone-system.css", page)
                self.assertIn("js/ui-runtime.js", page)
                self.assertIn(f'<body class="{body_class}">', page)
                self.assertIn('class="skip-link"', page)
                self.assertRegex(page, r'<main\b[^>]*\bid="(?:mainContent|guideContent)"')

    def test_public_landing_uses_shared_tokens_and_runtime(self):
        landing = source("reports/templates/reports/landing.html")

        self.assertIn("css/tokens.css", landing)
        self.assertIn("js/ui-runtime.js", landing)
        self.assertGreater(landing.index("css/tokens.css"), landing.index("css/dark-mode.css"))

    def test_error_pages_use_the_shared_state_and_safe_actions(self):
        for relative_path in (
            "reports/templates/403.html",
            "reports/templates/404.html",
            "reports/templates/500.html",
            "reports/templates/reports/share_invalid.html",
        ):
            page = source(relative_path)
            with self.subTest(path=relative_path):
                self.assertIn("reports/partials/ui_state.html", page)
                self.assertNotIn("javascript:", page)

        state = source("reports/templates/reports/partials/ui_state.html")
        self.assertIn("data-page-reload", state)
        self.assertIn("data-history-back", state)
        self.assertIn('role="{{ role|default:\'status\' }}"', state)

    def test_runtime_preserves_submitter_values_and_enhances_accessibility(self):
        runtime = source("static/js/ui-runtime.js")

        self.assertIn("event.submitter", runtime)
        self.assertNotIn("submitter.disabled", runtime)
        self.assertIn('setAttribute("aria-invalid", "true")', runtime)
        self.assertIn('setAttribute("aria-current", "page")', runtime)
        self.assertIn('body.dataset.displayMode = standalone ? "standalone" : "browser"', runtime)
        self.assertIn('event.key !== "Escape"', runtime)

    def test_report_rows_use_one_accessible_actions_menu(self):
        reports_page = source("reports/templates/reports/my_reports.html")
        menu = source("reports/templates/reports/partials/report_actions_menu.html")

        self.assertEqual(reports_page.count("partials/report_actions_menu.html"), 2)
        self.assertNotIn('class="mr-iconbtn"', reports_page)
        self.assertIn("<details", menu)
        self.assertIn("<summary", menu)
        self.assertIn("ui-actions-menu__danger", menu)
