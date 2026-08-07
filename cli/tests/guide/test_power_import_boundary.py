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


def _is_forbidden_import(module: str) -> bool:
    return any(module == root or module.startswith(f"{root}.") for root in FORBIDDEN_IMPORT_ROOTS)


def _power_boundary_violations(source: str, filename: str = "<synthetic>") -> tuple[str, ...]:
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_import(alias.name):
                    violations.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                full_path = f"{module}.{alias.name}" if module else alias.name
                if _is_forbidden_import(full_path) and full_path not in ALLOWED_INERT_IMPORTS:
                    violations.append(f"line {node.lineno}: import {full_path}")
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if name is not None and (name in FORBIDDEN_CALL_NAMES or name.startswith(FORBIDDEN_CALL_PREFIXES)):
                violations.append(f"line {node.lineno}: call to {name}")
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
    ],
)
def test_power_boundary_rejects_direct_and_parent_package_alias_imports(source: str) -> None:
    assert _power_boundary_violations(source)


def test_power_boundary_allows_only_the_inert_secret_topic_contribution_import() -> None:
    assert not _power_boundary_violations(
        "from agentworks.secrets import guide_contributions as secret_topics"
    )
    violations = _power_boundary_violations(
        "from agentworks.secrets import guide_contributions, resolve_secrets"
    )
    assert violations == ("line 1: import agentworks.secrets.resolve_secrets",)
