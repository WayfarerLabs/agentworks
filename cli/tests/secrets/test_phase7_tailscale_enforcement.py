"""Tailscale secret-source boundary enforcement pins for Phase 7."""

from __future__ import annotations

import ast
import inspect
import textwrap

from tests.secrets.phase7_call_graph_support import (
    _production_protected_callable_alias_violations,
    _production_semantic_target_calls,
    _protected_callable_alias_violations_from_tree,
    _semantic_target_calls_from_tree,
)
from tests.secrets.test_phase7_enforcement import (
    _TAILSCALE_ENSURE_FORBIDDEN_TARGETS,
    _TAILSCALE_SOURCE_EDGE_MANIFEST,
    _TAILSCALE_STANDALONE_EDGE_MANIFEST,
    _function_node,
    _object,
    _object_name,
)


def test_tailscale_ensure_has_only_explicit_reader_and_name() -> None:
    from agentworks.vms.manager.tailscale import _ensure_tailscale

    signature = inspect.signature(_ensure_tailscale)
    assert tuple(signature.parameters) == (
        "db",
        "config",
        "vm",
        "platform",
        "ctx",
        "auth_keys",
        "auth_key_name",
    )
    assert signature.parameters["auth_keys"].default is inspect.Parameter.empty
    assert signature.parameters["auth_key_name"].default is inspect.Parameter.empty
    tree = ast.parse(textwrap.dedent(inspect.getsource(_ensure_tailscale)))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not {"InteractionPolicy", "resolve_for_command", "Registry", "resolve_template"} & (names | attrs)
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "auth_keys"
        and node.func.attr == "get"
    ]
    assert len(reads) == 1
    assert isinstance(reads[0].args[0], ast.Name) and reads[0].args[0].id == "auth_key_name"


def test_exact_tailscale_source_edges_and_forbidden_false_edges() -> None:
    ensure = _object("agentworks.vms.manager.tailscale", "_ensure_tailscale")
    resolve_for_command = _object("agentworks.secrets.orchestration", "resolve_for_command")
    registry = _object("agentworks.resources.registry", "Registry")
    resolve_template = _object("agentworks.vms.templates", "resolve_template")
    forbidden_targets = (registry, resolve_for_command, resolve_template)
    calls = _production_semantic_target_calls(ensure, *forbidden_targets)
    ensure_calls = [entry for entry in calls if entry[1] is ensure]
    discovered: list[tuple[str, str, str, str, str]] = []
    for owner, target, call in ensure_calls:
        assert target is ensure
        auth_keywords = [keyword.value for keyword in call.keywords if keyword.arg == "auth_keys"]
        assert len(auth_keywords) == 1
        discovered.append((*owner, "_ensure_tailscale", "auth_keys", ast.unparse(auth_keywords[0])))
    assert tuple(sorted(discovered)) == _TAILSCALE_SOURCE_EDGE_MANIFEST

    standalone_calls = [
        (owner[0], owner[1], "resolve_for_command")
        for owner, target, call in calls
        if target is resolve_for_command and any(keyword.arg == "extra_decls" for keyword in call.keywords)
    ]
    assert tuple(standalone_calls) == _TAILSCALE_STANDALONE_EDGE_MANIFEST

    forbidden_names = {_object_name(target): target for target in forbidden_targets}
    assert tuple(forbidden_names) == _TAILSCALE_ENSURE_FORBIDDEN_TARGETS
    forbidden_ensure_edges = [
        (owner, _object_name(target))
        for owner, target, _call in calls
        if owner == ("agentworks.vms.manager.tailscale", "_ensure_tailscale")
    ]
    assert forbidden_ensure_edges == []
    assert _production_protected_callable_alias_violations(ensure, *forbidden_targets) == []


