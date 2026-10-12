import importlib.util
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


def collector_module():
    path = Path(__file__).resolve().parents[1] / "deploy" / "hetzner" / "collect_operations_inventory.py"
    spec = importlib.util.spec_from_file_location("inventory_collector_tested", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HostInventoryCollectorTests(TestCase):
    def setUp(self):
        self.collector = collector_module()

    def test_cpu_uses_previous_same_boot_interval(self):
        previous = json.dumps({"boot_id": "same-boot", "monotonic": 1000, "ticks": [1000, 500, 50]})

        def read(path, **kwargs):
            return "same-boot" if path.name == "boot_id" else previous

        with patch.object(Path, "read_text", read), patch.object(Path, "write_text"), \
                patch.object(self.collector.os, "chmod"), patch.object(self.collector.os, "replace"), \
                patch.object(self.collector.time, "monotonic", return_value=1300), \
                patch.object(self.collector, "_cpu_sample", return_value=(2000, 1200, 70)), \
                patch.object(self.collector.time, "sleep") as sleep:
            cpu, iowait, duration, source = self.collector._cpu_metrics()
        self.assertEqual((cpu, iowait, duration, source), (30, 2, 300, "interval"))
        sleep.assert_not_called()

    def test_reboot_invalidates_previous_cpu_interval(self):
        previous = json.dumps({"boot_id": "old-boot", "monotonic": 1000, "ticks": [1000, 500, 50]})

        def read(path, **kwargs):
            return "new-boot" if path.name == "boot_id" else previous

        with patch.object(Path, "read_text", read), patch.object(Path, "write_text"), \
                patch.object(self.collector.os, "chmod"), patch.object(self.collector.os, "replace"), \
                patch.object(self.collector.time, "monotonic", side_effect=[1300, 1301]), \
                patch.object(self.collector, "_cpu_sample", side_effect=[(100, 40, 5), (200, 100, 7)]), \
                patch.object(self.collector.time, "sleep"):
            cpu, iowait, duration, source = self.collector._cpu_metrics()
        self.assertEqual((cpu, iowait, duration, source), (40, 2, 1, "initial_sample"))

    def test_immutable_application_tag_wins_over_stale_release_file_and_proxy(self):
        app_sha = "b" * 40
        labels = {"com.docker.compose.project": "school-reports",
                  "com.docker.compose.project.working_dir": "/opt/school_reports"}
        rows = []
        for service, image in (("caddy", "caddy:2"), ("web", f"ghcr.io/owner/app:{app_sha}")):
            rows.append({"Name": f"/{service}", "Config": {"Image": image, "Labels": {
                **labels, "com.docker.compose.service": service,
            }}, "State": {"Status": "running", "Running": True}})
        with patch.object(self.collector, "_run", side_effect=["caddy-id\nweb-id", json.dumps(rows), ""]), \
                patch.object(self.collector, "_repository", return_value="owner/app"), \
                patch.object(Path, "read_text", return_value="a" * 40):
            result = self.collector._docker_inventory(8192 * 1024**2)
        self.assertEqual(result[0]["deployed_sha"], app_sha)
        self.assertEqual(result[0]["deployed_image"], f"ghcr.io/owner/app:{app_sha}")

