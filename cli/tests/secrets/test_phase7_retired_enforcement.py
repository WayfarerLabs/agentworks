"""Retired seam and permanent vocabulary enforcement for Phase 7."""

from __future__ import annotations

import ast
import importlib
import re
import sys
from pathlib import Path

import pytest

from tests.secrets.test_phase7_enforcement import (
    _FORBIDDEN_ROOT_IMPORTS,
    _OLD_MODULES,
    _PACKAGE_EXPORT_MANIFEST,
    _RETIRED_SYMBOLS,
)


def _resolved_from_module(current_package: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    parts = current_package.split(".")
    keep = len(parts) - (node.level - 1)
    base = parts[:keep]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _dotted_attribute(node: ast.Attribute) -> tuple[str, ...] | None:
    parts = [node.attr]
    current: ast.expr = node.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _retired_semantic_violations(
    source: str,
    *,
    module: str,
    current_package: str,
) -> list[str]:
    def forbidden_module(target: str) -> bool:
        return any(target == old or target.startswith(f"{old}.") for old in _OLD_MODULES)

    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    violations: list[str] = []
    forbidden_root_runtime = _FORBIDDEN_ROOT_IMPORTS | {"SECRET_BACKEND_REGISTRY"}
    if forbidden_module(module):
        violations.append(f"1:module:{module}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                aliases[bound] = alias.name if alias.asname else bound
                if forbidden_module(alias.name) or alias.name.rsplit(".", 1)[-1] in _RETIRED_SYMBOLS:
                    violations.append(f"{node.lineno}:import:{alias.name}")
                if alias.asname in _RETIRED_SYMBOLS:
                    violations.append(f"{node.lineno}:alias:{alias.asname}")
        elif isinstance(node, ast.ImportFrom):
            base = _resolved_from_module(current_package, node)
            if forbidden_module(base):
                violations.append(f"{node.lineno}:from:{base}")
            for alias in node.names:
                target = f"{base}.{alias.name}" if base else alias.name
                aliases[alias.asname or alias.name] = target
                if forbidden_module(target) or alias.name in _RETIRED_SYMBOLS or alias.asname in _RETIRED_SYMBOLS:
                    violations.append(f"{node.lineno}:imported:{target}")
                if base == "agentworks.secrets" and alias.name in forbidden_root_runtime:
                    violations.append(f"{node.lineno}:forbidden-root:{target}")
                if module == "agentworks.secrets" and alias.name in forbidden_root_runtime:
                    violations.append(f"{node.lineno}:root-reexport:{target}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _RETIRED_SYMBOLS:
            violations.append(f"{node.lineno}:name:{node.id}")
        elif (
            module == "agentworks.secrets"
            and isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in forbidden_root_runtime
        ):
            violations.append(f"{node.lineno}:root-binding:{node.id}")
        elif isinstance(node, ast.Attribute):
            parts = _dotted_attribute(node)
            if parts is None:
                continue
            target = ".".join((aliases.get(parts[0], parts[0]), *parts[1:]))
            if forbidden_module(target) or parts[-1] in _RETIRED_SYMBOLS:
                violations.append(f"{node.lineno}:attribute:{target}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in _RETIRED_SYMBOLS:
            violations.append(f"{node.lineno}:definition:{node.name}")
        elif (
            module == "agentworks.secrets"
            and isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            for element in node.value.elts:
                if (
                    isinstance(element, ast.Constant)
                    and isinstance(element.value, str)
                    and element.value in (_RETIRED_SYMBOLS | forbidden_root_runtime)
                ):
                    violations.append(f"{node.lineno}:root-export:{element.value}")
    return violations


def test_retired_resolution_seams_are_absent_from_production_ast() -> None:
    cli_root = Path(__file__).parents[2]
    found: list[str] = []
    for root in (cli_root / "agentworks", cli_root / "tests"):
        for path in root.rglob("*.py"):
            relative = path.relative_to(cli_root).with_suffix("")
            parts = relative.parts
            is_package = parts[-1] == "__init__"
            module_parts = parts[:-1] if is_package else parts
            module = ".".join(module_parts)
            current_package = module if is_package else module.rsplit(".", 1)[0]
            for violation in _retired_semantic_violations(
                path.read_text(),
                module=module,
                current_package=current_package,
            ):
                found.append(f"{path.relative_to(cli_root)}:{violation}")
    assert found == []


def test_retired_semantic_guard_negative_relocation_fixtures_cover_the_deny_lists() -> None:
    fixtures: list[tuple[str, str, str]] = []
    for symbol in _RETIRED_SYMBOLS:
        fixtures.extend(
            (
                ("agentworks.fixture", "agentworks", f"{symbol}\n"),
                ("agentworks.fixture", "agentworks", f"from fixture import {symbol} as relocated\n"),
                ("agentworks.fixture", "agentworks", f"class {symbol}:\n    pass\n"),
            )
        )
    for old_module in _OLD_MODULES:
        package, leaf = old_module.rsplit(".", 1)
        fixtures.extend(
            (
                (old_module, package, "value = 1\n"),
                ("agentworks.fixture", "agentworks", f"import {old_module} as relocated\n"),
                ("agentworks.fixture", "agentworks", f"from {old_module} import relocated\n"),
                ("agentworks.fixture", "agentworks", f"from {package} import {leaf} as relocated\n"),
                ("agentworks.secrets.fixture", "agentworks.secrets", f"from . import {leaf} as relocated\n"),
            )
        )
    for root_name in _FORBIDDEN_ROOT_IMPORTS | {"SECRET_BACKEND_REGISTRY"}:
        fixtures.extend(
            (
                ("agentworks.fixture", "agentworks", f"from agentworks.secrets import {root_name}\n"),
                ("agentworks.secrets", "agentworks.secrets", f"from .resolve import {root_name}\n"),
                ("agentworks.secrets", "agentworks.secrets", f"__all__ = [{root_name!r}]\n"),
                ("agentworks.secrets", "agentworks.secrets", f"{root_name} = object()\n"),
            )
        )
    fixtures.extend(
        (
            (
                "agentworks.fixture",
                "agentworks",
                "from agentworks import secrets as relocated\nvalue = relocated.backends\n",
            ),
            ("agentworks.secrets", "agentworks.secrets", "from . import backends as relocated\n"),
        )
    )
    for module, current_package, source in fixtures:
        assert _retired_semantic_violations(
            source,
            module=module,
            current_package=current_package,
        ), source


def test_retired_module_guard_catches_a_real_importable_filesystem_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.secrets as secrets_package

    retired_module = tmp_path / "backends.py"
    retired_module.write_text("MUTATION_SENTINEL = True\n")
    monkeypatch.setattr(secrets_package, "__path__", [*secrets_package.__path__, str(tmp_path)])
    importlib.invalidate_caches()
    try:
        imported = importlib.import_module("agentworks.secrets.backends")
        assert imported.MUTATION_SENTINEL is True
        assert _retired_semantic_violations(
            retired_module.read_text(),
            module="agentworks.secrets.backends",
            current_package="agentworks.secrets",
        ) == ["1:module:agentworks.secrets.backends"]
    finally:
        sys.modules.pop("agentworks.secrets.backends", None)
        if hasattr(secrets_package, "backends"):
            delattr(secrets_package, "backends")
        importlib.invalidate_caches()


def test_secret_package_runtime_surface_is_exact() -> None:
    import agentworks.secrets as package

    assert tuple(package.__all__) == _PACKAGE_EXPORT_MANIFEST
    assert all(getattr(package, name) is not None for name in _PACKAGE_EXPORT_MANIFEST)
    forbidden = _RETIRED_SYMBOLS | _FORBIDDEN_ROOT_IMPORTS | {"SECRET_BACKEND_REGISTRY", "backends"}
    assert forbidden.isdisjoint(vars(package))


def _contains_retired_vocabulary(text: str, phrase: str) -> bool:
    tokens = re.findall(r"[a-z0-9_-]+", text.lower())
    retired = re.findall(r"[a-z0-9_-]+", phrase.lower())
    return any(tokens[index : index + len(retired)] == retired for index in range(len(tokens)))


def test_retired_vocabulary_match_is_token_aware() -> None:
    assert _contains_retired_vocabulary("the active backend", "active backend")
    assert not _contains_retired_vocabulary("an interactive backend", "active backend")


def test_permanent_runtime_vocabulary_and_rendered_secret_guide_are_source_first() -> None:
    from agentworks.secrets.guide_contributions import guide_contributions

    root = Path(__file__).parents[3]
    permanent = [root / "README.md", root / "docs", root / "cli" / "README.md", root / "cli" / "agentworks"]
    retired_phrases = (
        "active backend",
        "backend chain",
        "first backend with a value",
        "next backend",
        "resolve_secrets",
        "ActiveBackend",
    )
    violations: list[str] = []
    for target in permanent:
        paths = (target,) if target.is_file() else (path for path in target.rglob("*") if path.suffix in {".md", ".py"})
        for path in paths:
            if "docs/sdd/" in path.as_posix():
                continue
            text = path.read_text()
            if any(_contains_retired_vocabulary(text, phrase) for phrase in retired_phrases):
                violations.append(str(path.relative_to(root)))
    assert violations == []

    topic = guide_contributions()[0]
    rendered = "\n".join(
        [topic.summary, *(block.markdown for block in topic.blocks if hasattr(block, "markdown"))]
    ).lower()
    assert "secret-source" in rendered
    assert "secret-backend" in rendered
    assert "resource sample secret-source" in rendered
    assert "env-var" in rendered and "prompt" in rendered
    assert "onepassword" in rendered and "op://" in rendered
    assert "implementation inventory is global" in rendered
    assert "not configured secret sources or their order" in rendered
    assert "configured source order" not in rendered
    assert "interaction policy" in rendered
    assert "preview" in rendered and "not proof" in rendered
    assert "consent" in rendered
    assert not any(_contains_retired_vocabulary(rendered, phrase) for phrase in retired_phrases)
