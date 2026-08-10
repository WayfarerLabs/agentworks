"""Resolver storage and indirection enforcement pins for Phase 7."""

from __future__ import annotations

import ast

import pytest

from tests.secrets.phase7_lexical_support import (
    _interaction_binding_sites,
    _object,
    _semantic_type_indexes,
)
from tests.secrets.phase7_resolver_support import _resolver_usage_violations_from_tree
from tests.secrets.phase7_stored_support import (
    _resolver_usage_violations,
    _stored_call_edges,
    _stored_call_edges_from_tree,
)
from tests.secrets.test_phase7_enforcement import (
    _cli_boundary_entries,
    _function_node,
    _parameter_boundary_entries,
    _stored_policy_entries,
)


def test_stored_policy_callback_callers_follow_the_supported_resolver_seam() -> None:
    # Each discovered call is validated by the semantic scanner; this only
    # prevents the production domain from becoming wholly empty.
    assert _stored_call_edges()
    assert _resolver_usage_violations() == []


def test_semantic_stored_callback_discovery_rejects_arbitrary_resolver_receiver_names() -> None:
    source = """
from agentworks.secrets.resolver import Resolver as OperationResolver

def unmanifested_local(config, registry, interaction):
    operation_resolver = OperationResolver(config, registry, interaction=interaction)
    operation_resolver.resolve()

def unmanifested_attribute(config, holder, registry, interaction):
    holder.operation_resolver = OperationResolver(config, registry, interaction=interaction)
    holder.operation_resolver.resolve()

class ResolverHolder:
    def __init__(self, config, registry, interaction):
        self.operation_resolver = OperationResolver(config, registry, interaction=interaction)

    def resolve_later(self):
        self.operation_resolver.resolve()
"""
    returns, fields = _semantic_type_indexes()
    assert _stored_call_edges_from_tree(
        ast.parse(source),
        module="agentworks._phase7_stored_fixture",
        current_package="agentworks",
        returns=returns,
        fields=fields,
    ) == {
        ("agentworks._phase7_stored_fixture", "unmanifested_local"): ("Resolver.resolve",),
        ("agentworks._phase7_stored_fixture", "unmanifested_attribute"): ("Resolver.resolve",),
        ("agentworks._phase7_stored_fixture", "ResolverHolder.resolve_later"): ("Resolver.resolve",),
    }


def test_resolver_usage_guard_rejects_callable_and_unsupported_receiver_indirection() -> None:
    source = """
from agentworks.secrets.resolver import Resolver, Resolver as OperationResolver, Resolver as ResolverKwargs
import agentworks.secrets.resolver as resolver_module

module_finish = OperationResolver.resolve
getattr(OperationResolver, "resolve_gate")(object(), object())
resolver_module.Resolver.__dict__["resolve_late_repair"](object(), object())

class PropertyHolder:
    @property
    def operation_resolver(self) -> OperationResolver:
        raise AssertionError

    def resolve_through_property(self):
        self.operation_resolver.resolve()

def unsupported(config, registry, interaction):
    operation_resolver = OperationResolver(config, registry, interaction=interaction)
    finish = operation_resolver.resolve
    resolvers = [operation_resolver]
    resolvers[0].resolve()
    getattr(operation_resolver, "resolve")()
    OperationResolver.resolve(operation_resolver)
    OperationResolver.resolve_gate(operation_resolver, object())
    resolver_module.Resolver.resolve_late_repair(operation_resolver, object())
    class_finish = OperationResolver.resolve

def unrelated(path, paths):
    finish = path.resolve
    finish()
    paths[0].resolve()
    getattr(path, "resolve")()

class UnrelatedClass:
    @classmethod
    def resolve(cls):
        return None

def unrelated_class_method():
    UnrelatedClass.resolve()
    unrelated_finish = UnrelatedClass.resolve

def shadowed_aliases(Resolver, value, /, *OperationResolver, resolver_module, **ResolverKwargs):
    Resolver.resolve(value)
    OperationResolver.resolve(value)
    resolver_module.Resolver.resolve(value)
    ResolverKwargs.resolve(value)

def nested_shadow_control():
    def inner(Resolver, value):
        Resolver.resolve(value)

unrelated_module_finish = UnrelatedClass.resolve
getattr(UnrelatedClass, "resolve")(object())
UnrelatedClass.__dict__["resolve"](object())
"""
    tree = ast.parse(source)
    returns, fields = _semantic_type_indexes()
    violations = _resolver_usage_violations_from_tree(
        tree,
        module="agentworks._phase7_resolver_indirection_fixture",
        current_package="agentworks",
        returns=returns,
        inherited_fields=fields,
    )
    assert [reason for _expression, _lineno, reason in violations] == [
        "class-style-extraction",
        "class-style-reflection",
        "class-style-reflection",
        "unsupported-property",
        "first-class-extraction",
        "unsupported-subscript",
        "unsupported-dynamic-lookup",
        "class-style-call",
        "class-style-call",
        "class-style-call",
        "class-style-extraction",
    ]
    assert all("unrelated" not in expression for expression, _lineno, _reason in violations)


