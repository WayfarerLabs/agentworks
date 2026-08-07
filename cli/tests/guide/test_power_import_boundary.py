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


def _is_forbidden_import(module: str) -> bool:
    return any(module == root or module.startswith(f"{root}.") for root in FORBIDDEN_IMPORT_ROOTS)


@pytest.mark.parametrize("path", sorted(GUIDE_PACKAGE.glob("*.py")), ids=lambda path: path.name)
def test_guide_package_has_no_operational_power_imports_or_mutating_calls(path: Path) -> None:
    """Keep the inert guide boundary resistant to aliases and low-level writes."""
    tree = ast.parse(path.read_text(), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_import(alias.name):
                    violations.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports_only_inert_secret_topics = module == "agentworks.secrets" and {
                alias.name for alias in node.names
            } == {"guide_contributions"}
            if _is_forbidden_import(module) and not imports_only_inert_secret_topics:
                violations.append(f"line {node.lineno}: from {module} import")
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if name is not None and (name in FORBIDDEN_CALL_NAMES or name.startswith(FORBIDDEN_CALL_PREFIXES)):
                violations.append(f"line {node.lineno}: call to {name}")

    assert not violations, f"{path.name} crosses the guide power boundary: {', '.join(violations)}"
