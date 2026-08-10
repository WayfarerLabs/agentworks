"""Lexical and semantic AST support for Phase 7 enforcement tests."""

from __future__ import annotations

import ast
import importlib
import inspect
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol, cast


class _NamedObject(Protocol):
    __name__: str


_RESOLVER_TYPE = "agentworks.secrets.resolver.Resolver"
_RESOLVER_CONTAINER_TYPE = f"{_RESOLVER_TYPE}[]"


def _object_name(value: object) -> str:
    return cast(_NamedObject, value).__name__


def _object(module_name: str, dotted_name: str) -> object:
    value: object = importlib.import_module(module_name)
    for part in dotted_name.split("."):
        value = getattr(value, part)
    return value


@dataclass(frozen=True)
class _ProductionModule:
    """One parsed production module shared by every structural contract."""

    module: str
    current_package: str
    tree: ast.Module
    parents: dict[ast.AST, ast.AST]


@dataclass(frozen=True)
class _InteractionCall:
    """One semantically resolved call to a policy-bearing callable."""

    owner: tuple[str, str]
    target: object
    target_name: str
    call: ast.Call


@cache
def _production_modules() -> tuple[_ProductionModule, ...]:
    root = Path(__file__).parents[2] / "agentworks"
    modules: list[_ProductionModule] = []
    for path in sorted(root.rglob("*.py")):
        module, current_package = _module_identity(path, root)
        tree = ast.parse(path.read_text())
        modules.append(
            _ProductionModule(
                module=module,
                current_package=current_package,
                tree=tree,
                parents={child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)},
            )
        )
    return tuple(modules)


def _enclosing_function(
    parents: dict[ast.AST, ast.AST], node: ast.AST
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current = parents.get(node)
    while current is not None and not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
        current = parents.get(current)
    return current if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _qualified_function_name(parents: dict[ast.AST, ast.AST], function: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    names = [function.name]
    current = parents.get(function)
    while isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names))


def _interaction_calls_from_tree(
    tree: ast.Module,
    *,
    module: str,
    current_package: str,
) -> tuple[_InteractionCall, ...]:
    """Find policy-bearing calls independently of how policy is forwarded."""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    local_definitions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    discovered: list[_InteractionCall] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        aliases = _visible_semantic_aliases(
            tree,
            parents=parents,
            node=call,
            current_package=current_package,
        )
        target_name = _semantic_call_name(
            call,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
        )
        explicit_policy = any(keyword.arg == "interaction" for keyword in call.keywords)
        target = _semantic_object(target_name) if target_name is not None else None
        if target is None:
            assert not explicit_policy, (module, call.lineno, ast.unparse(call.func), "unresolved policy call")
            continue
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            assert not explicit_policy, (module, call.lineno, target_name, "uninspectable policy call")
            continue
        if "interaction" not in signature.parameters:
            assert not explicit_policy, (module, call.lineno, target_name, "unexpected policy keyword")
            continue
        function = _enclosing_function(parents, call)
        assert function is not None
        discovered.append(
            _InteractionCall(
                owner=(module, _qualified_function_name(parents, function)),
                target=target,
                target_name=target_name,
                call=call,
            )
        )
    return tuple(discovered)


@cache
def _interaction_calls() -> tuple[_InteractionCall, ...]:
    return tuple(
        call
        for source in _production_modules()
        for call in _interaction_calls_from_tree(
            source.tree,
            module=source.module,
            current_package=source.current_package,
        )
    )


def _validated_interaction_edges(
    sites: tuple[_InteractionCall, ...],
) -> dict[tuple[str, str], tuple[object, ...]]:
    """Validate semantically discovered policy-forwarding edges."""
    preview = _object("agentworks.secrets.preview", "_preview")
    discovered: dict[tuple[str, str], list[object]] = {}
    for site in sites:
        keywords = [keyword for keyword in site.call.keywords if keyword.arg == "interaction"]
        assert len(keywords) == 1, (
            *site.owner,
            site.target_name,
            "interaction must be forwarded as one explicit keyword",
        )
        forwarded = keywords[0].value
        if site.owner == ("agentworks.secrets.preview", "preview_resolution") and site.target is preview:
            assert isinstance(forwarded, ast.Constant) and forwarded.value is None
            continue
        assert isinstance(forwarded, ast.Name) and forwarded.id == "interaction", (
            *site.owner,
            site.target_name,
            ast.unparse(forwarded),
        )
        discovered.setdefault(site.owner, []).append(site.target)
    return {owner: tuple(edges) for owner, edges in discovered.items()}


