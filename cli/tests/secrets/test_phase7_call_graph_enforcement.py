"""Exact caller and storage graph enforcement pins for Phase 7."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import cast

import pytest

from tests.secrets.phase7_call_graph_support import _actual_boundary_call_edges
from tests.secrets.phase7_lexical_support import _interaction_call_edges
from tests.secrets.phase7_stored_support import _stored_call_edges
from tests.secrets.test_phase7_enforcement import (
    _CLI_MANIFEST,
    _DIRECTED_CALLEE_ALIASES,
    _DIRECTED_EDGE_MANIFEST,
    _INTERNAL_MANIFEST,
    _SERVICE_MANIFEST,
    _STORED_INVOCATION_MANIFEST,
    _STORED_POLICY_MANIFEST,
    _VERIFY_CLI_MANIFEST,
    _combined_entries,
    _function_node,
    _invoke_with_opaque_arguments,
    _object,
)


def test_ast_derived_boundary_caller_graph_exactly_matches_literal_edges() -> None:
    assert _actual_boundary_call_edges() == _DIRECTED_EDGE_MANIFEST


@pytest.mark.parametrize(
    ("module_name", "constructor"),
    (
        ("agentworks.secrets.resolver", "Resolver"),
        ("agentworks.workspaces.nodes", "PendingWorkspaceNode"),
        ("agentworks.agents.nodes", "PendingAgentNode"),
    ),
)
def test_every_policy_storage_owner_runtime_stores_the_exact_sentinel(
    module_name: str,
    constructor: str,
) -> None:
    from agentworks.secrets.policy import InteractionPolicy

    sentinel = InteractionPolicy.REFUSE
    instance = _invoke_with_opaque_arguments(_object(module_name, constructor), sentinel)
    assert vars(instance)["_interaction"] is sentinel


@pytest.mark.parametrize(
    ("module_name", "class_name", "method_name", "arguments"),
    _STORED_INVOCATION_MANIFEST,
)
def test_every_stored_policy_boundary_runtime_revalidates_exact_sentinel(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    class_name: str,
    method_name: str,
    arguments: tuple[object, ...],
) -> None:
    from agentworks.secrets.policy import InteractionPolicy

    class _Validated(BaseException):
        pass

    sentinel = InteractionPolicy.REFUSE
    seen: list[object] = []
    stop = _Validated()

    def _validate(value: object) -> object:
        seen.append(value)
        raise stop

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "validate_interaction_policy", _validate)
    owner = cast(type[object], _object(module_name, class_name))
    instance = owner.__new__(owner)
    vars(instance)["_interaction"] = sentinel
    with pytest.raises(_Validated) as caught:
        getattr(instance, method_name)(*arguments)
    assert caught.value is stop
    assert seen == [sentinel]


def test_every_manifest_owner_reaches_a_declared_policy_or_storage_seam() -> None:
    interaction_edges = _interaction_call_edges()
    stored_edges = _stored_call_edges()
    owners = set(interaction_edges) | set(stored_edges)
    final_owners = {
        ("agentworks.secrets.resolver", "Resolver.__init__"),
        ("agentworks.workspaces.nodes", "PendingWorkspaceNode.__init__"),
        ("agentworks.agents.nodes", "PendingAgentNode.__init__"),
    }
    terminal_index: dict[str, set[tuple[str, str]]] = {}
    for owner in owners | final_owners:
        name = owner[1].rsplit(".", 1)[-1]
        terminal_index.setdefault(name, set()).add(owner)
        if name == "__init__":
            terminal_index.setdefault(owner[1].split(".", 1)[0], set()).add(owner)

    def _reaches_seam(owner: tuple[str, str], seen: set[tuple[str, str]]) -> bool:
        if owner in final_owners:
            return True
        if owner in seen:
            return False
        seen.add(owner)
        for callee, keyword in interaction_edges.get(owner, ()):
            assert keyword == "interaction"
            terminal = callee.rsplit(".", 1)[-1]
            terminal = _DIRECTED_CALLEE_ALIASES.get(terminal, terminal)
            if terminal in {"ResolutionPolicy", "_preview"}:
                return True
            if any(_reaches_seam(target, seen.copy()) for target in terminal_index.get(terminal, ())):
                return True
        for callee in stored_edges.get(owner, ()):
            if _reaches_seam(("agentworks.secrets.resolver", callee), seen.copy()):
                return True
        return False

    roots = set(
        _combined_entries(
            _SERVICE_MANIFEST,
            _INTERNAL_MANIFEST,
            _CLI_MANIFEST,
            _VERIFY_CLI_MANIFEST,
            _STORED_POLICY_MANIFEST,
        )
    )
    assert {root for root in roots if not _reaches_seam(root, set())} == set()

    actual_graph: dict[tuple[str, str], set[tuple[str, str] | str]] = {}
    for owner in owners:
        targets = actual_graph.setdefault(owner, set())
        for callee, _keyword in interaction_edges.get(owner, ()):
            terminal = _DIRECTED_CALLEE_ALIASES.get(callee.rsplit(".", 1)[-1], callee.rsplit(".", 1)[-1])
            if terminal in {"ResolutionPolicy", "_preview"}:
                targets.add(terminal)
            else:
                targets.update(terminal_index.get(terminal, ()))
        for callee in stored_edges.get(owner, ()):
            targets.add(("agentworks.secrets.resolver", callee))

    reverse: dict[tuple[str, str] | str, set[tuple[str, str]]] = {}
    for caller, targets in actual_graph.items():
        for target in targets:
            reverse.setdefault(target, set()).add(caller)
    frontier: list[tuple[str, str] | str] = ["ResolutionPolicy", "_preview", *final_owners]
    reverse_callers: set[tuple[str, str]] = set()
    while frontier:
        target = frontier.pop()
        for caller in reverse.get(target, ()):
            if caller not in reverse_callers:
                reverse_callers.add(caller)
                frontier.append(caller)
    assert reverse_callers == owners - final_owners


def test_reverse_signature_closure_matches_literal_boundary_manifests() -> None:
    root = Path(__file__).parents[2] / "agentworks"
    discovered: set[tuple[str, str]] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        module = ".".join(("agentworks", *path.relative_to(root).with_suffix("").parts))
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
            parameters = (*function.args.args, *function.args.kwonlyargs)
            if not any(parameter.arg == "interaction" for parameter in parameters):
                continue
            if module == "agentworks.secrets.preview" and function.name == "_preview":
                continue
            parent = parents.get(function)
            name = f"{parent.name}.{function.name}" if isinstance(parent, ast.ClassDef) else function.name
            discovered.add((module, name))
    expected = set(_combined_entries(_SERVICE_MANIFEST, _INTERNAL_MANIFEST))
    assert discovered == expected


def test_policy_storage_and_teardown_edges_preserve_identity_shape() -> None:
    for module_name, constructor in (
        ("agentworks.secrets.resolver", "Resolver.__init__"),
        ("agentworks.workspaces.nodes", "PendingWorkspaceNode.__init__"),
        ("agentworks.agents.nodes", "PendingAgentNode.__init__"),
    ):
        tree = _function_node(_object(module_name, constructor))
        stores = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Attribute) and target.attr == "_interaction" for target in node.targets)
        ]
        assert len(stores) == 1
        assert isinstance(stores[0].value, ast.Name) and stores[0].value.id == "interaction"

    for module_name, teardown, callee in (
        ("agentworks.workspaces.nodes", "PendingWorkspaceNode.teardown", "delete_workspace"),
        ("agentworks.agents.nodes", "PendingAgentNode.teardown", "delete_agent"),
    ):
        tree = _function_node(_object(module_name, teardown))
        calls = [
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == callee
        ]
        assert len(calls) == 1
        forwarded = next(keyword.value for keyword in calls[0].keywords if keyword.arg == "interaction")
        assert isinstance(forwarded, ast.Name) and forwarded.id == "interaction"
