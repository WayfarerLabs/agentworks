"""Resolver-aware AST support for Phase 7 enforcement tests."""

from __future__ import annotations

import ast
from typing import cast

from tests.secrets.phase7_lexical_support import (
    _binding_targets,
    _dotted_attribute,
    _enclosing_function,
    _lexically_shadowed_names,
    _scope_nodes,
    _semantic_aliases,
    _semantic_annotation_type,
    _semantic_object,
    _semantic_reference,
    _visible_semantic_aliases,
)
from tests.secrets.test_phase7_enforcement import (
    _RESOLVER_CONTAINER_TYPE,
    _RESOLVER_TYPE,
    _object,
    _object_name,
)


def _expression_type(
    expression: ast.expr,
    *,
    bindings: dict[str, str],
    aliases: dict[str, str],
    module: str,
    local_definitions: set[str],
    returns: dict[str, str],
    fields: dict[tuple[str, str], str],
) -> str | None:
    spelling = ast.unparse(expression)
    if spelling in bindings:
        return bindings[spelling]
    if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
        for element in expression.elts:
            inferred = _expression_type(
                element,
                bindings=bindings,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
                returns=returns,
                fields=fields,
            )
            if inferred == _RESOLVER_TYPE:
                return _RESOLVER_CONTAINER_TYPE
        return None
    if isinstance(expression, ast.Dict):
        for element in expression.values:
            inferred = _expression_type(
                element,
                bindings=bindings,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
                returns=returns,
                fields=fields,
            )
            if inferred == _RESOLVER_TYPE:
                return _RESOLVER_CONTAINER_TYPE
        return None
    if isinstance(expression, ast.Call):
        target = _semantic_call_name(
            expression,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
        )
        if target is None:
            return None
        value = _semantic_object(target)
        resolver = _object("agentworks.secrets.resolver", "Resolver")
        if value is resolver:
            return _RESOLVER_TYPE
        return returns.get(target)
    if isinstance(expression, ast.Attribute):
        owner = _expression_type(
            expression.value,
            bindings=bindings,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
            returns=returns,
            fields=fields,
        )
        if owner is not None:
            return fields.get((owner, expression.attr))
    return None


