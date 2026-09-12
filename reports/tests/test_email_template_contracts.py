from django.template.loader import get_template
from django.test import SimpleTestCase

from reports.email_branding import render_branded_email


class DynamicEmailTemplateContractTests(SimpleTestCase):
    TEMPLATES = (
        "message.html",
        "subscription_expiry.html",
        "subscription_activated.html",
        "password_changed.html",
    )

    def test_dynamic_email_fragments_remain_resolvable(self):
        for name in self.TEMPLATES:
            with self.subTest(name=name):
                self.assertIsNotNone(get_template(f"reports/emails/{name}"))

    def test_dynamic_email_fragments_render_through_the_branding_boundary(self):
        for name in self.TEMPLATES:
            with self.subTest(name=name):
                html = render_branded_email(
                    name,
                    email_title="Contract title",
                    email_intro="Contract body",
                    body_html="Contract body",
                )
                self.assertIn("Contract title", html)
                self.assertIn('dir="rtl"', html)
