"""Validate the fixed host command boundary without touching Docker/systemd."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


def _module():
    path = Path(__file__).resolve().parents[1] / "deploy" / "hetzner" / "operations_host_agent.py"
    spec = importlib.util.spec_from_file_location("operations_host_agent_tested", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Host agent source could not be loaded")
    module = importlib.util.module_from_spec(spec)
    try:
        import fcntl  # noqa: F401, PLC0415
    except ImportError:
        with patch.dict(sys.modules, {"fcntl": types.ModuleType("fcntl")}):
            spec.loader.exec_module(module)
    else:
        spec.loader.exec_module(module)
    return module


class HostAgentCommandTests(TestCase):
    def setUp(self):
        self.agent = _module()

    def test_unknown_project_cannot_execute_a_host_command(self):
        with patch.object(self.agent, "run") as run:
            with self.assertRaises(ValueError):
                self.agent.execute({
                    "action": "restart_service",
                    "project_slug": "unknown",
                    "compose_project": "other",
                    "service_key": "web",
                })
            run.assert_not_called()

    def test_logs_read_only_matching_service_and_bounded_options(self):
        job = {
            "action": "read_logs",
            "project_slug": "tawtheeq",
            "compose_project": "school_reports",
            "service_key": "web",
            "parameters": {"since_minutes": 10000, "tail": 10000},
        }
        with patch.object(self.agent, "containers", return_value=[{
            "id": "safe-container-id", "name": "web-1", "service": "web", "running": True,
        }]), patch.object(self.agent, "run", return_value="error line") as run:
            result = self.agent.execute(job)
        self.assertIn("error line", result["log_content"])
        run.assert_called_once_with(
            "docker", "logs", "--timestamps", "--since", "180m", "--tail", "250",
            "safe-container-id", timeout=30, include_stderr=True,
        )

    def test_proxy_reload_validates_before_reloading(self):
        with patch.object(self.agent, "run") as run:
            self.agent.execute({
                "action": "reload_proxy",
                "project_slug": "tawtheeq",
                "compose_project": "school_reports",
            })
        self.assertEqual(run.call_count, 2)
        self.assertIn("validate", run.call_args_list[0].args)
        self.assertIn("reload", run.call_args_list[1].args)
