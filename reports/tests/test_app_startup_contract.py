from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from reports.apps import ReportsConfig


class ReportsAppStartupContractTests(SimpleTestCase):
    def test_ready_connects_storage_and_file_lifecycle_receivers(self) -> None:
        config = ReportsConfig("reports", __import__("reports"))

        with (
            patch("reports.storage_tracking.connect_all") as connect_storage,
            patch("reports.file_cleanup.connect_all") as connect_cleanup,
        ):
            config.ready()

        connect_storage.assert_called_once_with()
        connect_cleanup.assert_called_once_with()

    def test_ready_does_not_hide_a_broken_critical_receiver(self) -> None:
        config = ReportsConfig("reports", __import__("reports"))

        with (
            patch(
                "reports.storage_tracking.connect_all",
                side_effect=RuntimeError("receiver registration failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "receiver registration failed"),
        ):
            config.ready()
