from __future__ import annotations

import ast
from pathlib import Path

import pytest

GUIDE_PACKAGE = Path(__file__).parents[2] / "agentworks" / "guide"
FORBIDDEN_IMPORT_ROOTS = {
    "builtins",
    "io",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "sqlite3",
    "subprocess",
    "tempfile",
    "agentworks.capabilities",
    "agentworks.orchestration",
    "agentworks.output",
    "agentworks.secrets",
    "agentworks.ssh",
    "agentworks.transports",
    "agentworks.vms.manager",
}
FORBIDDEN_CALL_NAMES = {
    "mkdir",
    "open",
    "rename",
    "touch",
    "unlink",
    "write",
    "write_bytes",
    "write_text",
}
FORBIDDEN_CALL_PREFIXES = ("delete_", "insert_", "remove_", "set_", "update_")
ALLOWED_INERT_IMPORTS = {"agentworks.secrets.guide_contributions"}
FORBIDDEN_BOUND_NAMES = {"__import__", "compile", "eval", "exec", "open"}


def _is_forbidden_import(module: str) -> bool:
    return any(module == root or module.startswith(f"{root}.") for root in FORBIDDEN_IMPORT_ROOTS)


def _resolved_from_module(node: ast.ImportFrom, package: str) -> str:
    if node.level == 0:
        return node.module or ""
    package_parts = package.split(".")
    retained_parts = len(package_parts) - node.level + 1
    if retained_parts <= 0:
        return "<invalid-relative-import>"
    base = ".".join(package_parts[:retained_parts])
    return f"{base}.{node.module}" if node.module else base


def _import_bindings(tree: ast.AST, package: str) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                bindings[bound] = alias.name if alias.asname else bound
        elif isinstance(node, ast.ImportFrom):
            module = _resolved_from_module(node, package)
            for alias in node.names:
                full_path = f"{module}.{alias.name}" if module else alias.name
                bindings[alias.asname or alias.name] = full_path
    return bindings


def _qualified_name(node: ast.expr, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value, bindings)
        return f"{owner}.{node.attr}" if owner is not None else None
    return None


def _power_boundary_violations(
    source: str,
    filename: str = "<synthetic>",
    *,
    package: str = "agentworks.guide",
) -> tuple[str, ...]:
    tree = ast.parse(source, filename=filename)
    bindings = _import_bindings(tree, package)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_import(alias.name):
                    violations.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = _resolved_from_module(node, package)
            if module == "<invalid-relative-import>":
                violations.append(f"line {node.lineno}: invalid relative import")
                continue
            for alias in node.names:
                full_path = f"{module}.{alias.name}" if module else alias.name
                if _is_forbidden_import(full_path) and full_path not in ALLOWED_INERT_IMPORTS:
                    violations.append(f"line {node.lineno}: import {full_path}")
        elif isinstance(node, ast.Attribute):
            qualified = _qualified_name(node, bindings)
            forbidden_method = node.attr in FORBIDDEN_CALL_NAMES or node.attr.startswith(FORBIDDEN_CALL_PREFIXES)
            if qualified is not None and _is_forbidden_import(qualified):
                violations.append(f"line {node.lineno}: reference to {qualified}")
            elif forbidden_method:
                violations.append(f"line {node.lineno}: reference to {node.attr}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in FORBIDDEN_BOUND_NAMES:
            violations.append(f"line {node.lineno}: reference to {node.id}")
    return tuple(violations)


@pytest.mark.parametrize("path", sorted(GUIDE_PACKAGE.glob("*.py")), ids=lambda path: path.name)
def test_guide_package_has_no_operational_power_imports_or_mutating_calls(path: Path) -> None:
    """Keep the inert guide boundary resistant to aliases and low-level writes."""
    violations = _power_boundary_violations(path.read_text(), str(path))

    assert not violations, f"{path.name} crosses the guide power boundary: {', '.join(violations)}"


@pytest.mark.parametrize(
    "source",
    [
        "import agentworks.output as output",
        "from agentworks.output import prompt as ask",
        "from agentworks import output as output",
        "from agentworks import secrets as secrets",
        "from agentworks import transports as transports",
        "from .. import output as output",
        "from ..output import prompt as ask",
        "from .. import transports as transports",
        "from ..secrets import resolve_secrets",
        "import agentworks as aw\nask = aw.output.prompt",
        "open('state.txt')",
        "writer = Path.write_text",
        "mutator = db.insert_vm",
        "import sqlite3\nconnect = sqlite3.connect",
    ],
)
def test_power_boundary_rejects_direct_and_parent_package_alias_imports(source: str) -> None:
    assert _power_boundary_violations(source)


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("from .. import output as output", "agentworks.guide"),
        ("from ... import output as output", "agentworks.guide.nested"),
        ("from ...output import prompt as ask", "agentworks.guide.nested"),
    ],
)
def test_power_boundary_resolves_relative_imports_from_parent_and_root(
    source: str,
    package: str,
) -> None:
    assert _power_boundary_violations(source, package=package)


def test_power_boundary_rejects_relative_import_beyond_package_root() -> None:
    violations = _power_boundary_violations("from ... import output", package="agentworks.guide")

    assert violations == ("line 1: invalid relative import",)


def test_power_boundary_allows_only_the_inert_secret_topic_contribution_import() -> None:
    assert not _power_boundary_violations("from agentworks.secrets import guide_contributions as secret_topics")
    assert not _power_boundary_violations("from ..secrets import guide_contributions as secret_topics")
    assert not _power_boundary_violations(
        "from ...secrets import guide_contributions as secret_topics",
        package="agentworks.guide.nested",
    )
    violations = _power_boundary_violations("from agentworks.secrets import guide_contributions, resolve_secrets")
    assert violations == ("line 1: import agentworks.secrets.resolve_secrets",)
