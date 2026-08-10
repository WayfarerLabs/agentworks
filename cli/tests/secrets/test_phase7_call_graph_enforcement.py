"""Exact caller and storage graph enforcement pins for Phase 7."""

from __future__ import annotations

import ast
import importlib
from typing import cast

import pytest

from tests.secrets.phase7_lexical_support import (
    _interaction_call_edges,
    _interaction_calls_from_tree,
    _object,
    _validated_interaction_edges,
)
from tests.secrets.phase7_stored_support import _stored_call_edges
from tests.secrets.test_phase7_enforcement import (
    _STORED_INVOCATION_MANIFEST,
    _cli_boundary_entries,
    _function_node,
    _invoke_with_opaque_arguments,
    _parameter_boundary_entries,
    _stored_policy_entries,
)


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


def test_every_discovered_owner_reaches_a_policy_or_storage_seam() -> None:
    interaction_edges = _interaction_call_edges()
    stored_edges = _stored_call_edges()
    owners = set(interaction_edges) | set(stored_edges)
    final_owners = {
        ("agentworks.secrets.resolver", "Resolver.__init__"),
        ("agentworks.workspaces.nodes", "PendingWorkspaceNode.__init__"),
        ("agentworks.agents.nodes", "PendingAgentNode.__init__"),
    }
    boundary_index = {
        _object(module_name, function_name): (module_name, function_name)
        for module_name, function_name in _parameter_boundary_entries()
    }
    boundary_index.update(
        {
            _object(module_name, function_name.removesuffix(".__init__")): (module_name, function_name)
            for module_name, function_name in final_owners
        }
    )
    seams = {
        _object("agentworks.secrets.resolve", "ResolutionPolicy"),
        _object("agentworks.secrets.preview", "_preview"),
    }

    def _reaches_seam(owner: tuple[str, str], seen: set[tuple[str, str]]) -> bool:
        if owner in final_owners:
            return True
        if owner in seen:
            return False
        seen.add(owner)
        for target in interaction_edges.get(owner, ()):
            if target in seams:
                return True
            target_owner = boundary_index.get(target)
            assert target_owner is not None, (owner, target)
            if _reaches_seam(target_owner, seen.copy()):
                return True
        for callee in stored_edges.get(owner, ()):
            if _reaches_seam(("agentworks.secrets.resolver", callee), seen.copy()):
                return True
        return False

    roots = set(_parameter_boundary_entries()) | set(_cli_boundary_entries()) | set(_stored_policy_entries())
    assert {root for root in roots if not _reaches_seam(root, set())} == set()

    actual_graph: dict[tuple[str, str], set[tuple[str, str] | object]] = {}
    for owner in owners:
        targets = actual_graph.setdefault(owner, set())
        for target in interaction_edges.get(owner, ()):
            if target in seams:
                targets.add(target)
            else:
                target_owner = boundary_index.get(target)
                assert target_owner is not None, (owner, target)
                targets.add(target_owner)
        for callee in stored_edges.get(owner, ()):
            targets.add(("agentworks.secrets.resolver", callee))

    reverse: dict[tuple[str, str] | object, set[tuple[str, str]]] = {}
    for caller, targets in actual_graph.items():
        for target in targets:
            reverse.setdefault(target, set()).add(caller)
    frontier: list[tuple[str, str] | object] = [*seams, *final_owners]
    reverse_callers: set[tuple[str, str]] = set()
    while frontier:
        target = frontier.pop()
        for caller in reverse.get(target, ()):
            if caller not in reverse_callers:
                reverse_callers.add(caller)
                frontier.append(caller)
    assert reverse_callers == owners - final_owners


def test_policy_call_domain_does_not_depend_on_the_forwarding_keyword() -> None:
    tree = ast.parse(
        """
from agentworks.vms.manager.power import delete_vm

def cli_root(policy_kwargs):
    delete_vm(None, None, "vm", **policy_kwargs)
"""
    )
    sites = _interaction_calls_from_tree(
        tree,
        module="fixture.cli",
        current_package="fixture",
    )
    assert tuple(site.owner for site in sites) == (("fixture.cli", "cli_root"),)
    with pytest.raises(AssertionError, match="one explicit keyword"):
        _validated_interaction_edges(sites)


def test_policy_call_domain_rejects_a_same_named_local_seam() -> None:
    tree = ast.parse(
        """
from agentworks.secrets.resolve import CompletionPolicy, ResolutionPolicy

def owner(interaction):
    class ResolutionPolicy:
        def __init__(self, *, interaction, completion):
            self.interaction = interaction
            self.completion = completion

    ResolutionPolicy(
        interaction=interaction,
        completion=CompletionPolicy.COMPLETE,
    )
"""
    )
    with pytest.raises(AssertionError, match="unresolved policy call"):
        _interaction_calls_from_tree(
            tree,
            module="fixture.policy",
            current_package="fixture",
        )


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
