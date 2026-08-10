"""Structural guards for the Phase 7 interaction and retired-seam closure."""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import Enum, StrEnum
from functools import cache
from pathlib import Path
from typing import cast

import pytest

from tests.secrets.phase7_lexical_support import (
    _interaction_calls,
    _object,
)

# The immediate follow-up retains this narrow exact-owner pin for the credential boundary.
_TAILSCALE_SOURCE_EDGE_MANIFEST = (
    ("agentworks.vms.manager.power", "start_vm", "_ensure_tailscale", "auth_keys", "auth_keys"),
    (
        "agentworks.vms.nodes",
        "LiveVMNode.auto_start",
        "_ensure_tailscale",
        "auth_keys",
        "gate_secrets",
    ),
)

# This narrow exact-owner pin keeps standalone resolution out of reusable VM lifecycle code.
_TAILSCALE_STANDALONE_EDGE_MANIFEST = (("agentworks.vms.manager.power", "start_vm", "resolve_for_command"),)

_TAILSCALE_ENSURE_FORBIDDEN_TARGETS = (
    "Registry",
    "resolve_for_command",
    "resolve_template",
)

_RETIRED_SYMBOLS = {
    "ActiveBackend",
    "SecretInteractionPolicy",
    "SecretVerification",
    "_compatibility_error",
    "_inspection_projection",
    "_resolve_complete_for_legacy_callers",
    "_verification_compatibility_error",
    "active_backends",
    "disabled_plugin_backends",
    "resolve_secrets",
    "resolve_secrets_quiet",
    "verify_named_secret",
}

_OLD_MODULES = {
    "agentworks.secrets.backends",
    "agentworks.secrets.env_var",
    "agentworks.secrets.prompt",
}

_FORBIDDEN_ROOT_IMPORTS = {
    "ActiveSource",
    "OutputInteractionBroker",
    "active_sources",
    "CompletionPolicy",
    "ResolutionPolicy",
    "env_var_name_for",
}

_PACKAGE_EXPORT_MANIFEST = (
    "InteractionPolicy",
    "ResolutionCategory",
    "ResolutionDetail",
    "ResolutionOutcome",
    "ResolutionRemediation",
    "SecretConfig",
    "SecretDecl",
    "SecretSourceDecl",
    "SecretTarget",
    "compute_needed_secrets",
    "guide_contributions",
    "publish_builtin_secret_sources",
    "resolve_for_command",
    "validate_chain",
)


def _first_statement(value: object) -> ast.stmt:
    tree = ast.parse(textwrap.dedent(inspect.getsource(value)))
    function = next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    body = function.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return body[0]


def _function_node(value: object) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(value)))
    return next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))


@cache
def _parameter_boundary_entries() -> tuple[tuple[str, str], ...]:
    """Discover every production boundary that accepts interaction policy."""
    root = Path(__file__).parents[2] / "agentworks"
    discovered: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).with_suffix("")
        module_parts = relative.parts[:-1] if relative.parts[-1] == "__init__" else relative.parts
        module = ".".join(("agentworks", *module_parts))
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            parameters = (*function.args.args, *function.args.kwonlyargs)
            if not any(parameter.arg == "interaction" for parameter in parameters):
                continue
            if module == "agentworks.secrets.preview" and function.name == "_preview":
                continue
            names = [function.name]
            current = parents.get(function)
            while isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(current.name)
                current = parents.get(current)
            discovered.append((module, ".".join(reversed(names))))
    return tuple(sorted(discovered))


_VERIFY_CLI_BOUNDARY = ("agentworks.cli.commands.secret", "secret_verify")


@cache
def _cli_boundary_entries() -> tuple[tuple[str, str], ...]:
    """Discover CLI roots from the signatures of their production callees."""
    discovered = {
        site.owner
        for site in _interaction_calls()
        if site.owner[0].startswith("agentworks.cli.commands.")
        and "interaction" not in inspect.signature(_object(*site.owner)).parameters
    }
    return tuple(sorted(discovered))


def _ordinary_cli_boundary_entries() -> tuple[tuple[str, str], ...]:
    return tuple(entry for entry in _cli_boundary_entries() if entry != _VERIFY_CLI_BOUNDARY)


