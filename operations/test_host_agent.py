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
        }]), patch.object(self.agent, "stream_log_lines", return_value=(["error line"], 10, False, False)) as stream:
            result = self.agent.execute(job)
        self.assertIn("error line", result["log_content"])
        stream.assert_called_once_with((
            "docker", "logs", "--timestamps", "--since", "2026-10-05T12:00:00Z",
            "--until", "2026-10-12T12:00:00Z", "safe-container-id",
        ), tail=1001, scan_budget=64 * 1024 * 1024, timeout=30)
        self.assertEqual(result["log_page"]["since_minutes"], 10080)

    def test_cli_options_cannot_be_passed_as_log_time(self):
        with patch.object(self.agent, "stream_log_lines") as stream:
            with self.assertRaises(ValueError):
                self.agent.read_logs([{"id": "one", "name": "web"}], {"until": "--details"})
            stream.assert_not_called()

    def test_log_page_keeps_newest_lines_and_orders_fractional_seconds(self):
        lines = [f"2026-10-12T12:00:00.{n:09d}Z line {n}" for n in range(51)]
        with patch.object(self.agent, "stream_log_lines", return_value=(list(reversed(lines)), 3000, False, False)):
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
        all_lines = [f"2026-10-12T12:00:00.{n:09d}Z event-{n}" for n in range(175)]

        def inclusive_logs(args, *, tail, scan_budget, timeout):
            # Real Docker first chooses a tail of the whole container log, then
            # applies --until. The old filter-then-tail mock concealed the bug.
            docker_tail = int(args[args.index("--tail") + 1]) if "--tail" in args else len(all_lines)
            until = args[args.index("--until") + 1]
            rows = [line for line in all_lines[-docker_tail:]
                    if self.agent.timestamp_key(line.split()[0]) <= self.agent.timestamp_key(until)]
            return rows[-tail:], sum(len(line) + 1 for line in rows), len(rows) > tail, False

        selected = [{"id": "one", "name": "backend"}]
        params = {"tail": 50, "since_at": "2026-10-12T11:00:00Z", "until": "2026-10-12T13:00:00Z"}
        with patch.object(self.agent, "stream_log_lines", side_effect=inclusive_logs):
            first = self.agent.read_logs(selected, params)
            second = self.agent.read_logs(selected, {**params, "until": first["log_page"]["next_until"]})
            third = self.agent.read_logs(selected, {**params, "until": second["log_page"]["next_until"]})
            last = self.agent.read_logs(selected, {**params, "until": third["log_page"]["next_until"]})
        self.assertEqual([page["log_page"]["returned_lines"] for page in (first, second, third, last)], [50, 50, 50, 25])
        self.assertFalse(last["log_page"]["has_more"])
        self.assertEqual(len(set(line for page in (first, second, third, last) for line in page["log_content"].splitlines())), 175)
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
        with patch.object(self.agent, "stream_log_lines", return_value=(lines, 3000, False, False)):
            result = self.agent.read_logs([{"id": "one", "name": "web"}], {"tail": 50})
        self.assertTrue(result["log_page"]["boundary_collision"])
        self.assertIn("تكرر وقت", result["log_page"]["retention_note"])

    def test_log_page_discloses_replica_limit(self):
        selected = [{"id": str(index), "name": f"web-{index}"} for index in range(5)]
        with patch.object(self.agent, "stream_log_lines", return_value=(["2026-10-12T12:00:00Z event"], 50, False, False)) as stream:
            result = self.agent.read_logs(selected, {"tail": 50})
        self.assertEqual(stream.call_count, 4)
        self.assertEqual(result["log_page"]["omitted_container_count"], 1)
        self.assertTrue(result["log_page"]["truncated"])
        self.assertIn("أول أربع", result["log_page"]["retention_note"])

    def test_stream_ring_discards_complete_old_records_with_bounded_memory(self):
        with patch.object(self.agent, "DEPLOY_PATH", Path.cwd()), patch.object(self.agent, "MAX_LOG_CAPTURE_BYTES", 250):
            lines, scanned, evicted, collision = self.agent.stream_log_lines(
                (sys.executable, "-c", "for n in range(1000): print(f'{n:04d} ' + 'x' * 95)"),
                tail=51, scan_budget=200000, timeout=5,
            )
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("0998 "))
        self.assertTrue(lines[-1].startswith("0999 "))
        self.assertGreater(scanned, 100000)
        self.assertTrue(evicted)
        self.assertFalse(collision)
        self.assertLessEqual(sum(len(line) + 1 for line in lines), 250)

    def test_ring_byte_eviction_still_exposes_older_page(self):
        lines = ["2026-10-12T12:00:00.000000010Z full line", "2026-10-12T12:00:00.000000011Z full line"]
        with patch.object(self.agent, "stream_log_lines", return_value=(lines, 2000000, True, False)):
            result = self.agent.read_logs([{"id": "one", "name": "web"}], {"tail": 50})
        self.assertEqual(result["log_page"]["returned_lines"], 2)
        self.assertTrue(result["log_page"]["has_more"])
        self.assertEqual(result["log_page"]["next_until"], "2026-10-12T12:00:00.000000009Z")

    def test_replica_byte_eviction_does_not_skip_unseen_rows_between_pages(self):
        histories = {
            "large": [f"2026-10-12T12:00:00.{n:09d}Z event-{n}" for n in range(60, 102)],
            "small": [f"2026-10-12T12:00:00.{n:09d}Z event-{n}" for n in range(1, 51)],
        }

        def read_replica(args, *, tail, scan_budget, timeout):
            self.assertNotIn("--tail", args)
            until = self.agent.timestamp_key(args[args.index("--until") + 1])
            rows = [line for line in histories[args[-1]] if self.agent.timestamp_key(line.split()[0]) <= until]
            # A's large complete records fill its byte ring after only two
            # rows, while B's older compact records all fit in its ring.
            count = 2 if args[-1] == "large" else tail
            return rows[-count:], sum(len(line) + 1 for line in rows), len(rows) > count, False

        selected = [{"id": "large", "name": "a"}, {"id": "small", "name": "b"}]
        params = {"tail": 50, "since_at": "2026-10-12T11:00:00Z", "until": "2026-10-12T13:00:00Z"}
        pages = []
        with patch.object(self.agent, "stream_log_lines", side_effect=read_replica):
            for _ in range(30):
                page = self.agent.read_logs(selected, params)
                pages.append(page)
                if not page["log_page"]["has_more"]:
                    break
                params["until"] = page["log_page"]["next_until"]
        self.assertFalse(pages[-1]["log_page"]["has_more"])
        # B must not pull the first cursor below A's unseen rows 60..99.
        self.assertEqual(pages[0]["log_page"]["returned_lines"], 2)
        self.assertEqual(pages[0]["log_page"]["next_until"], "2026-10-12T12:00:00.000000099Z")
        observed = [line.rsplit(" ", 1)[-1] for page in pages for line in page["log_content"].splitlines()]
        expected = [line.rsplit(" ", 1)[-1] for rows in histories.values() for line in rows]
        self.assertCountEqual(observed, expected)
        for newer, older in zip(pages, pages[1:], strict=False):
            self.assertLess(self.agent.timestamp_key(older["log_page"]["latest_at"]),
                            self.agent.timestamp_key(newer["log_page"]["earliest_at"]))

    def test_stream_tracks_timestamp_collision_at_byte_eviction_boundary(self):
        code = "for n in range(10): print('2026-10-12T12:00:00.000000001Z ' + str(n) + 'x' * 60)"
        with patch.object(self.agent, "DEPLOY_PATH", Path.cwd()), patch.object(self.agent, "MAX_LOG_CAPTURE_BYTES", 200):
            result = self.agent.stream_log_lines((sys.executable, "-c", code), tail=51, scan_budget=2000, timeout=5)
        lines, _, evicted, collision = result
        self.assertEqual(len(lines), 2)
        self.assertTrue(evicted)
        self.assertTrue(collision)
        with patch.object(self.agent, "stream_log_lines", return_value=result):
            page = self.agent.read_logs([{"id": "one", "name": "web"}], {"tail": 50})
        self.assertTrue(page["log_page"]["boundary_collision"])
        self.assertTrue(page["log_page"]["has_more"])
        self.assertIn("تكرر وقت", page["log_page"]["retention_note"])

    def test_log_scan_budget_is_shared_across_replicas(self):
        with patch.object(self.agent, "MAX_LOG_SCAN_BYTES", 1000), patch.object(
            self.agent, "stream_log_lines", side_effect=[([], 700, False, False), ([], 300, False, False)],
        ) as stream:
            self.agent.read_logs([{"id": "one", "name": "web1"}, {"id": "two", "name": "web2"}], {"tail": 50})
        self.assertEqual(stream.call_args_list[0].kwargs["scan_budget"], 1000)
        self.assertEqual(stream.call_args_list[1].kwargs["scan_budget"], 300)

    def test_stream_scan_and_individual_record_overflow_fail_without_partial_results(self):
        with patch.object(self.agent, "DEPLOY_PATH", Path.cwd()):
            with self.assertRaises(self.agent.LogScanLimitExceeded):
                self.agent.stream_log_lines(
                    (sys.executable, "-c", "print('private-suffix-' * 1000)"),
                    tail=51, scan_budget=1000, timeout=5,
                )
            with patch.object(self.agent, "MAX_LOG_CAPTURE_BYTES", 1000):
                with self.assertRaises(self.agent.LogOutputLimitExceeded):
                    self.agent.stream_log_lines(
                        (sys.executable, "-c", "print('private-suffix-' * 1000)"),
                        tail=51, scan_budget=100000, timeout=5,
                    )

    def test_stream_stalled_process_is_killed(self):
        with patch.object(self.agent, "DEPLOY_PATH", Path.cwd()):
            with self.assertRaises(self.agent.subprocess.TimeoutExpired):
                self.agent.stream_log_lines(
                    (sys.executable, "-c", "import time; time.sleep(10)"),
                    tail=51, scan_budget=1000, timeout=0.1,
                )

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
