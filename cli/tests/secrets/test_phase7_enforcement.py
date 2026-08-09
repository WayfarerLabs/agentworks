"""Structural guards for the Phase 7 interaction and retired-seam closure."""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import Enum, StrEnum
from typing import Protocol, cast

import pytest


class _NamedObject(Protocol):
    __name__: str


def _object_name(value: object) -> str:
    return cast(_NamedObject, value).__name__


# Post-0.14, consolidate these literal exact-caller pins behind a reusable graph contract.
_SERVICE_MANIFEST = {
    "agentworks.secrets.resolver": ("Resolver.__init__",),
    "agentworks.secrets.orchestration": ("resolve_for_command",),
    "agentworks.env.show": ("show_env",),
    "agentworks.secrets.verification": ("verify_secrets",),
    "agentworks.vms.manager.lifecycle": ("create_vm", "reinit_vm"),
    "agentworks.vms.manager.power": ("rekey_vm", "describe_vm", "start_vm", "stop_vm", "delete_vm"),
    "agentworks.vms.backup": ("backup_vm",),
    "agentworks.vms.manager.exec": ("shell_vm", "exec_vm", "add_git_credential"),
    "agentworks.vms.manager.tailscale": ("port_forward_vm",),
    "agentworks.workspaces.manager.create": ("create_workspace",),
    "agentworks.workspaces.manager.repair": ("repair_workspace",),
    "agentworks.workspaces.manager.rehome": ("rehome_workspace",),
    "agentworks.workspaces.manager.copy": ("copy_workspace",),
    "agentworks.workspaces.manager.delete": ("delete_workspace",),
    "agentworks.agents.manager.lifecycle": ("create_agent", "reinit_agent", "delete_agent"),
    "agentworks.agents.manager.access": ("shell_agent", "exec_agent"),
    "agentworks.agents.grants": ("grant_workspaces", "revoke_workspaces"),
    "agentworks.sessions.manager._create": ("create_session",),
    "agentworks.sessions.manager._lifecycle": (
        "resume_session",
        "resume_all_sessions",
        "stop_session",
        "stop_all_sessions",
    ),
    "agentworks.sessions.manager._queries": (
        "delete_session",
        "describe_session",
        "attach_session",
        "list_sessions",
    ),
    "agentworks.sessions.manager._logs": ("session_logs",),
    "agentworks.sessions.multi_console.attach": ("attach_console",),
    "agentworks.sessions.multi_console.restore": ("restore_session",),
    "agentworks.sessions.multi_console.crud": ("add_sessions", "add_shell"),
}

_CLI_MANIFEST = {
    "agentworks.cli.commands.env": ("env_show",),
    "agentworks.cli.commands.vm": (
        "vm_create",
        "vm_backup",
        "vm_describe",
        "vm_start",
        "vm_stop",
        "vm_delete",
        "vm_rekey",
        "vm_reinit",
        "vm_exec",
        "vm_shell",
        "vm_port_forward",
        "vm_add_git_credential",
    ),
    "agentworks.cli.commands.workspace": (
        "workspace_create",
        "workspace_rehome",
        "workspace_repair",
        "workspace_delete",
        "workspace_copy",
    ),
    "agentworks.cli.commands.agent": (
        "agent_create",
        "agent_reinit",
        "agent_grant_workspaces",
        "agent_revoke_workspaces",
        "agent_exec",
        "agent_shell",
        "agent_delete",
    ),
    "agentworks.cli.commands.session": (
        "session_create",
        "session_describe",
        "session_list",
        "session_stop",
        "session_resume",
        "session_attach",
        "session_delete",
        "session_logs",
    ),
    "agentworks.cli.commands.console": (
        "console_attach",
        "console_add_sessions",
        "console_add_shell",
        "console_restore_session",
    ),
}

_VERIFY_CLI_MANIFEST = {
    "agentworks.cli.commands.secret": ("secret_verify",),
}

