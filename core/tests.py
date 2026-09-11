from __future__ import annotations

import re
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from config.settings import _media_querystring_auth_enabled, _validated_site_url
from core.client_ip import client_ip, client_ip_for_ratelimit
from core.views import healthz, ops_metrics


class SiteUrlValidationTests(SimpleTestCase):
    def test_production_rejects_localhost_as_canonical_origin(self):
        for value in (
            "http://localhost:8000",
            "https://127.0.0.1",
            "https://preview.localhost",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    _validated_site_url(value, environment="production")

    def test_production_accepts_public_https_origin(self):
        self.assertEqual(
            _validated_site_url(
                "https://tawtheeq-ksa.com/", environment="production"
            ),
            "https://tawtheeq-ksa.com",
        )


class HealthzTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/healthz/")

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    )
    def test_healthz_skips_channel_probe_by_default(self):
        with patch("core.views.os.getenv", return_value=""):
            with patch("channels.layers.get_channel_layer") as get_layer:
                response = healthz(self.request)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"channels": "skipped"', response.content)
        self.assertNotIn(b'"instance"', response.content)
        self.assertIn("no-store", response.headers["Cache-Control"])
        get_layer.assert_not_called()

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    )
    def test_healthz_does_not_disclose_backend_exception(self):
        with patch("django.db.backends.utils.CursorWrapper.execute", side_effect=RuntimeError("database-secret")):
            response = healthz(self.request)

        self.assertEqual(response.status_code, 503)
        self.assertIn(b'"db": "error"', response.content)
        self.assertNotIn(b"database-secret", response.content)


class ClientIpTests(SimpleTestCase):
    @override_settings(TRUSTED_PROXY_CIDRS=["172.16.0.0/12"])
    def test_trusts_real_ip_from_internal_proxy(self):
        request = SimpleNamespace(META={
            "REMOTE_ADDR": "172.18.0.4",
            "HTTP_X_REAL_IP": "203.0.113.15",
        })
        self.assertEqual(client_ip_for_ratelimit(request), "203.0.113.15")

    @override_settings(TRUSTED_PROXY_CIDRS=["172.16.0.0/12"])
    def test_ignores_spoofed_real_ip_from_untrusted_peer(self):
        request = SimpleNamespace(META={
            "REMOTE_ADDR": "198.51.100.22",
            "HTTP_X_REAL_IP": "203.0.113.15",
        })
        self.assertEqual(client_ip_for_ratelimit(request), "198.51.100.22")

    @override_settings(TRUSTED_PROXY_CIDRS=["172.16.0.0/12"])
    def test_audit_address_ignores_untrusted_forwarded_for(self):
        request = SimpleNamespace(META={
            "REMOTE_ADDR": "198.51.100.22",
            "HTTP_X_FORWARDED_FOR": "203.0.113.15",
        })
        self.assertEqual(client_ip(request), "198.51.100.22")

    def test_audit_address_uses_none_but_ratelimit_keeps_stable_fallback(self):
        request = SimpleNamespace(META={"REMOTE_ADDR": "not-an-ip"})
        self.assertIsNone(client_ip(request))
        self.assertEqual(client_ip_for_ratelimit(request), "0.0.0.0")  # noqa: S104 - sentinel, not a bind.


class OpsMetricsAuthorizationTests(SimpleTestCase):
    def test_staff_user_cannot_read_operational_metrics(self):
        request = RequestFactory().get("/ops/metrics/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_staff=True,
            is_superuser=False,
        )
        response = ops_metrics(request)
        self.assertEqual(response.status_code, 403)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    SECURITY_CONTACT_EMAIL="security@example.test",
    SITE_URL="https://canonical.example.test",
)
class PublicMetadataTests(SimpleTestCase):
    def test_sitemap_uses_canonical_host_and_only_indexable_routes(self):
        response = self.client.get("/sitemap.xml")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        for route_name in (
            "reports:landing",
            "reports:user_guide",
            "reports:privacy_policy",
            "reports:terms_conditions",
            "reports:refund_policy",
            "reports:service_delivery_policy",
            "reports:complaints_policy",
            "reports:faq",
        ):
            self.assertIn(
                f"https://canonical.example.test{reverse(route_name)}",
                body,
            )
        self.assertNotIn(reverse("reports:login"), body)
        self.assertIn("<changefreq>weekly</changefreq>", body)
        self.assertIn("<priority>1.0</priority>", body)
        self.assertNotIn("/user-guide/", body)
        self.assertNotIn("/privacy-policy/", body)

    def test_robots_advertises_sitemap_and_blocks_non_content_endpoints(self):
        response = self.client.get("/robots.txt")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Allow: /", body)
        self.assertIn("Disallow: /api/", body)
        self.assertIn(
            "Sitemap: https://canonical.example.test/sitemap.xml",
            body,
        )

    @override_settings(
        CANONICAL_HOST_REDIRECT=True,
        ALLOWED_HOSTS=["legacy.example.test", "canonical.example.test"],
    )
    def test_legacy_host_permanently_redirects_to_canonical_origin(self):
        response = self.client.get(
            "/faq/?source=legacy",
            HTTP_HOST="legacy.example.test",
        )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response.headers["Location"],
            "https://canonical.example.test/faq/?source=legacy",
        )

    def test_security_txt_uses_configured_contact_and_privacy_route(self):
        response = self.client.get("/.well-known/security.txt")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Contact: mailto:security@example.test", body)
        self.assertIn(f"Policy: http://testserver{reverse('reports:privacy_policy')}", body)
        self.assertNotIn("support@example.com", body)
        # RFC 9116 يوجب حقل Expires بصيغة زمنية صالحة في المستقبل.
        expires_line = next(line for line in body.splitlines() if line.startswith("Expires: "))
        expires_at = datetime.strptime(expires_line[len("Expires: "):], "%Y-%m-%dT%H:%M:%SZ")
        self.assertGreater(expires_at.replace(tzinfo=dt_timezone.utc), timezone.now())