def test_tailscale_semantic_edge_discovery_rejects_third_owner_and_alias_bypass() -> None:
    ensure = _object("agentworks.vms.manager.tailscale", "_ensure_tailscale")
    resolve_for_command = _object("agentworks.secrets.orchestration", "resolve_for_command")
    source = """
from agentworks.secrets import resolve_for_command as acquire_auth_source
from agentworks import vms as vms_surface
from agentworks.vms.manager import _ensure_tailscale as perform_ensure
import agentworks.vms.manager as qualified_manager

def third_owner(db, config, vm, platform, ctx, reader, name):
    perform_ensure(
        db,
        config,
        vm,
        platform,
        ctx,
        auth_keys=reader,
        auth_key_name=name,
    )

def aliased_standalone(config, declaration, interaction, registry):
    return acquire_auth_source(
        [],
        config,
        registry,
        extra_decls=[declaration],
        interaction=interaction,
    )

def reexport_attribute_owner(db, config, vm, platform, ctx, reader, name):
    vms_surface.manager._ensure_tailscale(
        db, config, vm, platform, ctx, auth_keys=reader, auth_key_name=name
    )

def qualified_attribute_owner(db, config, vm, platform, ctx, reader, name):
    qualified_manager._ensure_tailscale(
        db, config, vm, platform, ctx, auth_keys=reader, auth_key_name=name
    )
"""
    calls = _semantic_target_calls_from_tree(
        ast.parse(source),
        module="agentworks._phase7_tailscale_fixture",
        current_package="agentworks",
        targets=(ensure, resolve_for_command),
    )
    assert [(owner, target) for owner, target, _call in calls] == [
        (("agentworks._phase7_tailscale_fixture", "third_owner"), ensure),
        (("agentworks._phase7_tailscale_fixture", "aliased_standalone"), resolve_for_command),
        (("agentworks._phase7_tailscale_fixture", "reexport_attribute_owner"), ensure),
        (("agentworks._phase7_tailscale_fixture", "qualified_attribute_owner"), ensure),
    ]
    assert any(
        target is resolve_for_command and any(keyword.arg == "extra_decls" for keyword in call.keywords)
        for _owner, target, call in calls
    )


def test_tailscale_guard_rejects_callable_alias_assignment_without_rejecting_direct_import_aliases() -> None:
    ensure = _object("agentworks.vms.manager.tailscale", "_ensure_tailscale")
    resolve_for_command = _object("agentworks.secrets.orchestration", "resolve_for_command")
    registry = _object("agentworks.resources.registry", "Registry")
    resolve_template = _object("agentworks.vms.templates", "resolve_template")
    source = """
from agentworks.secrets import resolve_for_command
from agentworks.resources import Registry
from agentworks.vms import manager as vm_manager
from agentworks.vms.manager import _ensure_tailscale as direct_ensure
from agentworks.vms.templates import resolve_template

def direct_import_alias(db, config, vm, platform, ctx, reader, name):
    direct_ensure(db, config, vm, platform, ctx, auth_keys=reader, auth_key_name=name)

def hidden_ensure(db, config, vm, platform, ctx, reader, name):
    perform_ensure = direct_ensure
    perform_ensure(db, config, vm, platform, ctx, auth_keys=reader, auth_key_name=name)

def chained_and_named():
    first = second = vm_manager._ensure_tailscale
    third = (fourth := vm_manager._ensure_tailscale)
    fifth = direct_ensure.__call__

def _ensure_tailscale():
    hidden_resolve = resolve_for_command
    hidden_registry = Registry
    hidden_template = resolve_template
    hidden_resolve()

def unrelated(other_callable):
    alias = other_callable
    alias()
"""
    violations = _protected_callable_alias_violations_from_tree(
        ast.parse(source),
        module="agentworks._phase7_tailscale_alias_fixture",
        current_package="agentworks",
        targets=(ensure, resolve_for_command, registry, resolve_template),
    )
    assert [(expression, target) for expression, _lineno, target in violations] == [
        ("direct_ensure", ensure),
        ("vm_manager._ensure_tailscale", ensure),
        ("vm_manager._ensure_tailscale", ensure),
        ("direct_ensure", ensure),
        ("resolve_for_command", resolve_for_command),
        ("Registry", registry),
        ("resolve_template", resolve_template),
    ]


def test_protected_target_guard_rejects_bounded_reflective_lookup_only() -> None:
    ensure = _object("agentworks.vms.manager.tailscale", "_ensure_tailscale")
    resolve_for_command = _object("agentworks.secrets.orchestration", "resolve_for_command")
    registry = _object("agentworks.resources.registry", "Registry")
    resolve_template = _object("agentworks.vms.templates", "resolve_template")
    source = """
from agentworks import resources
from agentworks.vms import manager, templates
import agentworks.secrets as secrets_surface
import builtins

def hidden_edges():
    getattr(manager, "_ensure_tailscale")
    vars(manager)["_ensure_tailscale"]
    builtins.getattr(manager, "_ensure_tailscale")
    builtins.vars(manager)["_ensure_tailscale"]
    manager.__dict__["_ensure_tailscale"]
    manager.__dict__.get("_ensure_tailscale")
    vars(manager).__getitem__("_ensure_tailscale")
    manager.__getattribute__("_ensure_tailscale")
    getattr(secrets_surface, "resolve_for_command")
    vars(resources)["Registry"]
    templates.__dict__["resolve_template"]

def unrelated_reflection(helper, getattr, vars):
    getattr(manager, "unrelated")
    vars(manager)["unrelated"]
    manager.__dict__.get("unrelated")
    getattr(manager, "_ensure_tailscale")
    vars(manager)["_ensure_tailscale"]
    helper.getattr(manager, "_ensure_tailscale")
    helper.vars(manager)["_ensure_tailscale"]
"""
    violations = _protected_callable_alias_violations_from_tree(
        ast.parse(source),
        module="agentworks._phase7_reflection_fixture",
        current_package="agentworks",
        targets=(ensure, resolve_for_command, registry, resolve_template),
    )
    assert [target for _expression, _lineno, target in violations] == [
        ensure,
        ensure,
        ensure,
        ensure,
        ensure,
        ensure,
        ensure,
        ensure,
        resolve_for_command,
        registry,
        resolve_template,
    ]


