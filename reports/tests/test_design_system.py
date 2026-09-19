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
        canonical_tokens = {
            "--twq-primary",
            "--twq-primary-hover",
            "--twq-primary-active",
            "--twq-primary-soft",
            "--twq-secondary",
            "--twq-accent",
            "--twq-success",
            "--twq-success-soft",
            "--twq-warning",
            "--twq-danger",
            "--twq-info",
            "--twq-bg",
            "--twq-surface",
            "--twq-surface-secondary",
            "--twq-surface-elevated",
            "--twq-text",
            "--twq-text-secondary",
            "--twq-text-muted",
            "--twq-text-inverse",
            "--twq-border",
            "--twq-divider",
            "--twq-focus",
            "--twq-space-1",
            "--twq-space-12",
            "--twq-radius-sm",
            "--twq-radius-pill",
            "--twq-shadow-sm",
            "--twq-font-family",
            "--twq-font-size-2xl",
            "--twq-duration-fast",
            "--twq-duration-normal",
            "--twq-ease-standard",
        }
        compatibility_tokens = {
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

        for token in canonical_tokens | compatibility_tokens:
            with self.subTest(token=token):
                self.assertIn(f"{token}:", tokens)
        self.assertIn('html[data-theme="dark"]', tokens)
        self.assertIn("prefers-reduced-motion", tokens)

    def test_shared_component_api_uses_canonical_tokens_and_logical_properties(self):
        css = source("static/css/design-system.css")
        required_components = {
            ".twq-btn",
            ".twq-field",
            ".twq-control",
            ".twq-card",
            ".twq-status",
            ".twq-table-wrap",
            ".twq-table",
            ".twq-modal",
            ".twq-empty",
            ".twq-loading",
            ".twq-alert",
            ".twq-pagination",
            ".twq-page-header",
            ".twq-section__header",
            ".twq-progress__bar",
            ".twq-disclosure",
            ".twq-document-item",
        }

        for selector in required_components:
            with self.subTest(selector=selector):
                self.assertIn(selector, css)

        self.assertIn("var(--twq-primary)", css)
        self.assertIn("padding-inline", css)
        self.assertIn("border-inline-start", css)
        self.assertIn("@media (max-width: 40rem)", css)

    def test_design_language_v1_patterns_keep_native_semantics(self):
        design_system = source("static/css/design-system.css")
        list_template = source("reports/templates/reports/leadership_portfolio_list.html")
        detail_template = source("reports/templates/reports/leadership_portfolio_detail.html")

        for template in (list_template, detail_template):
            self.assertIn("twq-page-header", template)
            self.assertIn("twq-page-header__context", template)
            self.assertIn("twq-progress__bar", template)
            self.assertIn("twq-section__header", template)

        self.assertIn('<details class="lp-detail__axis twq-disclosure"', detail_template)
        self.assertIn('<summary class="twq-disclosure__summary">', detail_template)
        self.assertIn("twq-document-item", detail_template)
        self.assertIn("twq-empty--page", list_template)
        self.assertIn("twq-empty--local", detail_template)
        self.assertIn("{% load static hijri_tags %}", detail_template)
        self.assertGreaterEqual(detail_template.count("report.report_date|hijri"), 2)
        self.assertIn(
            'datetime="{{ evidence.report.report_date|date:\'Y-m-d\' }}"',
            detail_template,
        )
        self.assertIn("prefers-reduced-motion", design_system)
        self.assertNotRegex(design_system, r"transition:\s*all")

    def test_pilot_css_keeps_only_module_owned_composition(self):
        list_css = source("static/css/leadership-portfolio-list.css")
        detail_css = source("static/css/leadership-portfolio-detail.css")

        for css in (list_css, detail_css):
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", css))
            self.assertIsNone(
                re.search(
                    r"(?m)^\s*(?:margin|padding|border)-(?:left|right)\s*:"
                    r"|^\s*(?:left|right)\s*:",
                    css,
                )
            )

        self.assertNotIn(".lp-pilot__header {", list_css)
        self.assertNotIn(".lp-detail__header {", detail_css)
        self.assertNotIn(".lp-detail__report-item {", detail_css)

    def test_shared_pagination_keeps_legacy_contract_and_adds_foundation_hooks(self):
        pagination = source("reports/templates/reports/_pagination.html")

        self.assertIn('class="royal-pagination twq-pagination"', pagination)
        self.assertIn("twq-pagination__link", pagination)
        self.assertIn("twq-pagination__meta", pagination)
        self.assertIn("request.GET.items", pagination)

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

    def test_report_rows_expose_accessible_named_actions(self):
        reports_page = source("reports/templates/reports/my_reports.html")
        menu = source("reports/templates/reports/partials/report_actions_menu.html")

        self.assertEqual(reports_page.count("partials/report_actions_menu.html"), 2)
        self.assertNotIn("<details", menu)
        self.assertIn('class="report-action-list"', menu)
        self.assertIn('role="group"', menu)
        self.assertIn("report-action--view", menu)
        self.assertIn("report-action--danger", menu)
        for visible_label in ("عرض", "تعديل", "مشاركة", "حذف"):
            with self.subTest(visible_label=visible_label):
                self.assertIn(f"<span>{visible_label}</span>", menu)
        self.assertNotIn('target="_blank"', menu)

    def test_my_reports_pilot_uses_design_language_v1_without_changing_query_contracts(self):
        template = source("reports/templates/reports/my_reports.html")

        for contract in (
            "css/my-reports.css",
            "twq-page",
            "twq-page-header",
            "twq-metrics",
            "twq-toolbar",
            "twq-toolbar__search",
            "twq-toolbar__actions",
            "twq-toolbar__active-filters",
            "twq-filter-chip",
            "twq-section__header",
            "twq-table-wrap",
            "twq-empty--local",
            "twq-empty--page",
            'name="q"',
            'name="start_date"',
            'name="end_date"',
            'id="mrImageModal"',
            'id="mrModalClose"',
            'id="mrModalImg"',
            'id="mrPrevBtn"',
            'id="mrNextBtn"',
            'class="my-report-gallery js-gallery"',
            'data-report="{{ report.pk }}"',
            'reports/_pagination.html',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, template)

        self.assertEqual(template.count("partials/report_actions_menu.html"), 2)
        self.assertNotIn("<style", template)
        self.assertNotRegex(template, r"\sstyle\s*=")

    def test_my_reports_css_is_token_driven_rtl_safe_and_reduced_motion_aware(self):
        css = source("static/css/my-reports.css")
        css_without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

        self.assertIn("var(--twq-primary)", css)
        self.assertIn("@media (max-width: 64rem)", css)
        self.assertIn("@media (max-width: 48rem)", css)
        self.assertIn("@media (max-width: 40rem)", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", css))
        self.assertIsNone(
            re.search(
                r"(?m)^\s*(?:margin|padding|border)-(?:left|right)\s*:"
                r"|^\s*(?:left|right)\s*:",
                css,
            )
        )
        self.assertNotRegex(css, r"transition:\s*all")
        self.assertNotIn("!important", css_without_comments)

    def test_shared_toolbar_and_active_filters_are_canonical_components(self):
        css = source("static/css/design-system.css")

        for contract in (
            ".twq-toolbar",
            ".twq-toolbar__search",
            ".twq-toolbar__control",
            ".twq-toolbar__icon",
            ".twq-toolbar__actions",
            ".twq-toolbar__active-filters",
            ".twq-toolbar__active-label",
            ".twq-filter-chip",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, css)

        toolbar_css = css.split("/* Search and filter toolbar */", 1)[1].split("/* Cards */", 1)[0]
        self.assertNotRegex(toolbar_css, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
        self.assertNotRegex(
            toolbar_css,
            r"(?m)^\s*(?:margin|padding|border)-(?:left|right)\s*:"
            r"|^\s*(?:left|right)\s*:",
        )
        self.assertNotIn("!important", toolbar_css)

    def test_report_state_chip_adopts_the_shared_semantic_status(self):
        state_chip = source("reports/templates/reports/partials/report_state_chip.html")

        self.assertIn("twq-status", state_chip)
        self.assertIn('data-status="{{ report.approval_tone }}"', state_chip)
        self.assertIn("get_approval_state_display", state_chip)
        self.assertIn("approval_detail", state_chip)