_STORED_INVOCATION_MANIFEST = (
    ("agentworks.secrets.resolver", "Resolver", "resolve", ()),
    ("agentworks.secrets.resolver", "Resolver", "resolve_gate", (object(),)),
    ("agentworks.secrets.resolver", "Resolver", "resolve_late_repair", (object(),)),
    ("agentworks.workspaces.nodes", "PendingWorkspaceNode", "teardown", ()),
    ("agentworks.agents.nodes", "PendingAgentNode", "teardown", ()),
)


def _stored_policy_entries() -> tuple[tuple[str, str], ...]:
    return tuple(
        (module_name, f"{class_name}.{method_name}")
        for module_name, class_name, method_name, _arguments in _STORED_INVOCATION_MANIFEST
    )


def test_boundary_discovery_is_non_vacuous_across_supported_shapes() -> None:
    assert {
        ("agentworks.secrets.resolver", "Resolver.__init__"),
        ("agentworks.orchestration.readiness", "preflight_all"),
        ("agentworks.cli.commands.session", "_resume_sessions"),
    } <= set(_parameter_boundary_entries())
    assert {
        ("agentworks.cli.commands.vm", "vm_create"),
        ("agentworks.cli.commands.session", "session_list"),
        _VERIFY_CLI_BOUNDARY,
    } <= set(_cli_boundary_entries())


def _assert_validation_assignment(statement: ast.stmt, *, ordinary: bool) -> None:
    assert isinstance(statement, ast.Assign)
    assert len(statement.targets) == 1
    assert isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == "interaction"
    call = statement.value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    assert call.func.id == "validate_interaction_policy"
    assert len(call.args) == 1
    if ordinary:
        source = call.args[0]
        assert isinstance(source, ast.Call) and isinstance(source.func, ast.Name)
        assert source.func.id == "ordinary_interaction_policy"
    else:
        assert isinstance(call.args[0], ast.Name) and call.args[0].id == "interaction"


def _assert_stored_validation_assignment(statement: ast.stmt) -> None:
    assert isinstance(statement, ast.Assign)
    assert len(statement.targets) == 1
    assert isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == "interaction"
    call = statement.value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    assert call.func.id == "validate_interaction_policy" and len(call.args) == 1
    source = call.args[0]
    assert isinstance(source, ast.Attribute)
    assert isinstance(source.value, ast.Name) and source.value.id == "self"
    assert source.attr == "_interaction"


@pytest.mark.parametrize(("module_name", "function_name"), _parameter_boundary_entries())
def test_discovered_parameter_boundary_requires_and_first_validates_exact_policy(
    module_name: str,
    function_name: str,
) -> None:
    function = _object(module_name, function_name)
    signature = inspect.signature(function)
    parameter = signature.parameters["interaction"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation in {
        "InteractionPolicy",
        importlib.import_module("agentworks.secrets.policy").InteractionPolicy,
    }
    _assert_validation_assignment(_first_statement(function), ordinary=False)


@pytest.mark.parametrize(("module_name", "function_name"), _stored_policy_entries())
def test_stored_policy_manifest_revalidates_as_first_statement(
    module_name: str,
    function_name: str,
) -> None:
    _assert_stored_validation_assignment(_first_statement(_object(module_name, function_name)))


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    _ordinary_cli_boundary_entries(),
)
def test_ordinary_cli_manifest_derives_policy_once_as_first_statement(
    module_name: str,
    function_name: str,
) -> None:
    _assert_validation_assignment(_first_statement(_object(module_name, function_name)), ordinary=True)


@pytest.mark.parametrize(("module_name", "function_name"), (_VERIFY_CLI_BOUNDARY,))
def test_verify_cli_manifest_selects_and_validates_explicit_policy_first(
    module_name: str,
    function_name: str,
) -> None:
    statement = _first_statement(_object(module_name, function_name))
    assert isinstance(statement, ast.Assign)
    assert len(statement.targets) == 1
    assert isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == "interaction"
    validation = statement.value
    assert isinstance(validation, ast.Call) and isinstance(validation.func, ast.Name)
    assert validation.func.id == "validate_interaction_policy"
    assert len(validation.args) == 1 and isinstance(validation.args[0], ast.IfExp)
    selected = validation.args[0]
    assert isinstance(selected.test, ast.Name) and selected.test.id == "allow_interaction"
    assert ast.unparse(selected.body) == "InteractionPolicy.ALLOW"
    assert ast.unparse(selected.orelse) == "InteractionPolicy.REFUSE"


class _ForeignEnum(Enum):
    REFUSE = "refuse"


class _ForeignStrEnum(StrEnum):
    REFUSE = "refuse"