def _resolver_bindings(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    inherited: dict[str, str],
    aliases: dict[str, str],
    module: str,
    local_definitions: set[str],
    returns: dict[str, str],
    fields: dict[tuple[str, str], str],
    class_name: str | None,
) -> dict[str, str]:
    bindings = dict(inherited)
    if class_name is not None:
        bindings["self"] = f"{module}.{class_name}"
        bindings["cls"] = f"{module}.{class_name}"
    for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs):
        if argument.annotation is None:
            continue
        annotation = _semantic_annotation_type(
            argument.annotation,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
        )
        if annotation is not None:
            bindings[argument.arg] = annotation

    assignments: list[tuple[tuple[str, ...], ast.expr]] = []
    for node in _scope_nodes(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assignments.append((_binding_targets(target), node.value))
        elif isinstance(node, ast.AnnAssign):
            annotation = _semantic_annotation_type(
                node.annotation,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
            )
            if annotation is not None:
                for binding_name in _binding_targets(node.target):
                    bindings[binding_name] = annotation
            if node.value is not None:
                assignments.append((_binding_targets(node.target), node.value))
        elif isinstance(node, ast.NamedExpr):
            assignments.append((_binding_targets(node.target), node.value))

    for _attempt in range(len(assignments) + 1):
        changed = False
        for targets, value in assignments:
            inferred = _expression_type(
                value,
                bindings=bindings,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
                returns=returns,
                fields=fields,
            )
            if inferred is None:
                continue
            for binding_name in targets:
                if bindings.get(binding_name) != inferred:
                    bindings[binding_name] = inferred
                    changed = True
        if not changed:
            break
    return bindings


def _instance_field_types_from_tree(
    tree: ast.Module,
    *,
    module: str,
    current_package: str,
    returns: dict[str, str],
    inherited_fields: dict[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    fields = dict(inherited_fields)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    local_definitions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    methods = [
        (owner, method)
        for owner in tree.body
        if isinstance(owner, ast.ClassDef)
        for method in owner.body
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for _attempt in range(len(methods) + 1):
        changed = False
        for owner, method in methods:
            scope_nodes = _scope_nodes(method)
            anchor = max(
                (node for node in scope_nodes if "lineno" in vars(node)),
                key=lambda node: cast(int, vars(node)["lineno"]),
                default=method,
            )
            aliases = _visible_semantic_aliases(
                tree,
                parents=parents,
                node=anchor,
                current_package=current_package,
            )
            bindings = _resolver_bindings(
                method,
                inherited={},
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
                returns=returns,
                fields=fields,
                class_name=owner.name,
            )
            for spelling, inferred in bindings.items():
                parts = spelling.split(".")
                if len(parts) != 2 or parts[0] != "self":
                    continue
                key = (f"{module}.{owner.name}", parts[1])
                if fields.get(key) != inferred:
                    fields[key] = inferred
                    changed = True
        if not changed:
            break
    return fields


def _resolver_property_types_from_tree(
    tree: ast.Module,
    *,
    module: str,
    current_package: str,
) -> dict[tuple[str, str], str]:
    aliases = _semantic_aliases(tree, current_package)
    local_definitions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    properties: dict[tuple[str, str], str] = {}
    for owner in tree.body:
        if not isinstance(owner, ast.ClassDef):
            continue
        for method in owner.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.returns is None:
                continue
            if not any(
                isinstance(decorator, ast.Name) and decorator.id == "property" for decorator in method.decorator_list
            ):
                continue
            inferred = _semantic_annotation_type(
                method.returns,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
            )
            if inferred is not None:
                properties[(f"{module}.{owner.name}", method.name)] = inferred
    return properties


def _resolver_scope_bindings(
    tree: ast.Module,
    node: ast.AST,
    *,
    module: str,
    current_package: str,
    returns: dict[str, str],
    fields: dict[tuple[str, str], str],
) -> tuple[dict[str, str], dict[str, str], set[str], dict[ast.AST, ast.AST]]:
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    aliases = _visible_semantic_aliases(
        tree,
        parents=parents,
        node=node,
        current_package=current_package,
    )
    local_definitions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    function = _enclosing_function(parents, node)
    assert function is not None
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
    return bindings, aliases, local_definitions, parents


def _resolver_receiver_kind(
    expression: ast.expr,
    *,
    bindings: dict[str, str],
    aliases: dict[str, str],
    module: str,
    local_definitions: set[str],
    returns: dict[str, str],
    fields: dict[tuple[str, str], str],
    properties: dict[tuple[str, str], str],
) -> str | None:
    if isinstance(expression, ast.Attribute):
        owner = _expression_type(
            expression.value,
            bindings=bindings,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
            returns=returns,
            fields=fields,
        )
        if owner is not None and properties.get((owner, expression.attr)) == _RESOLVER_TYPE:
            return "property"
    if isinstance(expression, ast.Subscript):
        container = _expression_type(
            expression.value,
            bindings=bindings,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
            returns=returns,
            fields=fields,
        )
        if container == _RESOLVER_CONTAINER_TYPE:
            return "subscript"
    inferred = _expression_type(
        expression,
        bindings=bindings,
        aliases=aliases,
        module=module,
        local_definitions=local_definitions,
        returns=returns,
        fields=fields,
    )
    return "direct" if inferred == _RESOLVER_TYPE else None


def _constant_member_name(expression: ast.expr) -> str | None:
    return expression.value if isinstance(expression, ast.Constant) and isinstance(expression.value, str) else None


def _is_builtin_reference(
    expression: ast.expr,
    expected: object,
    *,
    aliases: dict[str, str],
    module: str,
    local_definitions: set[str],
    shadowed: set[str],
) -> bool:
    expected_name = _object_name(expected)
    if isinstance(expression, ast.Name):
        if expression.id == expected_name and expression.id not in aliases:
            return expression.id not in shadowed
        return aliases.get(expression.id) == f"builtins.{expected_name}"
    reference = _semantic_reference(
        expression,
        aliases=aliases,
        module=module,
        local_definitions=local_definitions,
    )
    return reference == f"builtins.{expected_name}" and _semantic_object(reference) is expected


def _reflected_owner_and_member(
    node: ast.AST,
    *,
    aliases: dict[str, str],
    module: str,
    local_definitions: set[str],
    shadowed: set[str],
) -> tuple[ast.expr, str] | None:
    if isinstance(node, ast.Call):
        if (
            _is_builtin_reference(
                node.func,
                getattr,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
                shadowed=shadowed,
            )
            and len(node.args) >= 2
        ):
            member = _constant_member_name(node.args[1])
            return (node.args[0], member) if member is not None else None
        if isinstance(node.func, ast.Attribute) and node.args:
            if node.func.attr == "__getattribute__":
                member = _constant_member_name(node.args[0])
                return (node.func.value, member) if member is not None else None
            if node.func.attr in {"get", "__getitem__"}:
                dictionary = node.func.value
                member = _constant_member_name(node.args[0])
                if member is None:
                    return None
                if isinstance(dictionary, ast.Attribute) and dictionary.attr == "__dict__":
                    return dictionary.value, member
                if (
                    isinstance(dictionary, ast.Call)
                    and _is_builtin_reference(
                        dictionary.func,
                        vars,
                        aliases=aliases,
                        module=module,
                        local_definitions=local_definitions,
                        shadowed=shadowed,
                    )
                    and dictionary.args
                ):
                    return dictionary.args[0], member
    if isinstance(node, ast.Subscript):
        member = _constant_member_name(node.slice)
        if member is None:
            return None
        dictionary = node.value
        if isinstance(dictionary, ast.Attribute) and dictionary.attr == "__dict__":
            return dictionary.value, member
        if (
            isinstance(dictionary, ast.Call)
            and _is_builtin_reference(
                dictionary.func,
                vars,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
                shadowed=shadowed,
            )
            and dictionary.args
        ):
            return dictionary.args[0], member
    return None


def _resolver_usage_violations_from_tree(
    tree: ast.Module,
    *,
    module: str,
    current_package: str,
    returns: dict[str, str],
    inherited_fields: dict[tuple[str, str], str],
) -> list[tuple[str, int, str]]:
    resolver = _object("agentworks.secrets.resolver", "Resolver")
    fields = _instance_field_types_from_tree(
        tree,
        module=module,
        current_package=current_package,
        returns=returns,
        inherited_fields=inherited_fields,
    )
    properties = _resolver_property_types_from_tree(
        tree,
        module=module,
        current_package=current_package,
    )
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    violations: list[tuple[str, int, str]] = []
    protected = {"resolve", "resolve_gate", "resolve_late_repair"}
    local_definitions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for node in ast.walk(tree):
        if not (
            isinstance(node, (ast.Call, ast.Subscript)) or isinstance(node, ast.Attribute) and node.attr in protected
        ):
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
        if reflected is not None and reflected[1] in protected:
            owner_expression, _member = reflected
            owner_reference = _semantic_reference(
                owner_expression,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
            )
            if owner_reference is not None and _semantic_object(owner_reference) is resolver:
                violations.append((ast.unparse(node), cast(int, vars(node)["lineno"]), "class-style-reflection"))
                continue
            function = _enclosing_function(parents, node)
            if function is not None:
                bindings, aliases, local_definitions, _scope_parents = _resolver_scope_bindings(
                    tree,
                    node,
                    module=module,
                    current_package=current_package,
                    returns=returns,
                    fields=fields,
                )
                kind = _resolver_receiver_kind(
                    owner_expression,
                    bindings=bindings,
                    aliases=aliases,
                    module=module,
                    local_definitions=local_definitions,
                    returns=returns,
                    fields=fields,
                    properties=properties,
                )
                if kind is not None:
                    violations.append(
                        (ast.unparse(node), cast(int, vars(node)["lineno"]), "unsupported-dynamic-lookup")
                    )
                    continue
        if isinstance(node, ast.Attribute) and node.attr in protected:
            owner_reference = _semantic_reference(
                node.value,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
            )
            if owner_reference is not None and _semantic_object(owner_reference) is resolver:
                parent = parents.get(node)
                reason = (
                    "class-style-call"
                    if isinstance(parent, ast.Call) and parent.func is node
                    else "class-style-extraction"
                )
                violations.append((ast.unparse(node), node.lineno, reason))
                continue
            function = _enclosing_function(parents, node)
            if function is None:
                continue
            bindings, aliases, local_definitions, _scope_parents = _resolver_scope_bindings(
                tree,
                node,
                module=module,
                current_package=current_package,
                returns=returns,
                fields=fields,
            )
            kind = _resolver_receiver_kind(
                node.value,
                bindings=bindings,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
                returns=returns,
                fields=fields,
                properties=properties,
            )
            if kind is None:
                continue
            parent = parents.get(node)
            direct_call = isinstance(parent, ast.Call) and parent.func is node
            if kind != "direct" or not direct_call:
                reason = f"unsupported-{kind}" if kind != "direct" else "first-class-extraction"
                violations.append((ast.unparse(node), node.lineno, reason))
    return sorted(violations, key=lambda violation: violation[1])


def _semantic_call_name(
    call: ast.Call,
    *,
    aliases: dict[str, str],
    module: str,
    local_definitions: set[str],
) -> str | None:
    if isinstance(call.func, ast.Name):
        if call.func.id in aliases:
            return aliases[call.func.id]
        if call.func.id in local_definitions:
            return f"{module}.{call.func.id}"
        return None
    if not isinstance(call.func, ast.Attribute):
        return None
    parts = _dotted_attribute(call.func)
    if parts is None or parts[0] not in aliases:
        return None
    return ".".join((aliases[parts[0]], *parts[1:]))
