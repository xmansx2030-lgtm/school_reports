from __future__ import annotations

import os
import time
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase

from reports.cache_utils import redis_cache_lock


REDIS_INTEGRATION_ENABLED = bool(os.getenv("RUN_REDIS_INTEGRATION")) and (
    "django_redis" in settings.CACHES["default"]["BACKEND"]
)


@skipUnless(REDIS_INTEGRATION_ENABLED, "Redis integration only")
class RedisBehaviorTests(SimpleTestCase):
    def setUp(self) -> None:
        self.key = f"hardening-integration:{uuid4().hex}"

    def tearDown(self) -> None:
        cache.delete(self.key)

    def test_serialization_expiry_and_atomic_add(self) -> None:
        payload = {"school_id": 17, "roles": ["manager", "teacher"]}

        self.assertTrue(cache.add(self.key, payload, timeout=1))
        self.assertFalse(cache.add(self.key, {"replaced": True}, timeout=1))
        self.assertEqual(cache.get(self.key), payload)

        time.sleep(1.1)
        self.assertIsNone(cache.get(self.key))

    def test_distributed_lock_is_non_reentrant_and_released(self) -> None:
        with redis_cache_lock(self.key, timeout=10) as first:
            with redis_cache_lock(self.key, timeout=10) as second:
                self.assertTrue(first)
                self.assertFalse(second)

        with redis_cache_lock(self.key, timeout=10) as reacquired:
            self.assertTrue(reacquired)

    def test_lock_degrades_to_not_acquired_on_connection_error(self) -> None:
        with patch(
            "reports.cache_utils.cache.lock",
            side_effect=ConnectionError("isolated Redis unavailable"),
            create=True,
        ):
            with redis_cache_lock(self.key, timeout=10) as acquired:
                self.assertFalse(acquired)
