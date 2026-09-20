"""Document media signing contract without contacting an R2 bucket."""

import os
import subprocess
import sys
from textwrap import dedent

from django.conf import settings
from django.test import SimpleTestCase


class DocumentMediaContractTests(SimpleTestCase):
    def test_document_file_uses_private_signed_r2_url_when_configured(self):
        """A fresh process is needed: model storage is chosen at import time."""
        probe = dedent(
            """
            import django
            django.setup()

            from urllib.parse import parse_qs, urlsplit
            from django.conf import settings
            from reports.models import Document
            from reports.storage import R2MediaStorage

            storage = Document._meta.get_field("file").storage
            assert isinstance(storage, R2MediaStorage)
            assert settings.MEDIA_PUBLIC_ACCESS_ENABLED is False
            assert settings.AWS_QUERYSTRING_AUTH is True
            assert settings.AWS_DEFAULT_ACL is None
            assert not getattr(settings, "AWS_S3_CUSTOM_DOMAIN", None)

            # Presigning is local: no bucket/object request is made.
            url = storage.url("schools/synthetic/documents/probe.pdf")
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            assert parsed.hostname == "r2-contract.invalid"
            assert "synthetic-private-media-test" in parsed.path
            assert query.get("X-Amz-Signature")
            assert query.get("X-Amz-Credential")
            assert query.get("X-Amz-Expires") == [str(settings.MEDIA_SIGNED_URL_EXPIRE_SECONDS)]
            assert settings.MEDIA_SIGNED_URL_EXPIRE_SECONDS == settings.AWS_QUERYSTRING_EXPIRE
            """
        )
        environment = os.environ.copy()
        environment.update(
            {
                "DJANGO_SETTINGS_MODULE": "config.test_settings",
                "ENV": "test",
                "PRODUCTION_STRICT_MODE": "0",
                "R2_ACCESS_KEY_ID": "synthetic_access_key_only",
                "R2_SECRET_ACCESS_KEY": "synthetic_secret_key_only",
                "R2_BUCKET_NAME": "synthetic-private-media-test",
                "R2_ENDPOINT_URL": "https://r2-contract.invalid",
                "R2_PUBLIC_DOMAIN": "",
                "MEDIA_PUBLIC_ACCESS_ENABLED": "0",
                "AWS_QUERYSTRING_AUTH": "0",
                "AWS_QUERYSTRING_EXPIRE": "900",
            }
        )
        result = subprocess.run(  # noqa: S603 - fixed in-repo probe, no shell or user input
            [sys.executable, "-c", probe],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-1200:])
