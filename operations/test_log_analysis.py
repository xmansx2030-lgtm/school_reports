from unittest import TestCase

from .log_analysis import sanitize_and_analyze


class LogRedactionTests(TestCase):
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
