"""Exact caller-graph AST support for Phase 7 enforcement tests."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import cast

from tests.secrets.phase7_lexical_support import (
    _enclosing_function,
    _lexically_shadowed_names,
    _module_identity,
    _qualified_function_name,
    _resolved_from_module,
    _semantic_object,
    _semantic_reference,
    _visible_semantic_aliases,
)
from tests.secrets.phase7_resolver_support import _reflected_owner_and_member, _semantic_call_name
from tests.secrets.test_phase7_enforcement import (
    _INTERNAL_MANIFEST,
    _SERVICE_MANIFEST,
    _object,
)


def _actual_boundary_call_edges() -> dict[tuple[str, str], tuple[tuple[str, str], ...]]:
    root = Path(__file__).parents[2] / "agentworks"
    boundary_values: set[object] = set()
    for manifest in (_SERVICE_MANIFEST, _INTERNAL_MANIFEST):
        for module_name, names in manifest.items():
            for name in names:
                target_name = name.removesuffix(".__init__")
                boundary_values.add(_object(module_name, target_name))
    boundary_values.update(
        (
            _object("agentworks.secrets.resolve", "ResolutionPolicy"),
            _object("agentworks.secrets.preview", "_preview"),
        )
    )
    boundary_terminals = {
        name.rsplit(".", 1)[-1]
        for manifest in (_SERVICE_MANIFEST, _INTERNAL_MANIFEST)
        for names in manifest.values()
        for name in names
    } | {"Resolver", "PendingWorkspaceNode", "PendingAgentNode", "ResolutionPolicy", "_preview"}

    def is_boundary(target: str) -> bool:
        module_name, attribute = target.rsplit(".", 1)
        if attribute not in boundary_terminals:
            return False
        try:
            value = getattr(importlib.import_module(module_name), attribute)
        except (AttributeError, ModuleNotFoundError):
            return False
        return value in boundary_values

    discovered: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).with_suffix("")
        is_package = relative.parts[-1] == "__init__"
        module_parts = relative.parts[:-1] if is_package else relative.parts
        module = ".".join(("agentworks", *module_parts))
        current_package = module if is_package else module.rsplit(".", 1)[0]
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        aliases: dict[str, str] = {}
        for statement in ast.walk(tree):
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    aliases[alias.asname or alias.name.split(".", 1)[0]] = (
                        alias.name if alias.asname else alias.name.split(".", 1)[0]
                    )
            elif isinstance(statement, ast.ImportFrom):
                base = _resolved_from_module(current_package, statement)
                for alias in statement.names:
                    aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
        local_definitions = {
            statement.name
            for statement in tree.body
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            target = _semantic_call_name(
                call,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
            )
            if target is None or not is_boundary(target):
                continue
            function = _enclosing_function(parents, call)
            assert function is not None
            owner = (module, _qualified_function_name(parents, function))
            callee = ast.unparse(call.func)
            if owner == ("agentworks.secrets.preview", "preview_resolution") and callee == "_preview":
                continue
            interaction = [keyword.value for keyword in call.keywords if keyword.arg == "interaction"]
            assert len(interaction) == 1, (owner, callee, target, call.lineno)
            assert isinstance(interaction[0], ast.Name) and interaction[0].id == "interaction", (
                owner,
                callee,
                ast.unparse(interaction[0]),
            )
            discovered.setdefault(owner, []).append((callee, "interaction"))
    return {owner: tuple(edges) for owner, edges in discovered.items()}


def _semantic_target_calls_from_tree(
    tree: ast.Module,
    *,
    module: str,
    current_package: str,
    targets: tuple[object, ...],
) -> list[tuple[tuple[str, str], object, ast.Call]]:
    local_definitions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: list[tuple[tuple[str, str], object, ast.Call]] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        aliases = _visible_semantic_aliases(
            tree,
            parents=parents,
            node=call,
            current_package=current_package,
        )
        semantic_name = _semantic_call_name(
            call,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
        )
        if semantic_name is None:
            continue
        target = _semantic_object(semantic_name)
        if target not in targets:
            continue
        function = _enclosing_function(parents, call)
        assert function is not None
        found.append(((module, _qualified_function_name(parents, function)), target, call))
    return found


def _inside_annotation(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.arg):
            return True
        if isinstance(parent, ast.AnnAssign):
            return current is parent.annotation
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and current is parent.returns:
            return True
        if isinstance(parent, ast.stmt):
            return False
        current = parent
    return False


def _protected_callable_alias_violations_from_tree(
    tree: ast.Module,
    *,
    module: str,
    current_package: str,
    targets: tuple[object, ...],
) -> list[tuple[str, int, object]]:
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    local_definitions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    violations: list[tuple[str, int, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)):
            continue
        aliases = _visible_semantic_aliases(
            tree,
            parents=parents,
            node=node,
            current_package=current_package,
        )
        reflected = _reflected_owner_and_member(
            node,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
            shadowed=_lexically_shadowed_names(tree, parents, node, current_package),
        )
        if reflected is not None:
            owner_expression, member = reflected
            owner_name = _semantic_reference(
                owner_expression,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
            )
            owner = _semantic_object(owner_name) if owner_name is not None else None
            reflected_target = getattr(owner, member, None) if owner is not None else None
            if reflected_target in targets:
                violations.append((ast.unparse(node), cast(int, vars(node)["lineno"]), reflected_target))
        if not isinstance(node, (ast.Name, ast.Attribute)) or _inside_annotation(node, parents):
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            continue
        semantic_name = _semantic_reference(
            node,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
        )
        if semantic_name is None:
            continue
        target = _semantic_object(semantic_name)
        if target not in targets:
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and parent.func is node:
            continue
        if isinstance(parent, ast.Attribute) and parent.value is node and isinstance(target, type):
            continue
        violations.append((ast.unparse(node), node.lineno, target))
    return sorted(violations, key=lambda violation: violation[1])


def _production_protected_callable_alias_violations(
    *targets: object,
) -> list[tuple[str, str, int, object]]:
    root = Path(__file__).parents[2] / "agentworks"
    violations: list[tuple[str, str, int, object]] = []
    for path in root.rglob("*.py"):
        module, current_package = _module_identity(path, root)
        for expression, lineno, target in _protected_callable_alias_violations_from_tree(
            ast.parse(path.read_text()),
            module=module,
            current_package=current_package,
            targets=targets,
        ):
            violations.append((module, expression, lineno, target))
    return violations


def _production_semantic_target_calls(
    *targets: object,
) -> list[tuple[tuple[str, str], object, ast.Call]]:
    root = Path(__file__).parents[2] / "agentworks"
    found: list[tuple[tuple[str, str], object, ast.Call]] = []
    for path in root.rglob("*.py"):
        module, current_package = _module_identity(path, root)
        found.extend(
            _semantic_target_calls_from_tree(
                ast.parse(path.read_text()),
                module=module,
                current_package=current_package,
                targets=targets,
            )
        )
    return found
