#!/usr/bin/env python3
"""Emit repeatable architecture-debt metrics for production Python modules."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = ("config", "core", "reports", "maintenance", "operations", "deploy", "scripts")
EXCLUDED_PARTS = {".venv", "__pycache__", "migrations", "tests"}


def production_files() -> list[Path]:
    paths = [ROOT / "manage.py"]
    for source_root in SOURCE_ROOTS:
        paths.extend((ROOT / source_root).rglob("*.py"))
    return sorted(
        path
        for path in paths
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts)
        and not path.name.startswith("test_")
        and path.name != "tests.py"
    )


def module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imported_modules(tree: ast.AST, source_module: str, known: set[str]) -> set[str]:
    edges: set[str] = set()
    source_parts = source_module.split(".")
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = source_parts[:-1]
                prefix = package[: max(0, len(package) - node.level + 1)]
                base = ".".join([*prefix, *(node.module or "").split(".")]).strip(".")
            else:
                base = node.module or ""
            if base:
                candidates.append(base)
                candidates.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        for candidate in candidates:
            probe = candidate
            while probe:
                if probe in known:
                    edges.add(probe)
                    break
                probe = probe.rpartition(".")[0]
    edges.discard(source_module)
    return edges


def strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in graph:
        if node not in indices:
            visit(node)
    return sorted(components, key=lambda item: (-len(item), item))


def c901_count() -> int | None:
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        ".",
        "--select",
        "C901",
        "--config",
        "lint.mccabe.max-complexity=20",
        "--output-format",
        "json",
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode not in (0, 1):
        return None
    try:
        return len(json.loads(result.stdout))
    except json.JSONDecodeError:
        return None


def main() -> int:
    files = production_files()
    trees: dict[str, ast.AST] = {}
    metrics = defaultdict(int)
    largest_file = ("", 0)
    largest_function = ("", "", 0)

    for path in files:
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        module = module_name(path)
        trees[module] = tree
        line_count = len(source.splitlines())
        if line_count > largest_file[1]:
            largest_file = (str(path.relative_to(ROOT)).replace("\\", "/"), line_count)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                metrics["wildcard_imports"] += 1
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                if length > largest_function[2]:
                    largest_function = (module, node.name, length)
            if isinstance(node, ast.ExceptHandler):
                names: set[str] = set()
                if isinstance(node.type, ast.Name):
                    names.add(node.type.id)
                elif isinstance(node.type, ast.Tuple):
                    names.update(item.id for item in node.type.elts if isinstance(item, ast.Name))
                if "Exception" in names:
                    metrics["broad_exceptions"] += 1
                    if node.body and all(isinstance(statement, (ast.Pass, ast.Continue, ast.Break)) for statement in node.body):
                        metrics["silent_broad_exceptions"] += 1

    known = set(trees)
    graph = {module: imported_modules(tree, module, known) for module, tree in trees.items()}
    components = strongly_connected_components(graph)
    result = {
        "production_python_files": len(files),
        "circular_components": len(components),
        "modules_in_cycles": sum(len(component) for component in components),
        "cycles": components,
        "wildcard_imports": metrics["wildcard_imports"],
        "broad_exceptions": metrics["broad_exceptions"],
        "silent_broad_exceptions": metrics["silent_broad_exceptions"],
        "c901_findings": c901_count(),
        "largest_python_file": {"path": largest_file[0], "lines": largest_file[1]},
        "largest_function": {
            "module": largest_function[0],
            "name": largest_function[1],
            "lines": largest_function[2],
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
