"""agw-launch runs on whatever python3 a supported Debian ships, so its shape is checked structurally."""

from __future__ import annotations

import ast
import sys

from agentworks.execution import agw_launch, envelope

# The first stage's top-level names are the globals the launcher runs in.
STAGE0_GLOBALS = {"os", "sys", "S", "a", "s", "f", "r", "g"}


def tree() -> ast.Module:
    return ast.parse(envelope.launcher_source(), feature_version=(3, 11))


def test_the_launcher_parses_at_the_oldest_supported_python() -> None:
    tree()


def test_the_launcher_defines_only_what_the_client_shares_and_main() -> None:
    names = set()
    for node in tree().body:
        if isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.If | ast.Expr):
            continue  # the `__main__` guard and the module docstring
        else:
            raise AssertionError(f"unexpected module-level statement: {ast.dump(node)[:80]}")

    assert names == {"STDLIB_MISSING_STATUS", "refusal_status", "main"}
    assert not names & STAGE0_GLOBALS


def test_every_import_is_standard_library_and_none_runs_at_import_time() -> None:
    imported: set[str] = set()
    for node in ast.walk(tree()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= sys.stdlib_module_names
    assert not [node for node in tree().body if isinstance(node, ast.Import | ast.ImportFrom)]


def test_imports_that_can_be_missing_sit_in_one_guard_that_exits_with_the_dedicated_status() -> None:
    main = next(node for node in tree().body if isinstance(node, ast.FunctionDef) and node.name == "main")
    guards = [
        node
        for node in main.body
        if isinstance(node, ast.Try) and any(isinstance(inner, ast.Import) for inner in node.body)
    ]
    guarded_imports = [node for guard in guards for node in guard.body if isinstance(node, ast.Import)]

    assert len(guards) == 1
    assert {alias.name for node in guarded_imports for alias in node.names} >= {"json", "pwd", "subprocess"}
    handler = guards[0].handlers[0]
    assert ast.unparse(handler.type) == "ImportError"  # type: ignore[arg-type]
    assert "STDLIB_MISSING_STATUS" in ast.unparse(handler)


def test_importing_the_launcher_has_no_effect_and_needs_nothing_unix_only() -> None:
    assert agw_launch.STDLIB_MISSING_STATUS == 69
    assert agw_launch.refusal_status("cwd") == 125