_INTERNAL_MANIFEST = {
    "agentworks.env.show": ("_reveal_values",),
    "agentworks.orchestration.secrets": ("predict_resolution", "require_predicted_refs"),
    "agentworks.orchestration.readiness": ("preflight_all",),
    "agentworks.secrets.preview": ("preview_operation_resolution",),
    "agentworks.secrets.resolve": ("resolve_partial_for_reveal",),
    "agentworks.vms.manager.boundary": ("gated_vm_boundary", "_live_vm_boundary"),
    "agentworks.vms.manager.lifecycle": ("_create_vm",),
    "agentworks.workspaces.manager.rehome": ("_rehome_vm",),
    "agentworks.sessions.multi_console.attach": ("_prepare_vm_target_for_attach",),
    "agentworks.sessions.manager._create_build": ("_build_session_graph",),
    "agentworks.sessions.manager._scope": ("_prepare_vm", "_batch_vm_boundary"),
    "agentworks.sessions.manager._create": ("_preflight_and_resolve",),
    "agentworks.sessions.manager._queries": ("_cleanup_now_empty_workspace", "_cleanup_now_empty_agent"),
    "agentworks.cli.commands.session": ("_resume_sessions",),
    "agentworks.workspaces.nodes": ("PendingWorkspaceNode.__init__", "pending_workspace_node"),
    "agentworks.agents.nodes": ("PendingAgentNode.__init__", "pending_agent_node"),
}

_STORED_POLICY_MANIFEST = {
    "agentworks.secrets.resolver": ("Resolver.resolve", "Resolver.resolve_gate", "Resolver.resolve_late_repair"),
    "agentworks.workspaces.nodes": ("PendingWorkspaceNode.teardown",),
    "agentworks.agents.nodes": ("PendingAgentNode.teardown",),
}

# Post-0.14, consolidate this literal exact-caller pin behind a reusable graph contract.
_PREFLIGHT_CALLER_MANIFEST = {
    ("agentworks.sessions.manager._create", "_preflight_and_resolve"),
    ("agentworks.sessions.manager._lifecycle", "resume_session"),
    ("agentworks.sessions.manager._scope", "_batch_vm_boundary"),
    ("agentworks.workspaces.manager.create", "create_workspace"),
    ("agentworks.vms.manager.exec", "add_git_credential"),
    ("agentworks.vms.manager.power", "rekey_vm"),
    ("agentworks.vms.manager.boundary", "gated_vm_boundary"),
    ("agentworks.vms.manager.boundary", "_live_vm_boundary"),
    ("agentworks.vms.manager.lifecycle", "_create_vm"),
    ("agentworks.vms.manager.lifecycle", "reinit_vm"),
    ("agentworks.agents.manager.lifecycle", "create_agent"),
    ("agentworks.agents.manager.lifecycle", "reinit_agent"),
}

