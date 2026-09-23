from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class AssignmentWorkflowUiTests(SimpleTestCase):
    templates = (
        "my_assignments.html",
        "assignment_board.html",
        "assignment_create.html",
        "assignment_view.html",
        "assignment_detail.html",
    )

    @staticmethod
    def _source(relative_path: str) -> str:
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    def test_core_surfaces_use_the_assignment_asset_and_no_embedded_styles(self):
        for name in self.templates:
            with self.subTest(template=name):
                source = self._source(f"reports/templates/reports/{name}")
                self.assertIn("css/assignments.css", source)
                self.assertNotIn("<style", source)
                self.assertNotIn(" style=", source)

    def test_interactive_surfaces_use_external_assignment_script(self):
        for name in ("assignment_create.html", "assignment_view.html", "assignment_detail.html"):
            with self.subTest(template=name):
                source = self._source(f"reports/templates/reports/{name}")
                self.assertIn("js/assignments.js", source)
                self.assertNotIn("<script nonce=", source)

    def test_create_has_an_accessible_server_error_summary(self):
        source = self._source("reports/templates/reports/assignment_create.html")
        self.assertIn('id="assignmentErrorSummary"', source)
        self.assertIn('role="alert"', source)
        self.assertIn('tabindex="-1"', source)
        self.assertIn('data-assignment-create', source)
        self.assertIn('summary.focus()', self._source("static/js/assignments.js"))

    def test_lists_use_v1_operational_patterns(self):
        source = "\n".join(
            self._source(f"reports/templates/reports/{name}")
            for name in ("my_assignments.html", "assignment_board.html")
        )
        for pattern in ("twq-page", "twq-page-header", "twq-section", "twq-status", "twq-empty"):
            self.assertIn(pattern, source)
        self.assertIn("twq-table", source)
        self.assertIn("twq-progress", source)

    def test_detail_keeps_post_csrf_mutations_and_action_eligibility(self):
        source = self._source("reports/templates/reports/assignment_detail.html")
        self.assertIn('method="post"', source)
        self.assertIn("{% csrf_token %}", source)
        self.assertIn("{% if actions %}", source)
        self.assertIn("data-assignment-approval", source)

    def test_assignment_css_uses_logical_properties_and_no_direct_hex_colors(self):
        source = self._source("static/css/assignments.css")
        self.assertIn("border-inline-start", source)
        self.assertIn("inset-inline-start", source)
        self.assertNotRegex(source, r"#[0-9a-fA-F]{3,8}\b")