class _StringSubclass(str):
    pass


class _Lookalike:
    value = "refuse"


_REJECTED_POLICIES: tuple[object, ...] = (
    "refuse",
    _ForeignEnum.REFUSE,
    _ForeignStrEnum.REFUSE,
    _StringSubclass("refuse"),
    _Lookalike(),
)


def _invoke_with_opaque_arguments(function: object, interaction: object) -> object:
    positional: list[object] = []
    keywords: dict[str, object] = {}
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            continue
        if parameter.name == "interaction":
            keywords[parameter.name] = interaction
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
            positional.append(object())
        else:
            keywords[parameter.name] = object()
    cast_function = cast(Callable[..., object], function)
    result = cast_function(*positional, **keywords)
    if hasattr(result, "__enter__") and hasattr(result, "__exit__"):
        with cast(AbstractContextManager[object], result):
            pass
    return result


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    _parameter_boundary_entries(),
)
@pytest.mark.parametrize("rejected", _REJECTED_POLICIES)
def test_every_parameter_boundary_rejects_wrong_type_before_opaque_work(
    module_name: str,
    function_name: str,
    rejected: object,
) -> None:
    from agentworks.errors import StateError

    with pytest.raises(StateError) as caught:
        _invoke_with_opaque_arguments(_object(module_name, function_name), rejected)
    assert str(caught.value) == "interaction must be an exact InteractionPolicy"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert caught.value.entity_kind is None and caught.value.entity_name is None and caught.value.hint is None


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    _parameter_boundary_entries(),
)
def test_every_parameter_boundary_runtime_revalidates_the_exact_sentinel(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    function_name: str,
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
    with pytest.raises(_Validated) as caught:
        _invoke_with_opaque_arguments(_object(module_name, function_name), sentinel)
    assert caught.value is stop
    assert seen == [sentinel]


@pytest.mark.parametrize(("module_name", "class_name", "method_name", "arguments"), _STORED_INVOCATION_MANIFEST)
@pytest.mark.parametrize("rejected", _REJECTED_POLICIES)
def test_every_stored_policy_boundary_rejects_corruption_before_work(
    module_name: str,
    class_name: str,
    method_name: str,
    arguments: tuple[object, ...],
    rejected: object,
) -> None:
    from agentworks.errors import StateError

    owner = cast(type[object], _object(module_name, class_name))
    instance = owner.__new__(owner)
    vars(instance)["_interaction"] = rejected
    with pytest.raises(StateError) as caught:
        getattr(instance, method_name)(*arguments)
    assert str(caught.value) == "interaction must be an exact InteractionPolicy"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert caught.value.entity_kind is None and caught.value.entity_name is None and caught.value.hint is None


@pytest.mark.parametrize(("module_name", "function_name"), _ordinary_cli_boundary_entries())
def test_every_cli_root_rejects_wrong_derivation_before_opaque_work(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    function_name: str,
) -> None:
    from agentworks.errors import StateError

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "ordinary_interaction_policy", lambda: "refuse")
    with pytest.raises(StateError, match="interaction must be an exact InteractionPolicy") as caught:
        _invoke_with_opaque_arguments(_object(module_name, function_name), object())
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_every_preflight_caller_forwards_the_validated_local() -> None:
    from pathlib import Path

    from tests.secrets.phase7_lexical_support import _enclosing_function

    root = Path(__file__).parents[2] / "agentworks"
    discovered: set[tuple[str, str]] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            callee = call.func
            if not isinstance(callee, ast.Name) or callee.id != "preflight_all":
                continue
            function = _enclosing_function(parents, call)
            assert function is not None
            module = ".".join(("agentworks", *path.relative_to(root).with_suffix("").parts))
            discovered.add((module, function.name))
            interaction = next((keyword.value for keyword in call.keywords if keyword.arg == "interaction"), None)
            assert isinstance(interaction, ast.Name) and interaction.id == "interaction"
    # Per-call forwarding is asserted above; this only prevents a vacuous scan.
    assert discovered
    assert discovered <= set(_parameter_boundary_entries())


def test_discovered_forwarding_edges_use_only_the_validated_local() -> None:
    """Every discovered policy-bearing edge forwards the validated local."""
    from tests.secrets.phase7_lexical_support import _interaction_call_edges

    # The helper validates every semantically discovered edge; this only pins
    # that production still contains at least one policy-bearing edge.
    assert _interaction_call_edges()
