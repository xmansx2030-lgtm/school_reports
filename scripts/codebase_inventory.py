"""Generate a deterministic inventory of every Git-tracked project file.

The inventory is intentionally based on ``git ls-files``. Runtime databases,
uploaded media, virtual environments, build outputs, and real environment files
are deployment state rather than codebase inputs and must remain ignored.
"""
from __future__ import annotations

import argparse
import hashlib
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
import subprocess


ROOT = Path(__file__).resolve().parents[1]
TEXT_EXTENSIONS = {
    "",
    ".cjs",
    ".css",
    ".dart",
    ".dockerignore",
    ".editorconfig",
    ".example",
    ".gitattributes",
    ".gitignore",
    ".html",
    ".js",
    ".json",
    ".kt",
    ".kts",
    ".lock",
    ".md",
    ".part",
    ".properties",
    ".ps1",
    ".py",
    ".service",
    ".sh",
    ".timer",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
CRITICAL_MARKERS = (
    ".github/workflows/",
    "config/settings.py",
    "config/urls.py",
    "core/client_ip.py",
    "core/limits_cache.py",
    "deploy/",
    "maintenance/services.py",
    "operations/authentication.py",
    "reports/api_auth.py",
    "reports/discount_codes.py",
    "reports/moyasar_gateway.py",
    "reports/permissions.py",
    "reports/services_approval.py",
    "reports/tamara_gateway.py",
    "reports/views/auth.py",
    "reports/views/billing_",
)
HISTORICAL_DOCUMENTS = {
    "CRITICAL_FIXES.md",
    "SECURITY_AUDIT_REPORT.md",
    "SECURITY_REMEDIATION_PLAN.md",
}


def tracked_files() -> list[str]:
    result = subprocess.run(  # noqa: S603,S607 - fixed Git argv, no shell.
        ["git", "ls-files", "-z"],  # noqa: S607 - Git is the intended PATH tool.
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(
        entry.decode("utf-8")
        for entry in result.stdout.split(b"\0")
        if entry
    )


def category(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    name = PurePosixPath(path).name
    if "/migrations/" in f"/{path}":
        return "Generated migration"
    if "/tests/" in f"/{path}" or name.startswith("test_") or name in {
        "tests.py",
        "widget_test.dart",
    }:
        return "Test"
    if path.startswith("static/vendor/") or path.startswith("static/js/vendor/"):
        return "Vendored asset"
    if suffix in {".png", ".ico", ".ttf", ".woff", ".woff2"}:
        return "Binary asset"
    if path.startswith("reports/templates/") or path.startswith("maintenance/templates/"):
        return "Template"
    if path.startswith("static/") or path.startswith("reports/static/") or path.startswith("maintenance/static/"):
        return "Static source"
    if path.startswith("operations_mobile/"):
        return "Flutter client"
    if path.startswith(".github/") or path.startswith("deploy/") or name.startswith("compose.") or name == "Dockerfile":
        return "CI/deployment"
    if path.startswith("docs/") or suffix == ".md":
        return "Documentation"
    if suffix == ".py":
        return "Python application"
    if path.startswith(".claude/") or path.startswith(".codex-"):
        return "Developer tooling"
    return "Configuration/other"


def line_count(path: Path) -> int | None:
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return None
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except UnicodeDecodeError:
        return None


def file_status(path: str, kind: str, lines: int | None, duplicate: bool) -> str:
    statuses: list[str] = []
    if kind in {"Generated migration", "Vendored asset"}:
        statuses.append("Generated")
    else:
        statuses.append("Active")
    if any(marker in path for marker in CRITICAL_MARKERS):
        statuses.append("Critical")
    if path in HISTORICAL_DOCUMENTS:
        statuses.append("Legacy documentation")
    if duplicate:
        statuses.append("Duplicate asset")

    suffix = PurePosixPath(path).suffix.lower()
    limit = 500 if suffix == ".py" else 350 if suffix in {".html", ".css", ".js", ".dart"} else None
    if limit is not None and lines is not None and lines > limit and kind not in {
        "Generated migration",
        "Vendored asset",
        "Test",
    }:
        statuses.append("Needs refactoring")
    return "; ".join(statuses)


def render_inventory(files: list[str]) -> str:
    hashes: dict[str, list[str]] = defaultdict(list)
    sizes: dict[str, int] = {}
    lines_by_file: dict[str, int | None] = {}
    categories: Counter[str] = Counter()

    for relative in files:
        path = ROOT / relative
        payload = path.read_bytes()
        sizes[relative] = len(payload)
        lines_by_file[relative] = line_count(path)
        categories[category(relative)] += 1
        if payload:
            hashes[hashlib.sha256(payload).hexdigest()].append(relative)

    duplicate_files = {
        relative
        for group in hashes.values()
        if len(group) > 1
        for relative in group
    }

    output = [
        "# Codebase File Inventory",
        "",
        "> Generated by `python scripts/codebase_inventory.py --output docs/CODEBASE_INVENTORY.md`.",
        "> Status is a triage aid, not permission to remove a file. Generated migrations are immutable schema history.",
        "",
        f"Tracked files: **{len(files):,}**.",
        "",
        "## Summary",
        "",
        "| Classification | Files |",
        "| --- | ---: |",
    ]
    output.extend(
        f"| {kind} | {count:,} |"
        for kind, count in sorted(categories.items(), key=lambda item: (-item[1], item[0]))
    )
    output.extend(
        [
            "",
            "## Files",
            "",
            "| File | Classification | Status | Lines | Bytes |",
            "| --- | --- | --- | ---: | ---: |",
        ]
    )
    for relative in files:
        kind = category(relative)
        lines = lines_by_file[relative]
        output.append(
            "| `{}` | {} | {} | {} | {:,} |".format(
                relative.replace("|", "\\|"),
                kind,
                file_status(relative, kind, lines, relative in duplicate_files),
                "binary" if lines is None else f"{lines:,}",
                sizes[relative],
            )
        )
    output.append("")
    return "\n".join(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write Markdown to this repository-relative path.")
    args = parser.parse_args()
    rendered = render_inventory(tracked_files())
    if args.output:
        destination = (ROOT / args.output).resolve()
        destination.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