def _interaction_call_edges() -> dict[tuple[str, str], tuple[object, ...]]:
    """Return qualified edges after validating every discovered call."""
    return _validated_interaction_edges(_interaction_calls())


def _module_identity(path: Path, root: Path) -> tuple[str, str]:
    relative = path.relative_to(root).with_suffix("")
    if relative.parts[-1] == "__init__":
        module = ".".join(("agentworks", *relative.parts[:-1]))
        return module, module
    module = ".".join(("agentworks", *relative.parts))
    return module, module.rsplit(".", 1)[0]


def _semantic_aliases(tree: ast.Module, current_package: str) -> dict[str, str]:
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
    return aliases


@cache
def _function_lexical_bindings(function: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    bindings = {
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }
    if function.args.vararg is not None:
        bindings.add(function.args.vararg.arg)
    if function.args.kwarg is not None:
        bindings.add(function.args.kwarg.arg)
    declarations: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not function:
                bindings.add(node.name)
                return
        elif isinstance(node, ast.Import):
            bindings.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            bindings.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name is not None:
            bindings.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            bindings.add(node.rest)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            declarations.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings.add(node.id)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in function.body:
        visit(statement)
    return frozenset(bindings - declarations)


@cache
def _module_semantic_events(
    tree: ast.Module,
    current_package: str,
) -> tuple[tuple[tuple[int, int], str, str, str | ast.expr | None], ...]:
    """Return source-ordered module bindings without rescanning per call site."""
    events: list[tuple[tuple[int, int], str, str, str | ast.expr | None]] = []

    def position(value: ast.AST) -> tuple[int, int]:
        return (
            cast(int, vars(value).get("end_lineno", vars(value)["lineno"])),
            cast(int, vars(value).get("end_col_offset", vars(value).get("col_offset", 0))),
        )

    def target_names(target: ast.expr) -> tuple[str, ...]:
        if isinstance(target, ast.Name):
            return (target.id,)
        if isinstance(target, (ast.Tuple, ast.List)):
            return tuple(name for element in target.elts for name in target_names(element))
        if isinstance(target, ast.Starred):
            return target_names(target.value)
        return ()

    def bind(names: tuple[str, ...], value: ast.AST) -> None:
        events.extend((position(value), "shadow", name, None) for name in names)

    def assign(names: tuple[str, ...], value: ast.expr, statement: ast.AST) -> None:
        events.extend((position(statement), "assign", name, value) for name in names)

    def visit(statement: ast.AST) -> None:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                name = alias.asname or alias.name.split(".", 1)[0]
                semantic_target = alias.name if alias.asname else alias.name.split(".", 1)[0]
                events.append((position(statement), "alias", name, semantic_target))
            return
        if isinstance(statement, ast.ImportFrom):
            base = _resolved_from_module(current_package, statement)
            for alias in statement.names:
                events.append((position(statement), "alias", alias.asname or alias.name, f"{base}.{alias.name}"))
            return
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind((statement.name,), statement)
            return
        if isinstance(statement, ast.Assign):
            for assignment_target in statement.targets:
                assign(target_names(assignment_target), statement.value, statement)
        elif isinstance(statement, ast.AnnAssign):
            if statement.value is None:
                bind(target_names(statement.target), statement)
            else:
                assign(target_names(statement.target), statement.value, statement)
        elif isinstance(statement, ast.AugAssign):
            bind(target_names(statement.target), statement)
        elif isinstance(statement, ast.NamedExpr):
            assign(target_names(statement.target), statement.value, statement)
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            bind(target_names(statement.target), statement.target)
        elif isinstance(statement, ast.With):
            for item in statement.items:
                if item.optional_vars is not None:
                    bind(target_names(item.optional_vars), item.optional_vars)
        elif isinstance(statement, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and statement.name is not None:
            bind((statement.name,), statement)
        elif isinstance(statement, ast.MatchMapping) and statement.rest is not None:
            bind((statement.rest,), statement)
        elif isinstance(statement, ast.Delete):
            for deletion_target in statement.targets:
                events.extend((position(statement), "delete", name, None) for name in target_names(deletion_target))
        for child in ast.iter_child_nodes(statement):
            visit(child)

    for statement in tree.body:
        visit(statement)
    return tuple(sorted(events, key=lambda event: event[0]))


def _module_semantic_state(
    tree: ast.Module,
    node: ast.AST,
    current_package: str,
) -> tuple[dict[str, str], set[str]]:
    aliases: dict[str, str] = {}
    shadowed: set[str] = set()
    node_position = (
        cast(int, vars(node)["lineno"]),
        cast(int, vars(node).get("col_offset", 0)),
    )
    for event_position, kind, name, target in _module_semantic_events(tree, current_package):
        if event_position >= node_position:
            continue
        if kind == "alias":
            assert isinstance(target, str)
            aliases[name] = target
            shadowed.discard(name)
        elif kind == "assign":
            assert isinstance(target, ast.expr)
            reference = _semantic_reference(
                target,
                aliases=aliases,
                module="",
                local_definitions=set(),
            )
            if reference is None:
                aliases.pop(name, None)
                shadowed.add(name)
            else:
                aliases[name] = reference
                shadowed.discard(name)
        elif kind == "shadow":
            aliases.pop(name, None)
            shadowed.add(name)
        else:
            aliases.pop(name, None)
            shadowed.discard(name)
    return aliases, shadowed


def _lexically_shadowed_names(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
    node: ast.AST,
    current_package: str,
) -> set[str]:
    _aliases, shadowed = _module_semantic_state(tree, node, current_package)
    function = _enclosing_function(parents, node)
    while function is not None:
        shadowed.update(_function_lexical_bindings(function))
        function = _enclosing_function(parents, function)
    return shadowed


def _visible_semantic_aliases(
    tree: ast.Module,
    *,
    parents: dict[ast.AST, ast.AST],
    node: ast.AST,
    current_package: str,
) -> dict[str, str]:
    aliases, _module_shadowed = _module_semantic_state(tree, node, current_package)

    def record(statement: ast.Import | ast.ImportFrom) -> None:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = (
                    alias.name if alias.asname else alias.name.split(".", 1)[0]
                )
        else:
            base = _resolved_from_module(current_package, statement)
            for alias in statement.names:
                aliases[alias.asname or alias.name] = f"{base}.{alias.name}"

    ancestry: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    function = _enclosing_function(parents, node)
    while function is not None:
        ancestry.append(function)
        function = _enclosing_function(parents, function)
    node_lineno = cast(int, vars(node)["lineno"])
    for scope in reversed(ancestry):
        for binding in _function_lexical_bindings(scope):
            aliases.pop(binding, None)
        events = cast(
            list[ast.Import | ast.ImportFrom | ast.Name],
            [
                candidate
                for candidate in _scope_nodes(scope)
                if isinstance(candidate, (ast.Import, ast.ImportFrom, ast.Name))
                and cast(int, vars(candidate)["lineno"]) <= node_lineno
            ],
        )
        for event in sorted(events, key=lambda candidate: (candidate.lineno, candidate.col_offset)):
            if isinstance(event, (ast.Import, ast.ImportFrom)):
                record(event)
            elif isinstance(event.ctx, (ast.Store, ast.Del)):
                aliases.pop(event.id, None)
    return aliases


def _semantic_reference(
    expression: ast.expr,
    *,
    aliases: dict[str, str],
    module: str,
    local_definitions: set[str],
) -> str | None:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        try:
            parsed = ast.parse(expression.value, mode="eval").body
        except SyntaxError:
            return None
        return _semantic_reference(
            parsed,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
        )
    if isinstance(expression, ast.Subscript):
        return _semantic_reference(
            expression.value,
            aliases=aliases,
            module=module,
            local_definitions=local_definitions,
        )
    if isinstance(expression, ast.Name):
        if expression.id in aliases:
            return aliases[expression.id]
        if expression.id in local_definitions:
            return f"{module}.{expression.id}"
        return None
    if isinstance(expression, ast.Attribute):
        parts = _dotted_attribute(expression)
        if parts is not None and parts[0] in aliases:
            return ".".join((aliases[parts[0]], *parts[1:]))
    return None


def _semantic_call_name(
    call: ast.Call,
    *,
    aliases: dict[str, str],
    module: str,
    local_definitions: set[str],
) -> str | None:
    """Resolve a direct call target to its qualified semantic name."""
    return _semantic_reference(
        call.func,
        aliases=aliases,
        module=module,
        local_definitions=local_definitions,
    )


def _semantic_annotation_type(
    expression: ast.expr,
    *,
    aliases: dict[str, str],
    module: str,
    local_definitions: set[str],
) -> str | None:
    resolver = _object("agentworks.secrets.resolver", "Resolver")
    if isinstance(expression, ast.Subscript):
        elements = expression.slice.elts if isinstance(expression.slice, ast.Tuple) else (expression.slice,)
        for element in elements:
            inferred = _semantic_annotation_type(
                element,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
            )
            if inferred == _RESOLVER_TYPE:
                return _RESOLVER_CONTAINER_TYPE
    elif isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.BitOr):
        for element in (expression.left, expression.right):
            inferred = _semantic_annotation_type(
                element,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
            )
            if inferred is not None:
                return inferred
    reference = _semantic_reference(
        expression,
        aliases=aliases,
        module=module,
        local_definitions=local_definitions,
    )
    if reference is not None and _semantic_object(reference) is resolver:
        return _RESOLVER_TYPE
    return reference


@cache
def _semantic_object(target: str) -> object | None:
    parts = target.split(".")
    for boundary in range(len(parts), 0, -1):
        try:
            value: object = importlib.import_module(".".join(parts[:boundary]))
        except ModuleNotFoundError:
            continue
        try:
            for attribute in parts[boundary:]:
                value = getattr(value, attribute)
        except AttributeError:
            continue
        return value
    return None


@cache
def _scope_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.AST, ...]:
    nodes: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            visit(child)

    for statement in function.body:
        visit(statement)
    return tuple(nodes)