def test_module_resolver_visibility_follows_binding_and_deletion_order() -> None:
    source = """
from agentworks.secrets.resolver import Resolver

before_shadow = Resolver.resolve
Resolver = Unrelated
after_assignment = Resolver.resolve
from agentworks.secrets.resolver import Resolver
after_reimport = Resolver.resolve
Resolver: object = Unrelated
after_annassign = Resolver.resolve
from agentworks.secrets.resolver import Resolver
Resolver += Unrelated
after_augassign = Resolver.resolve
from agentworks.secrets.resolver import Resolver
import unrelated as Resolver
after_import_replacement = Resolver.resolve
from agentworks.secrets.resolver import Resolver
class Resolver:
    pass
after_definition = Resolver.resolve
from agentworks.secrets.resolver import Resolver
del Resolver
after_delete = Resolver.resolve
from agentworks.secrets.resolver import Resolver
after_restore = Resolver.resolve
global Resolver
after_global = Resolver.resolve
"""
    returns, fields = _semantic_type_indexes()
    violations = _resolver_usage_violations_from_tree(
        ast.parse(source),
        module="agentworks._phase7_module_resolver_visibility_fixture",
        current_package="agentworks",
        returns=returns,
        inherited_fields=fields,
    )
    assert [(expression, reason) for expression, _lineno, reason in violations] == [
        ("Resolver.resolve", "class-style-extraction"),
        ("Resolver.resolve", "class-style-extraction"),
        ("Resolver.resolve", "class-style-extraction"),
        ("Resolver.resolve", "class-style-extraction"),
    ]


def test_validated_interaction_local_is_never_rebound_in_any_boundary() -> None:
    entries = set(_parameter_boundary_entries()) | set(_cli_boundary_entries()) | set(_stored_policy_entries())
    for module_name, function_name in entries:
        function = _function_node(_object(module_name, function_name))
        bindings = _interaction_binding_sites(function)
        assert len(bindings) == 1, (module_name, function_name, bindings)
        assert bindings[0][0] == "store", (module_name, function_name, bindings)


@pytest.mark.parametrize(
    "source",
    (
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    interaction = InteractionPolicy.REFUSE
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    from fixture import other_policy as interaction
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    import fixture as interaction
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    (interaction := replacement)
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    for interaction in values:
        pass
""",
        """async def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    async for interaction in values:
        pass
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    with context() as interaction:
        pass
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    try:
        work()
    except Error as interaction:
        pass
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    values = [item for interaction in items]
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    match value:
        case interaction:
            pass
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    match value:
        case [*interaction]:
            pass
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    match value:
        case {**interaction}:
            pass
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    def interaction():
        pass
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    class interaction:
        pass
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    del interaction
""",
        """def fixture(interaction):
    global interaction
    interaction = validate_interaction_policy(interaction)
""",
        """def outer():
    interaction = object()
    def fixture(value):
        nonlocal interaction
        interaction = validate_interaction_policy(value)
""",
        """def fixture(interaction):
    interaction = validate_interaction_policy(interaction)
    def mutate_policy():
        nonlocal interaction
        interaction = InteractionPolicy.REFUSE
""",
    ),
)
def test_interaction_binding_scanner_rejects_every_python_rebinding_form(source: str) -> None:
    tree = ast.parse(source)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "fixture"
    ]
    assert len(functions) == 1
    assert len(_interaction_binding_sites(functions[0])) > 1


@pytest.mark.parametrize(
    ("module_name", "constructor"),
    (
        ("agentworks.secrets.resolver", "Resolver"),
        ("agentworks.workspaces.nodes", "PendingWorkspaceNode"),
        ("agentworks.agents.nodes", "PendingAgentNode"),
    ),
)
def test_every_storage_owner_stores_only_the_validated_named_local(
    module_name: str,
    constructor: str,
) -> None:
    function = _function_node(_object(module_name, constructor))
    stores = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
        and node.targets[0].attr == "_interaction"
    ]
    assert len(stores) == 1
    assert isinstance(stores[0].value, ast.Name) and stores[0].value.id == "interaction"
