"""Structural guards for the Phase 7 interaction and retired-seam closure."""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import Enum, StrEnum
from pathlib import Path
from typing import Protocol, cast

import pytest


class _NamedObject(Protocol):
    __name__: str


def _object_name(value: object) -> str:
    return cast(_NamedObject, value).__name__


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

_PREFLIGHT_CALLER_MANIFEST = {
    ("agentworks.sessions.manager._create", "_preflight_and_resolve"),
    ("agentworks.sessions.manager._lifecycle", "resume_session"),
    ("agentworks.sessions.manager._scope", "_batch_vm_boundary"),
    ("agentworks.workspaces.manager.create", "create_workspace"),
    ("agentworks.vms.manager.exec", "add_git_credential"),
    ("agentworks.vms.manager.power", "rekey_vm"),
    ("agentworks.vms.manager.boundary", "gated_vm_boundary"),
    ("agentworks.vms.manager.boundary", "_live_vm_boundary"),
    ("agentworks.vms.manager.lifecycle", "create_vm"),
    ("agentworks.vms.manager.lifecycle", "reinit_vm"),
    ("agentworks.agents.manager.lifecycle", "create_agent"),
    ("agentworks.agents.manager.lifecycle", "reinit_agent"),
}

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
    ("agentworks.vms.manager.lifecycle", "create_vm"): (
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
    ("agentworks.vms.manager.lifecycle", "create_vm"): ("Resolver.resolve",),
    ("agentworks.vms.manager.lifecycle", "reinit_vm"): ("Resolver.resolve",),
    ("agentworks.vms.manager.power", "rekey_vm"): ("Resolver.resolve",),
    ("agentworks.workspaces.manager.create", "create_workspace"): ("Resolver.resolve",),
}

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


def test_exact_twelve_preflight_callers_forward_the_validated_local() -> None:
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


def _interaction_call_edges() -> dict[tuple[str, str], tuple[tuple[str, str], ...]]:
    root = Path(__file__).parents[2] / "agentworks"
    discovered: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        module = ".".join(("agentworks", *path.relative_to(root).with_suffix("").parts))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            keywords = [keyword for keyword in call.keywords if keyword.arg == "interaction"]
            if not keywords:
                continue
            assert len(keywords) == 1
            function = _enclosing_function(parents, call)
            assert function is not None
            function_name = _qualified_function_name(parents, function)
            callee = ast.unparse(call.func)
            forwarded = keywords[0].value
            if (
                module == "agentworks.secrets.preview"
                and function_name == "preview_resolution"
                and callee == "_preview"
            ):
                assert isinstance(forwarded, ast.Constant) and forwarded.value is None
                continue
            assert isinstance(forwarded, ast.Name) and forwarded.id == "interaction", (
                module,
                function_name,
                callee,
                ast.unparse(forwarded),
            )
            discovered.setdefault((module, function_name), []).append((callee, "interaction"))
    return {owner: tuple(edges) for owner, edges in discovered.items()}


def test_manifest_forwarding_edges_use_only_the_validated_local() -> None:
    """Every policy-bearing edge exactly matches the literal directed graph."""
    assert _interaction_call_edges() == _DIRECTED_EDGE_MANIFEST


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


