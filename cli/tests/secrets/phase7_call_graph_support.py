"""Exact caller-graph AST support for Phase 7 enforcement tests."""

from __future__ import annotations

import ast
from typing import cast

from tests.secrets.phase7_lexical_support import (
    _enclosing_function,
    _lexically_shadowed_names,
    _production_modules,
    _qualified_function_name,
    _semantic_call_name,
    _semantic_object,
    _semantic_reference,
    _visible_semantic_aliases,
)
from tests.secrets.phase7_resolver_support import _reflected_owner_and_member


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
    violations: list[tuple[str, str, int, object]] = []
    for source in _production_modules():
        for expression, lineno, target in _protected_callable_alias_violations_from_tree(
            source.tree,
            module=source.module,
            current_package=source.current_package,
            targets=targets,
        ):
            violations.append((source.module, expression, lineno, target))
    return violations


def _production_semantic_target_calls(
    *targets: object,
) -> list[tuple[tuple[str, str], object, ast.Call]]:
    found: list[tuple[tuple[str, str], object, ast.Call]] = []
    for source in _production_modules():
        found.extend(
            _semantic_target_calls_from_tree(
                source.tree,
                module=source.module,
                current_package=source.current_package,
                targets=targets,
            )
        )
    return found