def _binding_targets(target: ast.expr) -> tuple[str, ...]:
    if isinstance(target, (ast.Name, ast.Attribute)):
        return (ast.unparse(target),)
    if isinstance(target, (ast.Tuple, ast.List)):
        return tuple(item for element in target.elts for item in _binding_targets(element))
    return ()


def _interaction_binding_sites(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, int]]:
    bindings: list[tuple[str, int]] = []

    def record(name: str | None, kind: str, node: ast.AST) -> None:
        if name == "interaction":
            bindings.append((kind, cast(int, vars(node)["lineno"])))

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not function:
                record(node.name, "definition", node)
                for descendant in ast.walk(node):
                    if isinstance(descendant, ast.Nonlocal) and "interaction" in descendant.names:
                        bindings.append(("nested-nonlocal", descendant.lineno))
                return
        elif isinstance(node, ast.Import):
            for alias in node.names:
                record(alias.asname or alias.name.split(".", 1)[0], "import", node)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                record(alias.asname or alias.name, "import-from", node)
                if alias.name == "*":
                    bindings.append(("wildcard-import", node.lineno))
        elif isinstance(node, ast.ExceptHandler):
            record(node.name, "exception-target", node)
        elif isinstance(node, ast.MatchAs):
            record(node.name, "match-as", node)
        elif isinstance(node, ast.MatchStar):
            record(node.name, "match-star", node)
        elif isinstance(node, ast.MatchMapping):
            record(node.rest, "match-rest", node)
        elif isinstance(node, ast.Global) and "interaction" in node.names:
            bindings.append(("global", node.lineno))
        elif isinstance(node, ast.Nonlocal) and "interaction" in node.names:
            bindings.append(("nonlocal", node.lineno))
        elif isinstance(node, ast.Name) and node.id == "interaction" and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings.append((type(node.ctx).__name__.lower(), node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in function.body:
        visit(statement)
    return bindings


@cache
def _semantic_type_indexes() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    returns: dict[str, str] = {}
    fields: dict[tuple[str, str], str] = {}
    for source in _production_modules():
        tree = source.tree
        module = source.module
        current_package = source.current_package
        aliases = _semantic_aliases(tree, current_package)
        local_definitions = {
            statement.name
            for statement in tree.body
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.returns is not None:
                annotation = _semantic_annotation_type(
                    statement.returns,
                    aliases=aliases,
                    module=module,
                    local_definitions=local_definitions,
                )
                if annotation is not None:
                    returns[f"{module}.{statement.name}"] = annotation
            elif isinstance(statement, ast.ClassDef):
                owner = f"{module}.{statement.name}"
                for field in statement.body:
                    if not isinstance(field, ast.AnnAssign) or not isinstance(field.target, ast.Name):
                        continue
                    annotation = _semantic_annotation_type(
                        field.annotation,
                        aliases=aliases,
                        module=module,
                        local_definitions=local_definitions,
                    )
                    if annotation is not None:
                        fields[(owner, field.target.id)] = annotation
    return returns, fields


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