def test_module_builtin_visibility_follows_shadow_restore_and_delete_order() -> None:
    ensure = _object("agentworks.vms.manager.tailscale", "_ensure_tailscale")
    source = """
from agentworks.vms import manager
import builtins

getattr(manager, "_ensure_tailscale")
getattr = helper
getattr(manager, "_ensure_tailscale")
from builtins import getattr
getattr(manager, "_ensure_tailscale")
getattr = builtins.getattr
getattr(manager, "_ensure_tailscale")
getattr: object = helper
getattr(manager, "_ensure_tailscale")
del getattr
getattr(manager, "_ensure_tailscale")
def getattr(*args):
    return None
getattr(manager, "_ensure_tailscale")
del getattr
getattr(manager, "_ensure_tailscale")

vars = helper
vars(manager)["_ensure_tailscale"]
del vars
vars(manager)["_ensure_tailscale"]
import unrelated as vars
vars(manager)["_ensure_tailscale"]
from builtins import vars
vars(manager)["_ensure_tailscale"]
vars = builtins.vars
vars(manager)["_ensure_tailscale"]
vars += helper
vars(manager)["_ensure_tailscale"]
"""
    violations = _protected_callable_alias_violations_from_tree(
        ast.parse(source),
        module="agentworks._phase7_module_builtin_visibility_fixture",
        current_package="agentworks",
        targets=(ensure,),
    )
    assert [target for _expression, _lineno, target in violations] == [ensure] * 8


def _called_names(nodes: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in nodes:
        for call in (node for node in ast.walk(statement) if isinstance(node, ast.Call)):
            callee = call.func
            if isinstance(callee, ast.Name):
                names.add(callee.id)
            elif isinstance(callee, ast.Attribute):
                names.add(callee.attr)
    return names


def test_tailscale_acquisition_and_ensure_are_lexically_inside_existing_holds() -> None:
    start = _function_node(_object("agentworks.vms.manager.power", "start_vm"))
    start_holds = [
        node
        for node in ast.walk(start)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "hold_active"
            for item in node.items
        )
    ]
    assert len(start_holds) == 1
    start_inside = _called_names(start_holds[0].body)
    assert {
        "_tailscale_rejoin_required",
        "resolve_template",
        "_lookup_or_synthesize_secret",
        "resolve_for_command",
        "ScopedSecrets",
        "_ensure_tailscale",
        "clear",
    } <= start_inside
    outside = [statement for statement in start.body if statement is not start_holds[0]]
    assert not {
        "_tailscale_rejoin_required",
        "resolve_template",
        "_lookup_or_synthesize_secret",
        "resolve_for_command",
        "ScopedSecrets",
        "_ensure_tailscale",
    } & _called_names(outside)

    auto_start = _function_node(_object("agentworks.vms.nodes", "LiveVMNode.auto_start"))
    gate_holds = [
        node
        for node in ast.walk(auto_start)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "vm_active"
            for item in node.items
        )
    ]
    assert len(gate_holds) == 1
    gate_inside = _called_names(gate_holds[0].body)
    assert {
        "_tailscale_rejoin_required",
        "repair_secret_refs",
        "_gate_ops_ctx",
        "_ensure_tailscale",
    } <= gate_inside
    gate_outside = [statement for statement in auto_start.body if statement is not gate_holds[0]]
    assert not {
        "_tailscale_rejoin_required",
        "repair_secret_refs",
        "_ensure_tailscale",
    } & _called_names(gate_outside)
    assert "resolve_for_command" not in _called_names(list(ast.iter_child_nodes(auto_start)))
    ensure_calls = [
        node
        for node in ast.walk(gate_holds[0])
        if isinstance(node, ast.Call) and (isinstance(node.func, ast.Name) and node.func.id == "_ensure_tailscale")
    ]
    assert len(ensure_calls) == 1
    auth_reader = next(keyword.value for keyword in ensure_calls[0].keywords if keyword.arg == "auth_keys")
    assert isinstance(auth_reader, ast.Name) and auth_reader.id == "gate_secrets"
