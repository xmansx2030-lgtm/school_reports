"""Exercise the real collector shell boundary without contacting Docker."""

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless


@skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("flock"), "Linux collector runner")
class CollectorRunnerTests(TestCase):
    def run_collector(self, fail=False):
        source = Path(__file__).resolve().parents[1] / "deploy/hetzner/run_operations_collector.sh"
        with TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "deploy/hetzner"
            target.mkdir(parents=True)
            shutil.copyfile(source, target / source.name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            temp_dir = root / "tmp"
            temp_dir.mkdir()
            programs = {
                "python3": '#!/bin/sh\nprintf "capture\\n" >> "$TEST_EVENTS"\n'
                           'test "$TEST_FAIL" != 1 || exit 23\n'
                           'printf "{}"\nprintf "captured\\n" >> "$TEST_EVENTS"\n',
                "docker": '#!/bin/sh\nprintf "import\\n" >> "$TEST_EVENTS"\n'
                          'cat >/dev/null\n',
            }
            for name, text in programs.items():
                path = bin_dir / name
                path.write_text(text)
                path.chmod(0o700)
            events = root / "events"
            result = subprocess.run(  # noqa: S603 - fixed local script, fake binaries
                [shutil.which("bash"), str(target / source.name)], capture_output=True, text=True, timeout=10,
                env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                     "DEPLOY_PATH": str(root), "TMPDIR": str(temp_dir),
                     "TEST_EVENTS": str(events), "TEST_FAIL": "1" if fail else "0"},
            )
            return result, events.read_text().splitlines(), list(temp_dir.iterdir())

    def test_import_starts_after_capture_finishes_and_temporary_report_is_removed(self):
        result, events, leftovers = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, ["capture", "captured", "import"])
        self.assertEqual(leftovers, [])

    def test_failed_capture_does_not_start_import_and_failure_is_not_hidden_by_lock(self):
        result, events, leftovers = self.run_collector(fail=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(events, ["capture"])
        self.assertEqual(leftovers, [])