class ContentSecurityPolicyTemplateTests(SimpleTestCase):
    INLINE_SCRIPT_TAG_RE = re.compile(
        r"<script\b(?![^>]*\bsrc\s*=)[^>]*>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    def test_inline_template_scripts_have_csp_nonce(self):
        templates_dir = settings.BASE_DIR / "reports" / "templates"
        missing_nonce = []

        for template_path in templates_dir.rglob("*.html"):
            text = template_path.read_text(encoding="utf-8")
            for match in self.INLINE_SCRIPT_TAG_RE.finditer(text):
                if "nonce=" not in match.group(0):
                    line = text.count("\n", 0, match.start()) + 1
                    missing_nonce.append(
                        f"{template_path.relative_to(settings.BASE_DIR)}:{line}"
                    )

        self.assertEqual(missing_nonce, [], msg=f"Inline scripts missing CSP nonce: {missing_nonce}")

    def test_inline_template_scripts_bypass_rocket_loader(self):
        templates_dir = settings.BASE_DIR / "reports" / "templates"
        missing_bypass = []

        for template_path in templates_dir.rglob("*.html"):
            text = template_path.read_text(encoding="utf-8")
            for match in self.INLINE_SCRIPT_TAG_RE.finditer(text):
                if 'data-cfasync="false"' not in match.group(0):
                    line = text.count("\n", 0, match.start()) + 1
                    missing_bypass.append(
                        f"{template_path.relative_to(settings.BASE_DIR)}:{line}"
                    )

        self.assertEqual(
            missing_bypass,
            [],
            msg=f"Inline scripts exposed to Rocket Loader: {missing_bypass}",
        )

    @override_settings(
        ALLOWED_HOSTS=["testserver"],
        CSP_ENABLED=True,
        CSP_REPORT_ONLY=False,
        MOYASAR_ENABLED=True,
        CONTENT_SECURITY_POLICY=(
            "default-src 'self'; "
            "script-src 'self'; "
            "script-src-elem 'none'; "
            "style-src 'self' 'unsafe-inline'"
        ),
    )
    def test_custom_csp_is_hardened_with_the_rendered_nonce(self):
        response = self.client.get(reverse("reports:login"))

        self.assertEqual(response.status_code, 200)
        policy = response.headers["Content-Security-Policy"]
        header_nonce_match = re.search(r"'nonce-([^']+)'", policy)
        html_nonce_match = re.search(
            rb'<script[^>]+\bnonce="([^"]+)"',
            response.content,
        )
        self.assertIsNotNone(header_nonce_match)
        self.assertIsNotNone(html_nonce_match)
        header_nonce = header_nonce_match.group(1)
        self.assertEqual(header_nonce.encode(), html_nonce_match.group(1))
        self.assertIn(f"script-src 'self' 'nonce-{header_nonce}'", policy)
        self.assertIn(
            f"script-src-elem 'nonce-{header_nonce}'",
            policy,
        )
        self.assertNotIn("script-src-elem 'none'", policy)
        # ما يحرسه هذا السطر أن form-action تبقى مقيّدة بـ 'self' وبوابات الدفع
        # المفعّلة وحدها. تثبيت السلسلة كاملةً كان يُسقطه عند تفعيل بوابة جديدة —
        # وهو تغيير مشروع لا انحدار.
        form_action = next(
            part.strip()
            for part in policy.split(";")
            if part.strip().startswith("form-action")
        )
        sources = form_action.split()[1:]
        self.assertEqual(sources[0], "'self'")
        self.assertIn("https://checkout.moyasar.com", sources)
        self.assertTrue(
            all(source == "'self'" or source.startswith("https://") for source in sources),
            f"مصدر غير آمن في form-action: {form_action}",
        )


class PrivateMediaSettingsTests(SimpleTestCase):
    def test_private_mode_forces_signed_urls(self):
        self.assertTrue(
            _media_querystring_auth_enabled(
                public_access_enabled=False,
                requested_querystring_auth=False,
            )
        )

    def test_public_mode_honors_explicit_querystring_choice(self):
        self.assertFalse(
            _media_querystring_auth_enabled(
                public_access_enabled=True,
                requested_querystring_auth=False,
            )
        )
        self.assertTrue(
            _media_querystring_auth_enabled(
                public_access_enabled=True,
                requested_querystring_auth=True,
            )
        )