# Post-0.14, consolidate this literal exact-caller pin behind a reusable graph contract.
_DIRECTED_EDGE_MANIFEST: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("agentworks.agents.grants", "grant_workspaces"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.agents.grants", "revoke_workspaces"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.agents.manager.access", "exec_agent"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.agents.manager.access", "shell_agent"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.agents.manager.lifecycle", "create_agent"): (
        ("Resolver", "interaction"),
        ("pending_agent_node", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.agents.manager.lifecycle", "delete_agent"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.agents.manager.lifecycle", "reinit_agent"): (
        ("Resolver", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.agents.nodes", "PendingAgentNode.teardown"): (("delete_agent", "interaction"),),
    ("agentworks.agents.nodes", "pending_agent_node"): (("PendingAgentNode", "interaction"),),
    ("agentworks.cli.commands.agent", "agent_create"): (("create_agent", "interaction"),),
    ("agentworks.cli.commands.agent", "agent_delete"): (("delete_agent", "interaction"),),
    ("agentworks.cli.commands.agent", "agent_exec"): (("exec_agent", "interaction"),),
    ("agentworks.cli.commands.agent", "agent_grant_workspaces"): (("grant_workspaces", "interaction"),),
    ("agentworks.cli.commands.agent", "agent_reinit"): (("reinit_agent", "interaction"),),
    ("agentworks.cli.commands.agent", "agent_revoke_workspaces"): (("revoke_workspaces", "interaction"),),
    ("agentworks.cli.commands.agent", "agent_shell"): (("shell_agent", "interaction"),),
    ("agentworks.cli.commands.console", "console_add_sessions"): (("add_sessions", "interaction"),),
    ("agentworks.cli.commands.console", "console_add_shell"): (("add_shell", "interaction"),),
    ("agentworks.cli.commands.console", "console_attach"): (("attach_console", "interaction"),),
    ("agentworks.cli.commands.console", "console_restore_session"): (("restore_session", "interaction"),),
    ("agentworks.cli.commands.env", "env_show"): (("show_env", "interaction"),),
    ("agentworks.cli.commands.secret", "secret_verify"): (("verify_secrets", "interaction"),),
    ("agentworks.cli.commands.session", "_resume_sessions"): (
        ("resume_all_sessions", "interaction"),
        ("resume_session", "interaction"),
    ),
    ("agentworks.cli.commands.session", "session_attach"): (("attach_session", "interaction"),),
    ("agentworks.cli.commands.session", "session_create"): (("create_session", "interaction"),),
    ("agentworks.cli.commands.session", "session_delete"): (("delete_session", "interaction"),),
    ("agentworks.cli.commands.session", "session_describe"): (("describe_session", "interaction"),),
    ("agentworks.cli.commands.session", "session_list"): (("list_sessions", "interaction"),),
    ("agentworks.cli.commands.session", "session_logs"): (("_session_logs", "interaction"),),
    ("agentworks.cli.commands.session", "session_resume"): (("_resume_sessions", "interaction"),),
    ("agentworks.cli.commands.session", "session_stop"): (
        ("stop_all_sessions", "interaction"),
        ("stop_session", "interaction"),
    ),
    ("agentworks.cli.commands.vm", "vm_add_git_credential"): (("add_git_credential", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_backup"): (("backup_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_create"): (("create_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_delete"): (("delete_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_describe"): (("describe_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_exec"): (("exec_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_port_forward"): (("port_forward_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_reinit"): (("reinit_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_rekey"): (("rekey_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_shell"): (("shell_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_start"): (("start_vm", "interaction"),),
    ("agentworks.cli.commands.vm", "vm_stop"): (("stop_vm", "interaction"),),
    ("agentworks.cli.commands.workspace", "workspace_copy"): (("copy_workspace", "interaction"),),
    ("agentworks.cli.commands.workspace", "workspace_create"): (("create_workspace", "interaction"),),
    ("agentworks.cli.commands.workspace", "workspace_delete"): (("delete_workspace", "interaction"),),
    ("agentworks.cli.commands.workspace", "workspace_rehome"): (("rehome_workspace", "interaction"),),
    ("agentworks.cli.commands.workspace", "workspace_repair"): (("repair_workspace", "interaction"),),
    ("agentworks.env.show", "_reveal_values"): (("resolve_partial_for_reveal", "interaction"),),
    ("agentworks.env.show", "show_env"): (("_reveal_values", "interaction"),),
    ("agentworks.orchestration.readiness", "preflight_all"): (("require_predicted_refs", "interaction"),),
    ("agentworks.orchestration.secrets", "predict_resolution"): (("preview_operation_resolution", "interaction"),),
    ("agentworks.orchestration.secrets", "require_predicted_refs"): (("predict_resolution", "interaction"),),
    ("agentworks.secrets.orchestration", "resolve_for_command"): (("ResolutionPolicy", "interaction"),),
    ("agentworks.secrets.preview", "preview_operation_resolution"): (("_preview", "interaction"),),
    ("agentworks.secrets.resolve", "resolve_partial_for_reveal"): (("ResolutionPolicy", "interaction"),),
    ("agentworks.secrets.resolver", "Resolver.resolve"): (("ResolutionPolicy", "interaction"),),
    ("agentworks.secrets.resolver", "Resolver.resolve_gate"): (("ResolutionPolicy", "interaction"),),
    ("agentworks.secrets.resolver", "Resolver.resolve_late_repair"): (("ResolutionPolicy", "interaction"),),
    ("agentworks.secrets.verification", "verify_secrets"): (("ResolutionPolicy", "interaction"),),
    ("agentworks.sessions.manager._create", "_preflight_and_resolve"): (("preflight_all", "interaction"),),
    ("agentworks.sessions.manager._create", "create_session"): (
        ("_build_session_graph", "interaction"),
        ("_preflight_and_resolve", "interaction"),
    ),
    ("agentworks.sessions.manager._create_build", "_build_session_graph"): (
        ("Resolver", "interaction"),
        ("pending_workspace_node", "interaction"),
        ("pending_agent_node", "interaction"),
    ),
    ("agentworks.sessions.manager._lifecycle", "resume_all_sessions"): (
        ("_mgr._batch_vm_boundary", "interaction"),
        ("resume_session", "interaction"),
    ),
    ("agentworks.sessions.manager._lifecycle", "resume_session"): (
        ("Resolver", "interaction"),
        ("preflight_all", "interaction"),
        ("resolve_for_command", "interaction"),
    ),
    ("agentworks.sessions.manager._lifecycle", "stop_all_sessions"): (("_mgr._batch_vm_boundary", "interaction"),),
    ("agentworks.sessions.manager._lifecycle", "stop_session"): (("_mgr._prepare_vm", "interaction"),),
    ("agentworks.sessions.manager._logs", "session_logs"): (("_mgr._prepare_vm", "interaction"),),
    ("agentworks.sessions.manager._queries", "_cleanup_now_empty_agent"): (("delete_agent", "interaction"),),
    ("agentworks.sessions.manager._queries", "_cleanup_now_empty_workspace"): (("delete_workspace", "interaction"),),
    ("agentworks.sessions.manager._queries", "attach_session"): (("_mgr._prepare_vm", "interaction"),),
    ("agentworks.sessions.manager._queries", "delete_session"): (
        ("_mgr._prepare_vm", "interaction"),
        ("_cleanup_now_empty_workspace", "interaction"),
        ("_cleanup_now_empty_agent", "interaction"),
    ),
    ("agentworks.sessions.manager._queries", "describe_session"): (("_mgr._prepare_vm", "interaction"),),
    ("agentworks.sessions.manager._queries", "list_sessions"): (("_mgr._batch_vm_boundary", "interaction"),),
    ("agentworks.sessions.manager._scope", "_batch_vm_boundary"): (
        ("Resolver", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.sessions.manager._scope", "_prepare_vm"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.sessions.multi_console.attach", "_prepare_vm_target_for_attach"): (
        ("gated_vm_boundary", "interaction"),
    ),
    ("agentworks.sessions.multi_console.attach", "attach_console"): (
        ("_mc._prepare_vm_target_for_attach", "interaction"),
        ("resolve_for_command", "interaction"),
    ),
    ("agentworks.sessions.multi_console.crud", "add_sessions"): (("resolve_for_command", "interaction"),),
    ("agentworks.sessions.multi_console.crud", "add_shell"): (("resolve_for_command", "interaction"),),
    ("agentworks.sessions.multi_console.restore", "restore_session"): (
        ("_mc._prepare_vm_target_for_attach", "interaction"),
        ("resolve_for_command", "interaction"),
        ("resolve_for_command", "interaction"),
    ),
    ("agentworks.vms.backup", "backup_vm"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.vms.manager.boundary", "_live_vm_boundary"): (
        ("Resolver", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.vms.manager.boundary", "gated_vm_boundary"): (
        ("Resolver", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.vms.manager.exec", "add_git_credential"): (
        ("Resolver", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.vms.manager.exec", "exec_vm"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.vms.manager.exec", "shell_vm"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.vms.manager.lifecycle", "create_vm"): (("_create_vm", "interaction"),),
    ("agentworks.vms.manager.lifecycle", "_create_vm"): (
        ("Resolver", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.vms.manager.lifecycle", "reinit_vm"): (
        ("Resolver", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.vms.manager.power", "delete_vm"): (("_live_vm_boundary", "interaction"),),
    ("agentworks.vms.manager.power", "describe_vm"): (("_live_vm_boundary", "interaction"),),
    ("agentworks.vms.manager.power", "rekey_vm"): (
        ("Resolver", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.vms.manager.power", "start_vm"): (
        ("_live_vm_boundary", "interaction"),
        ("resolve_for_command", "interaction"),
    ),
    ("agentworks.vms.manager.power", "stop_vm"): (("_live_vm_boundary", "interaction"),),
    ("agentworks.vms.manager.tailscale", "port_forward_vm"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.workspaces.manager.copy", "copy_workspace"): (
        ("gated_vm_boundary", "interaction"),
        ("gated_vm_boundary", "interaction"),
    ),
    ("agentworks.workspaces.manager.create", "create_workspace"): (
        ("Resolver", "interaction"),
        ("pending_workspace_node", "interaction"),
        ("preflight_all", "interaction"),
    ),
    ("agentworks.workspaces.manager.delete", "delete_workspace"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.workspaces.manager.rehome", "_rehome_vm"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.workspaces.manager.rehome", "rehome_workspace"): (("_rehome_vm", "interaction"),),
    ("agentworks.workspaces.manager.repair", "repair_workspace"): (("gated_vm_boundary", "interaction"),),
    ("agentworks.workspaces.nodes", "PendingWorkspaceNode.teardown"): (("delete_workspace", "interaction"),),
    ("agentworks.workspaces.nodes", "pending_workspace_node"): (("PendingWorkspaceNode", "interaction"),),
}

_DIRECTED_CALLEE_ALIASES = {"_session_logs": "session_logs"}

# Post-0.14, consolidate this literal exact-caller pin behind a reusable graph contract.
_STORED_CALL_EDGE_MANIFEST: dict[tuple[str, str], tuple[str, ...]] = {
    ("agentworks.agents.manager.lifecycle", "create_agent"): ("Resolver.resolve",),
    ("agentworks.agents.manager.lifecycle", "reinit_agent"): ("Resolver.resolve",),
    ("agentworks.orchestration.activation", "gate_secret_resolver.resolve_gate_secret"): ("Resolver.resolve_gate",),
    ("agentworks.sessions.manager._create", "_preflight_and_resolve"): ("Resolver.resolve",),
    ("agentworks.sessions.manager._lifecycle", "resume_session"): ("Resolver.resolve",),
    ("agentworks.sessions.manager._scope", "_batch_vm_boundary"): ("Resolver.resolve",),
    ("agentworks.sessions.manager._scope", "_batch_vm_boundary._gate_resolver._resolve"): (
        "Resolver.resolve_late_repair",
    ),
    ("agentworks.vms.manager.boundary", "_live_vm_boundary"): ("Resolver.resolve",),
    ("agentworks.vms.manager.boundary", "gated_vm_boundary"): ("Resolver.resolve",),
    ("agentworks.vms.manager.exec", "add_git_credential"): ("Resolver.resolve",),
    ("agentworks.vms.manager.lifecycle", "_create_vm"): ("Resolver.resolve",),
    ("agentworks.vms.manager.lifecycle", "reinit_vm"): ("Resolver.resolve",),
    ("agentworks.vms.manager.power", "rekey_vm"): ("Resolver.resolve",),
    ("agentworks.workspaces.manager.create", "create_workspace"): ("Resolver.resolve",),
}

# Post-0.14 consolidation candidate: replace this exact-caller graph pin.
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

# Post-0.14 consolidation candidate: replace this exact-caller graph pin.
_TAILSCALE_STANDALONE_EDGE_MANIFEST = (("agentworks.vms.manager.power", "start_vm", "resolve_for_command"),)

_TAILSCALE_ENSURE_FORBIDDEN_TARGETS = (
    "Registry",
    "resolve_for_command",
    "resolve_template",
)

_RESOLVER_TYPE = "agentworks.secrets.resolver.Resolver"
_RESOLVER_CONTAINER_TYPE = f"{_RESOLVER_TYPE}[]"


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


def _object(module_name: str, dotted_name: str) -> object:
    value: object = importlib.import_module(module_name)
    for part in dotted_name.split("."):
        value = getattr(value, part)
    return value


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


def _manifest_entries(manifest: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    return [(module, name) for module, names in manifest.items() for name in names]


def _combined_entries(*manifests: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    return [entry for manifest in manifests for entry in _manifest_entries(manifest)]


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


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    _manifest_entries(_SERVICE_MANIFEST),
)
def test_service_manifest_requires_and_first_validates_exact_policy(
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


@pytest.mark.parametrize(("module_name", "function_name"), _manifest_entries(_INTERNAL_MANIFEST))
def test_internal_manifest_requires_and_first_validates_exact_policy(
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


@pytest.mark.parametrize(("module_name", "function_name"), _manifest_entries(_STORED_POLICY_MANIFEST))
def test_stored_policy_manifest_revalidates_as_first_statement(
    module_name: str,
    function_name: str,
) -> None:
    _assert_stored_validation_assignment(_first_statement(_object(module_name, function_name)))


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    _manifest_entries(_CLI_MANIFEST),
)
def test_ordinary_cli_manifest_derives_policy_once_as_first_statement(
    module_name: str,
    function_name: str,
) -> None:
    _assert_validation_assignment(_first_statement(_object(module_name, function_name)), ordinary=True)


@pytest.mark.parametrize(("module_name", "function_name"), _manifest_entries(_VERIFY_CLI_MANIFEST))
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
    _combined_entries(_SERVICE_MANIFEST, _INTERNAL_MANIFEST),
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
    _combined_entries(_SERVICE_MANIFEST, _INTERNAL_MANIFEST),
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


_STORED_INVOCATION_MANIFEST = (
    ("agentworks.secrets.resolver", "Resolver", "resolve", ()),
    ("agentworks.secrets.resolver", "Resolver", "resolve_gate", (object(),)),
    ("agentworks.secrets.resolver", "Resolver", "resolve_late_repair", (object(),)),
    ("agentworks.workspaces.nodes", "PendingWorkspaceNode", "teardown", ()),
    ("agentworks.agents.nodes", "PendingAgentNode", "teardown", ()),
)


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


@pytest.mark.parametrize(("module_name", "function_name"), _manifest_entries(_CLI_MANIFEST))
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


def test_exact_twelve_preflight_callers_forward_the_validated_local() -> None:
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
    assert discovered == _PREFLIGHT_CALLER_MANIFEST


def test_manifest_forwarding_edges_use_only_the_validated_local() -> None:
    """Every policy-bearing edge exactly matches the literal directed graph."""
    from tests.secrets.phase7_lexical_support import _interaction_call_edges

    assert _interaction_call_edges() == _DIRECTED_EDGE_MANIFEST
