import json
from unittest import TestCase

from .log_analysis import sanitize_and_analyze


class LogRedactionTests(TestCase):
    def test_json_escaped_quotes_do_not_leave_password_suffix(self):
        content, _ = sanitize_and_analyze(json.dumps({"password": 'left"right-secret', "message": "safe"}))
        self.assertNotIn("left", content)
        self.assertNotIn("right-secret", content)
        self.assertIn("safe", content)

    def test_cookie_headers_hide_all_credentials_and_json_escapes(self):
        for raw in (
            "Cookie: sessionid=demo-session; csrftoken=demo-csrf; custom_token=demo-auth",
            "Set-Cookie: arbitrary=demo-auth; Path=/; HttpOnly",
            json.dumps({"Cookie": 'custom=demo-auth; token=quoted"value', "message": "safe"}),
        ):
            content, _ = sanitize_and_analyze(raw)
            for secret in ("demo-session", "demo-csrf", "demo-auth", "quoted", "value"):
                self.assertNotIn(secret, content)

    def test_json_quoted_values_and_database_urls_are_hidden(self):
        raw = (
            'ERROR {"password": "two word secret", "api_key":"abc123"}\n'
            'postgresql://reporter:database-pass@db.internal/reports?password=query-pass\n'
            'Authorization: Bearer bearer-secret'
        )
        content, analysis = sanitize_and_analyze(raw)
        for secret in ("two word secret", "abc123", "database-pass", "query-pass", "bearer-secret"):
            self.assertNotIn(secret, content)
        self.assertEqual(analysis["errors"], 1)

    def test_multiline_private_key_is_not_displayed(self):
        content, _ = sanitize_and_analyze(
            "-----BEGIN PRIVATE KEY-----\nSECRETKEYLINE\n-----END PRIVATE KEY-----\nnormal line"
        )
        self.assertNotIn("SECRETKEYLINE", content)
        self.assertIn("normal line", content)

    def test_redaction_happens_before_long_line_is_shortened(self):
        raw = 'ERROR password="' + "private-value " * 500 + '" end'
        content, analysis = sanitize_and_analyze(raw)
        self.assertNotIn("private-value", content)
        self.assertIn("[REDACTED]", content)
        self.assertTrue(analysis["truncated"])

    def test_byte_limit_reports_omitted_lines_and_preserves_latest_records(self):
        content, analysis = sanitize_and_analyze("\n".join(f"line {n} " + "long message " * 150 for n in range(1000)))
        self.assertLessEqual(len(content), 200000)
        self.assertGreater(analysis["omitted_lines"], 800)
        self.assertIn("line 999", content)

    def test_tail_starting_inside_pem_retains_safe_timestamp(self):
        stamp = "2026-10-12T12:00:00.000000001Z"
        content, _ = sanitize_and_analyze(stamp + " [web] " + "AbCd1234" * 8)
        self.assertNotIn("AbCd1234", content)
        self.assertTrue(content.startswith(stamp))
