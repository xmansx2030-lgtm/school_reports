from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from operations.collector import sync_inventory_report


class Command(BaseCommand):
    help = "Synchronize Docker projects and their resource usage from a host collector report."

    def add_arguments(self, parser):
        parser.add_argument("report", nargs="?", help="JSON report path; omit or use - to read stdin.")

    def handle(self, *args, **options):
        report_path = options.get("report")
        try:
            raw = (
                Path(report_path).read_text(encoding="utf-8")
                if report_path and report_path != "-"
                else sys.stdin.read()
            )
            report = json.loads(raw)
            result = sync_inventory_report(report)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(json.dumps(result, ensure_ascii=False, sort_keys=True)))
