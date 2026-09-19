"""Markup contracts for the manager reports operational-list pilot."""

from django.conf import settings
from django.test import SimpleTestCase


class AdminReportsDesignTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.template = (settings.BASE_DIR / "reports/templates/reports/admin_reports.html").read_text(encoding="utf-8")
        cls.stylesheet = (settings.BASE_DIR / "static/css/admin-reports.css").read_text(encoding="utf-8")

    def test_page_uses_shared_language_with_page_scoped_styles(self):
        self.assertIn("css/admin-reports.css", self.template)
        self.assertNotIn("<style", self.template)
        for pattern in ("twq-page-header", "twq-toolbar", "twq-table", "twq-empty", "twq-pagination"):
            self.assertIn(pattern, self.template)
        self.assertIn(".repdash-wrap", self.stylesheet)

    def test_existing_filter_and_row_action_contracts_survive(self):
        for field in ('name="teacher_name"', 'name="start_date"', 'name="end_date"', 'name="category"'):
            self.assertIn(field, self.template)
        for hook in (
            'id="reportStartDate"',
            'id="reportEndDate"',
            'id="reportTeacherName"',
            'id="reportCategory"',
            'data-rd-action="details"',
            'data-pk="{{ r.pk }}"',
            'id="rd-data-{{ r.pk }}"',
            'id="rdDetailsDialog"',
        ):
            self.assertIn(hook, self.template)
        self.assertIn("{% csrf_token %}", self.template)
        self.assertIn("?next={{ request.get_full_path|urlencode }}", self.template)
        self.assertIn("{% if r.user_can_delete|default:can_delete %}", self.template)
        self.assertIn("{% if qs %}&{{ qs }}{% endif %}", self.template)

    def test_approval_state_has_matching_desktop_and_mobile_placement(self):
        self.assertIn('<th scope="col">حالة الاعتماد</th>', self.template)
        self.assertIn('class="repdash-approval-cell"', self.template)
        self.assertIn('class="rd-row rd-row--approval"', self.template)
        self.assertEqual(self.template.count('data-status="{{ r.approval_tone }}"'), 2)
        self.assertEqual(self.template.count('{{ r.get_approval_state_display }}'), 2)
        self.assertIn('var(--twq-status-recommended)', self.stylesheet)
