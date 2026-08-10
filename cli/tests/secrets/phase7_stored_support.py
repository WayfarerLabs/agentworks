"""Stored-policy call graph support for Phase 7 enforcement tests."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.secrets.phase7_lexical_support import (
    _enclosing_function,
    _module_identity,
    _qualified_function_name,
    _semantic_object,
    _semantic_type_indexes,
    _visible_semantic_aliases,
)
from tests.secrets.phase7_resolver_support import (
    _expression_type,
    _instance_field_types_from_tree,
    _resolver_bindings,
    _resolver_usage_violations_from_tree,
)
from tests.secrets.test_phase7_enforcement import _object


def _stored_call_edges_from_tree(
    tree: ast.Module,
    *,
    module: str,
    current_package: str,
    returns: dict[str, str],
    fields: dict[tuple[str, str], str],
) -> dict[tuple[str, str], tuple[str, ...]]:
    resolver = _object("agentworks.secrets.resolver", "Resolver")
    fields = _instance_field_types_from_tree(
        tree,
        module=module,
        current_package=current_package,
        returns=returns,
        inherited_fields=fields,
    )
    local_definitions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    discovered: dict[tuple[str, str], list[str]] = {}
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in {
            "resolve",
            "resolve_gate",
            "resolve_late_repair",
        }:
            continue
        function = _enclosing_function(parents, call)
        assert function is not None
        aliases = _visible_semantic_aliases(
            tree,
            parents=parents,
            node=call,
            current_package=current_package,
        )
        ancestry: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        current: ast.AST | None = function
        while isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ancestry.append(current)
            current = _enclosing_function(parents, current)
        bindings: dict[str, str] = {}
        for scope in reversed(ancestry):
            parent = parents.get(scope)
            class_name = parent.name if isinstance(parent, ast.ClassDef) else None
            bindings = _resolver_bindings(
                scope,
                inherited=bindings,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
                returns=returns,
                fields=fields,
                class_name=class_name,
            )
        receiver_type = _expression_type(
            call.func.value,
            bindings=bindings,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
            returns=returns,
            fields=fields,
        )
        if receiver_type is None or _semantic_object(receiver_type) is not resolver:
            continue
        owner = (module, _qualified_function_name(parents, function))
        discovered.setdefault(owner, []).append(f"Resolver.{call.func.attr}")
    return {owner: tuple(edges) for owner, edges in discovered.items()}


def _stored_call_edges() -> dict[tuple[str, str], tuple[str, ...]]:
    root = Path(__file__).parents[2] / "agentworks"
    returns, fields = _semantic_type_indexes()
    discovered: dict[tuple[str, str], tuple[str, ...]] = {}
    for path in root.rglob("*.py"):
        module, current_package = _module_identity(path, root)
        discovered.update(
            _stored_call_edges_from_tree(
                ast.parse(path.read_text()),
                module=module,
                current_package=current_package,
                returns=returns,
                fields=fields,
            )
        )
    return discovered


def _resolver_usage_violations() -> list[tuple[str, str, str, int, str]]:
    root = Path(__file__).parents[2] / "agentworks"
    returns, fields = _semantic_type_indexes()
    violations: list[tuple[str, str, str, int, str]] = []
    for path in root.rglob("*.py"):
        module, current_package = _module_identity(path, root)
        tree = ast.parse(path.read_text())
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for expression, lineno, reason in _resolver_usage_violations_from_tree(
            tree,
            module=module,
            current_package=current_package,
            returns=returns,
            inherited_fields=fields,
        ):
            matching = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Attribute, ast.Call, ast.Subscript))
                and node.lineno == lineno
                and ast.unparse(node) == expression
            ]
            assert len(matching) == 1
            function = _enclosing_function(parents, matching[0])
            owner = _qualified_function_name(parents, function) if function is not None else "<module>"
            violations.append((module, owner, expression, lineno, reason))
    return violations
