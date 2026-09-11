"""Fail when a literal environment variable used by Python is undocumented.

The check deliberately follows only literal, upper-case keys. Dynamically built
names cannot be documented reliably and should be avoided for configuration.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=")
SKIPPED_PARTS = {".venv", "migrations", "tests"}
SKIPPED_FILES = {"test_settings.py"}


def _tracked_python_files() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to audit tracked configuration files")
    result = subprocess.run(  # noqa: S603 -- executable is resolved locally, arguments are fixed
        [git, "ls-files", "-z", "--", "*.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8"))
        if path.name in SKIPPED_FILES or SKIPPED_PARTS.intersection(path.parts):
            continue
        paths.append(ROOT / path)
    return paths


def _function_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_environment_reader(node: ast.Call) -> bool:
    name = _function_name(node.func)
    if name in {"getenv", "required_env"}:
        return True
    if "env" in name.lower():
        return True
    if name not in {"get", "setdefault"} or not isinstance(node.func, ast.Attribute):
        return False
    owner = node.func.value
    return (
        isinstance(owner, ast.Attribute)
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "os"
        and owner.attr == "environ"
    )


def _used_environment_keys() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in _tracked_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args or not _is_environment_reader(node):
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            key = first.value
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                continue
            found.setdefault(key, set()).add(path.relative_to(ROOT).as_posix())
    return found


def _documented_environment_keys() -> set[str]:
    documented = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE.match(line)
        if match:
            documented.add(match.group(1))
    return documented


def main() -> int:
    used = _used_environment_keys()
    documented = _documented_environment_keys()
    missing = sorted(set(used) - documented)
    if not missing:
        print(f"Environment contract OK: {len(used)} literal keys are documented.")
        return 0

    print("Environment variables missing from .env.example:", file=sys.stderr)
    for key in missing:
        print(f"  {key}: {', '.join(sorted(used[key]))}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