def _function_lexical_bindings(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
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
    return bindings - declarations


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
    for event_position, kind, name, target in sorted(events, key=lambda event: event[0]):
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


def _scope_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    nodes: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            visit(child)

    for statement in function.body:
        visit(statement)
    return nodes


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


def _semantic_type_indexes() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    root = Path(__file__).parents[2] / "agentworks"
    returns: dict[str, str] = {}
    fields: dict[tuple[str, str], str] = {}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        module, current_package = _module_identity(path, root)
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


def test_stored_policy_callback_callers_exactly_match_literal_graph() -> None:
    assert _stored_call_edges() == _STORED_CALL_EDGE_MANIFEST
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


def test_validated_interaction_local_is_never_rebound_in_any_manifest_caller() -> None:
    entries = set(
        _combined_entries(
            _SERVICE_MANIFEST,
            _INTERNAL_MANIFEST,
            _CLI_MANIFEST,
            _VERIFY_CLI_MANIFEST,
            _STORED_POLICY_MANIFEST,
        )
    )
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


def _actual_boundary_call_edges() -> dict[tuple[str, str], tuple[tuple[str, str], ...]]:
    root = Path(__file__).parents[2] / "agentworks"
    boundary_values: set[object] = set()
    for manifest in (_SERVICE_MANIFEST, _INTERNAL_MANIFEST):
        for module_name, names in manifest.items():
            for name in names:
                target_name = name.removesuffix(".__init__")
                boundary_values.add(_object(module_name, target_name))
    boundary_values.update(
        (
            _object("agentworks.secrets.resolve", "ResolutionPolicy"),
            _object("agentworks.secrets.preview", "_preview"),
        )
    )
    boundary_terminals = {
        name.rsplit(".", 1)[-1]
        for manifest in (_SERVICE_MANIFEST, _INTERNAL_MANIFEST)
        for names in manifest.values()
        for name in names
    } | {"Resolver", "PendingWorkspaceNode", "PendingAgentNode", "ResolutionPolicy", "_preview"}

    def is_boundary(target: str) -> bool:
        module_name, attribute = target.rsplit(".", 1)
        if attribute not in boundary_terminals:
            return False
        try:
            value = getattr(importlib.import_module(module_name), attribute)
        except (AttributeError, ModuleNotFoundError):
            return False
        return value in boundary_values

    discovered: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).with_suffix("")
        is_package = relative.parts[-1] == "__init__"
        module_parts = relative.parts[:-1] if is_package else relative.parts
        module = ".".join(("agentworks", *module_parts))
        current_package = module if is_package else module.rsplit(".", 1)[0]
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
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
        local_definitions = {
            statement.name
            for statement in tree.body
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            target = _semantic_call_name(
                call,
                aliases=aliases,
                module=module,
                local_definitions=local_definitions,
            )
            if target is None or not is_boundary(target):
                continue
            function = _enclosing_function(parents, call)
            assert function is not None
            owner = (module, _qualified_function_name(parents, function))
            callee = ast.unparse(call.func)
            if owner == ("agentworks.secrets.preview", "preview_resolution") and callee == "_preview":
                continue
            interaction = [keyword.value for keyword in call.keywords if keyword.arg == "interaction"]
            assert len(interaction) == 1, (owner, callee, target, call.lineno)
            assert isinstance(interaction[0], ast.Name) and interaction[0].id == "interaction", (
                owner,
                callee,
                ast.unparse(interaction[0]),
            )
            discovered.setdefault(owner, []).append((callee, "interaction"))
    return {owner: tuple(edges) for owner, edges in discovered.items()}


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
    root = Path(__file__).parents[2] / "agentworks"
    violations: list[tuple[str, str, int, object]] = []
    for path in root.rglob("*.py"):
        module, current_package = _module_identity(path, root)
        for expression, lineno, target in _protected_callable_alias_violations_from_tree(
            ast.parse(path.read_text()),
            module=module,
            current_package=current_package,
            targets=targets,
        ):
            violations.append((module, expression, lineno, target))
    return violations


def _production_semantic_target_calls(
    *targets: object,
) -> list[tuple[tuple[str, str], object, ast.Call]]:
    root = Path(__file__).parents[2] / "agentworks"
    found: list[tuple[tuple[str, str], object, ast.Call]] = []
    for path in root.rglob("*.py"):
        module, current_package = _module_identity(path, root)
        found.extend(
            _semantic_target_calls_from_tree(
                ast.parse(path.read_text()),
                module=module,
                current_package=current_package,
                targets=targets,
            )
        )
    return found


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


def _retired_semantic_violations(
    source: str,
    *,
    module: str,
    current_package: str,
) -> list[str]:
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    violations: list[str] = []
    forbidden_root_runtime = _FORBIDDEN_ROOT_IMPORTS | {"SECRET_BACKEND_REGISTRY"}

    def forbidden_module(target: str) -> bool:
        return any(target == old or target.startswith(f"{old}.") for old in _OLD_MODULES)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                aliases[bound] = alias.name if alias.asname else bound
                if forbidden_module(alias.name) or alias.name.rsplit(".", 1)[-1] in _RETIRED_SYMBOLS:
                    violations.append(f"{node.lineno}:import:{alias.name}")
                if alias.asname in _RETIRED_SYMBOLS:
                    violations.append(f"{node.lineno}:alias:{alias.asname}")
        elif isinstance(node, ast.ImportFrom):
            base = _resolved_from_module(current_package, node)
            if forbidden_module(base):
                violations.append(f"{node.lineno}:from:{base}")
            for alias in node.names:
                target = f"{base}.{alias.name}" if base else alias.name
                aliases[alias.asname or alias.name] = target
                if forbidden_module(target) or alias.name in _RETIRED_SYMBOLS or alias.asname in _RETIRED_SYMBOLS:
                    violations.append(f"{node.lineno}:imported:{target}")
                if base == "agentworks.secrets" and alias.name in forbidden_root_runtime:
                    violations.append(f"{node.lineno}:forbidden-root:{target}")
                if module == "agentworks.secrets" and alias.name in forbidden_root_runtime:
                    violations.append(f"{node.lineno}:root-reexport:{target}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _RETIRED_SYMBOLS:
            violations.append(f"{node.lineno}:name:{node.id}")
        elif (
            module == "agentworks.secrets"
            and isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in forbidden_root_runtime
        ):
            violations.append(f"{node.lineno}:root-binding:{node.id}")
        elif isinstance(node, ast.Attribute):
            parts = _dotted_attribute(node)
            if parts is None:
                continue
            target = ".".join((aliases.get(parts[0], parts[0]), *parts[1:]))
            if forbidden_module(target) or parts[-1] in _RETIRED_SYMBOLS:
                violations.append(f"{node.lineno}:attribute:{target}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in _RETIRED_SYMBOLS:
            violations.append(f"{node.lineno}:definition:{node.name}")
        elif (
            module == "agentworks.secrets"
            and isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            for element in node.value.elts:
                if (
                    isinstance(element, ast.Constant)
                    and isinstance(element.value, str)
                    and element.value in (_RETIRED_SYMBOLS | forbidden_root_runtime)
                ):
                    violations.append(f"{node.lineno}:root-export:{element.value}")
    return violations


def test_retired_resolution_seams_are_absent_from_production_ast() -> None:
    cli_root = Path(__file__).parents[2]
    found: list[str] = []
    for root in (cli_root / "agentworks", cli_root / "tests"):
        for path in root.rglob("*.py"):
            relative = path.relative_to(cli_root).with_suffix("")
            parts = relative.parts
            is_package = parts[-1] == "__init__"
            module_parts = parts[:-1] if is_package else parts
            module = ".".join(module_parts)
            current_package = module if is_package else module.rsplit(".", 1)[0]
            for violation in _retired_semantic_violations(
                path.read_text(),
                module=module,
                current_package=current_package,
            ):
                found.append(f"{path.relative_to(cli_root)}:{violation}")
    assert found == []


def test_retired_semantic_guard_negative_relocation_fixtures_cover_the_deny_lists() -> None:
    fixtures: list[tuple[str, str, str]] = []
    for symbol in _RETIRED_SYMBOLS:
        fixtures.extend(
            (
                ("agentworks.fixture", "agentworks", f"{symbol}\n"),
                ("agentworks.fixture", "agentworks", f"from fixture import {symbol} as relocated\n"),
                ("agentworks.fixture", "agentworks", f"class {symbol}:\n    pass\n"),
            )
        )
    for old_module in _OLD_MODULES:
        package, leaf = old_module.rsplit(".", 1)
        fixtures.extend(
            (
                ("agentworks.fixture", "agentworks", f"import {old_module} as relocated\n"),
                ("agentworks.fixture", "agentworks", f"from {old_module} import relocated\n"),
                ("agentworks.fixture", "agentworks", f"from {package} import {leaf} as relocated\n"),
                ("agentworks.secrets.fixture", "agentworks.secrets", f"from . import {leaf} as relocated\n"),
            )
        )
    for root_name in _FORBIDDEN_ROOT_IMPORTS | {"SECRET_BACKEND_REGISTRY"}:
        fixtures.extend(
            (
                ("agentworks.fixture", "agentworks", f"from agentworks.secrets import {root_name}\n"),
                ("agentworks.secrets", "agentworks.secrets", f"from .resolve import {root_name}\n"),
                ("agentworks.secrets", "agentworks.secrets", f"__all__ = [{root_name!r}]\n"),
                ("agentworks.secrets", "agentworks.secrets", f"{root_name} = object()\n"),
            )
        )
    fixtures.extend(
        (
            (
                "agentworks.fixture",
                "agentworks",
                "from agentworks import secrets as relocated\nvalue = relocated.backends\n",
            ),
            ("agentworks.secrets", "agentworks.secrets", "from . import backends as relocated\n"),
        )
    )
    for module, current_package, source in fixtures:
        assert _retired_semantic_violations(
            source,
            module=module,
            current_package=current_package,
        ), source


def test_secret_package_runtime_surface_is_exact() -> None:
    import agentworks.secrets as package

    assert tuple(package.__all__) == _PACKAGE_EXPORT_MANIFEST
    assert all(getattr(package, name) is not None for name in _PACKAGE_EXPORT_MANIFEST)
    forbidden = _RETIRED_SYMBOLS | _FORBIDDEN_ROOT_IMPORTS | {"SECRET_BACKEND_REGISTRY", "backends"}
    assert forbidden.isdisjoint(vars(package))


def test_permanent_runtime_vocabulary_and_rendered_secret_guide_are_source_first() -> None:
    from agentworks.secrets import guide_contributions

    root = Path(__file__).parents[3]
    permanent = [root / "README.md", root / "docs", root / "cli" / "README.md", root / "cli" / "agentworks"]
    retired_phrases = ("active backend", "backend chain", "resolve_secrets", "ActiveBackend")
    violations: list[str] = []
    for target in permanent:
        paths = (target,) if target.is_file() else (path for path in target.rglob("*") if path.suffix in {".md", ".py"})
        for path in paths:
            if "docs/sdd/" in path.as_posix():
                continue
            text = path.read_text()
            normalized = " ".join(text.split()).lower()
            if any(phrase.lower() in normalized for phrase in retired_phrases):
                violations.append(str(path.relative_to(root)))
    assert violations == []

    topic = guide_contributions()[0]
    rendered = "\n".join(
        [topic.summary, *(block.markdown for block in topic.blocks if hasattr(block, "markdown"))]
    ).lower()
    assert "source" in rendered
    assert "interaction policy" in rendered
    assert "preview" in rendered and "not proof" in rendered
    assert "consent" in rendered
    assert not any(phrase.lower() in rendered for phrase in retired_phrases)


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
    ensure_calls = _production_semantic_target_calls(ensure)
    discovered: list[tuple[str, str, str, str, str]] = []
    for owner, target, call in ensure_calls:
        assert target is ensure
        auth_keywords = [keyword.value for keyword in call.keywords if keyword.arg == "auth_keys"]
        assert len(auth_keywords) == 1
        discovered.append((*owner, "_ensure_tailscale", "auth_keys", ast.unparse(auth_keywords[0])))
    assert tuple(sorted(discovered)) == _TAILSCALE_SOURCE_EDGE_MANIFEST

    resolve_for_command = _object("agentworks.secrets.orchestration", "resolve_for_command")
    standalone_calls = [
        (owner[0], owner[1], "resolve_for_command")
        for owner, target, call in _production_semantic_target_calls(resolve_for_command)
        if target is resolve_for_command and any(keyword.arg == "extra_decls" for keyword in call.keywords)
    ]
    assert tuple(standalone_calls) == _TAILSCALE_STANDALONE_EDGE_MANIFEST

    registry = _object("agentworks.resources.registry", "Registry")
    resolve_template = _object("agentworks.vms.templates", "resolve_template")
    forbidden_targets = (registry, resolve_for_command, resolve_template)
    forbidden_names = {_object_name(target): target for target in forbidden_targets}
    assert tuple(forbidden_names) == _TAILSCALE_ENSURE_FORBIDDEN_TARGETS
    forbidden_ensure_edges = [
        (owner, _object_name(target))
        for owner, target, _call in _production_semantic_target_calls(*forbidden_targets)
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
