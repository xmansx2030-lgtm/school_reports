"""Validate the fixed host command boundary without touching Docker/systemd."""

from __future__ import annotations

import importlib.util
import json
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
            "parameters": {"since_minutes": 100000, "tail": 10000,
                           "since_at": "2026-10-05T12:00:00Z", "until": "2026-10-12T12:00:00Z"},
        }
        with patch.object(self.agent, "containers", return_value=[{
            "id": "safe-container-id", "name": "web-1", "service": "web", "running": True,
        }]), patch.object(self.agent, "run", return_value="error line") as run:
            result = self.agent.execute(job)
        self.assertIn("error line", result["log_content"])
        run.assert_called_once_with(
            "docker", "logs", "--timestamps", "--since", "2026-10-05T12:00:00Z",
            "--until", "2026-10-12T12:00:00Z", "--tail", "1001",
            "safe-container-id", timeout=30, include_stderr=True, max_output_bytes=1024 * 1024,
        )
        self.assertEqual(result["log_page"]["since_minutes"], 10080)

    def test_cli_options_cannot_be_passed_as_log_time(self):
        with patch.object(self.agent, "run") as run:
            with self.assertRaises(ValueError):
                self.agent.read_logs([{"id": "one", "name": "web"}], {"until": "--details"})
            run.assert_not_called()

    def test_log_page_keeps_newest_lines_and_orders_fractional_seconds(self):
        lines = [f"2026-10-12T12:00:00.{n:09d}Z line {n}" for n in range(51)]
        with patch.object(self.agent, "run", return_value="\n".join(reversed(lines))):
            result = self.agent.read_logs([{"id": "one", "name": "web"}], {"tail": 50})
        self.assertEqual(result["log_page"]["returned_lines"], 50)
        self.assertEqual(result["log_page"]["next_until"], "2026-10-12T12:00:00.000000000Z")
        self.assertTrue(result["log_page"]["has_more"])
        self.assertLess(self.agent.timestamp_key("2026-10-12T12:00:00Z"), self.agent.timestamp_key("2026-10-12T12:00:00.1Z"))

    def test_nanosecond_cursor_rollover_padding_and_backend_parity(self):
        from operations.log_cursors import previous_timestamp

        examples = (
            ("2026-10-12T12:00:00.000000001Z", "2026-10-12T12:00:00.000000000Z"),
            ("2026-10-12T12:00:00.1Z", "2026-10-12T12:00:00.099999999Z"),
            ("2026-10-12T12:00:00.123456789Z", "2026-10-12T12:00:00.123456788Z"),
            ("2026-10-12T12:00:00Z", "2026-10-12T11:59:59.999999999Z"),
            ("2026-10-12T00:00:00.000000000Z", "2026-10-11T23:59:59.999999999Z"),
            ("2026-01-01T00:00:00.0Z", "2025-12-31T23:59:59.999999999Z"),
        )
        for stamp, expected in examples:
            with self.subTest(stamp=stamp):
                self.assertEqual(previous_timestamp(stamp), expected)
                self.assertEqual(self.agent.previous_timestamp(stamp), expected)

    def test_inclusive_docker_until_pages_do_not_repeat_boundary_line(self):
        all_lines = [f"2026-10-12T12:00:00.{n:09d}Z event-{n}" for n in range(100)]

        def inclusive_logs(*args, **kwargs):
            until = args[args.index("--until") + 1]
            tail = int(args[args.index("--tail") + 1])
            rows = [line for line in all_lines if self.agent.timestamp_key(line.split()[0]) <= self.agent.timestamp_key(until)]
            return "\n".join(rows[-tail:])

        selected = [{"id": "one", "name": "backend"}]
        params = {"tail": 50, "since_at": "2026-10-12T11:00:00Z", "until": "2026-10-12T13:00:00Z"}
        with patch.object(self.agent, "run", side_effect=inclusive_logs):
            first = self.agent.read_logs(selected, params)
            second = self.agent.read_logs(selected, {**params, "until": first["log_page"]["next_until"]})
        self.assertLess(self.agent.timestamp_key(second["log_page"]["latest_at"]),
                        self.agent.timestamp_key(first["log_page"]["earliest_at"]))
        self.assertEqual(len(set(first["log_content"].splitlines()) & set(second["log_content"].splitlines())), 0)

    def test_large_raw_log_capture_fails_without_returning_partial_secret(self):
        with patch.object(self.agent, "DEPLOY_PATH", Path.cwd()):
            with self.assertRaises(self.agent.LogOutputLimitExceeded) as error:
                self.agent.run(
                    sys.executable, "-c", "import sys; sys.stdout.write('private-suffix-' * 10000)",
                    timeout=5, include_stderr=True, max_output_bytes=1000,
                )
        self.assertNotIn("private-suffix", str(error.exception))

    def test_bounded_capture_merges_complete_stdout_and_stderr(self):
        with patch.object(self.agent, "DEPLOY_PATH", Path.cwd()):
            result = self.agent.run(
                sys.executable, "-c", "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr)",
                timeout=5, include_stderr=True, max_output_bytes=1000,
            )
        self.assertIn("stdout-line", result)
        self.assertIn("stderr-line", result)

    def test_bounded_capture_stops_a_stalled_process(self):
        with patch.object(self.agent, "DEPLOY_PATH", Path.cwd()):
            with self.assertRaises(self.agent.subprocess.TimeoutExpired):
                self.agent.run(
                    sys.executable, "-c", "import time; time.sleep(10)",
                    timeout=0.1, include_stderr=True, max_output_bytes=1000,
                )

    def test_log_page_warns_about_identical_boundary_timestamps(self):
        lines = ["2026-10-12T12:00:00Z older"] + ["2026-10-12T12:00:00.000Z same-time" for _ in range(50)]
        with patch.object(self.agent, "run", return_value="\n".join(lines)):
            result = self.agent.read_logs([{"id": "one", "name": "web"}], {"tail": 50})
        self.assertTrue(result["log_page"]["boundary_collision"])
        self.assertIn("تكرر وقت", result["log_page"]["retention_note"])

    def test_log_page_discloses_replica_limit(self):
        selected = [{"id": str(index), "name": f"web-{index}"} for index in range(5)]
        with patch.object(self.agent, "run", return_value="2026-10-12T12:00:00Z event") as run:
            result = self.agent.read_logs(selected, {"tail": 50})
        self.assertEqual(run.call_count, 4)
        self.assertEqual(result["log_page"]["omitted_container_count"], 1)
        self.assertTrue(result["log_page"]["truncated"])
        self.assertIn("أول أربع", result["log_page"]["retention_note"])

    def test_diagnostics_never_returns_environment_or_raw_health_output(self):
        row = {"Id": "one", "Name": "/backend", "Config": {"Env": ["PRIVATE=secret-value"],
               "Labels": {"com.docker.compose.service": "backend"}},
               "State": {"Running": True, "Status": "running", "Health": {"Status": "healthy", "Log": ["secret-value"]}},
               "HostConfig": {"Memory": 500000000}}
        with patch.object(self.agent, "run", side_effect=["one", json.dumps([row]), "", "Result=success"]):
            result = self.agent.execute({"action": "diagnostics", "project_slug": "mowadhabah", "compose_project": "mowadhabah"})
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertEqual(result["diagnostics"]["running"], 1)

    def test_backup_allowlist_matches_backend_policy(self):
        from operations.action_policy import BACKUP_UNITS
        self.assertEqual(self.agent.BACKUP_UNITS, BACKUP_UNITS)

    def test_mowadhabah_backend_restart_waits_for_health(self):
        row = {"id": "backend-id", "service": "backend", "running": True, "health": "healthy"}
        with patch.object(self.agent, "containers", return_value=[row]), patch.object(self.agent, "run") as run:
            result = self.agent.execute({"action": "restart_service", "project_slug": "mowadhabah",
                                        "compose_project": "mowadhabah", "service_key": "backend", "service_kind": "web"})
        run.assert_called_once_with("docker", "restart", "--time", "30", "backend-id", timeout=90)
        self.assertIn("summary", result)

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
