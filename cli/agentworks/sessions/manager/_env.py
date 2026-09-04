"""Session template resolution and env/secret-target composition."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from agentworks.db import SessionMode
from agentworks.errors import (
    AgentworksError,
    NotFoundError,
    ValidationError,
)

if TYPE_CHECKING:
    import re
    from collections.abc import Mapping

    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, VMRow, WorkspaceRow
    from agentworks.env import EnvEntry
    from agentworks.resources.registry import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.sessions.template import SessionTemplate
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.workspaces.templates import ResolvedTemplate as ResolvedWorkspaceTemplate
from ._constants import _KNOWN_TEMPLATE_VARS, _TEMPLATE_VAR_RE


def _resolve_template(
    registry: Registry,
    template_name: str | None,
    *,
    db: Database | None = None,
    instance_name: str | None = None,
    overlay: SessionTemplate | None = None,
) -> ResolvedSessionTemplate:
    """Resolve a session template by name, applying inheritance."""
    from agentworks.sessions.templates import resolve_live_template, resolve_template

    try:
        if db is not None and instance_name is not None:
            return resolve_live_template(db, registry, instance_name, template_name)
        return resolve_template(
            registry,
            template_name,
            overlay=overlay,
            instance_name=instance_name,
        )
    except ValueError as e:
        raise ValidationError(
            str(e),
            entity_kind="session-template",
            entity_name=template_name,
        ) from None


def _display_registry(config: Config) -> Registry | None:
    """Build the registry for a read-only display column, degrading to
    ``None`` when config validation fails.

    ``load_request_registry`` builds and validates the registry and renders
    manifest/deprecation warnings at most once in the current request. It can
    raise ``AgentworksError`` for reasons
    unrelated to session templates (a misconfigured secret source
    chain, a bad ``defaults.site``, an unrelated resource collision).
    ``session list`` / ``session describe`` are read-only and never
    built the registry before the HARNESS INTEGRATION column existed, so a bad
    registry must degrade the HARNESS INTEGRATION cell to ``"-"`` for every row
    rather than abort the whole command. Catching ``AgentworksError``
    keeps the same breadth as the per-template guard below.
    """
    from agentworks.bootstrap import load_request_registry

    try:
        return load_request_registry(config, include_live_resources=False)
    except AgentworksError:
        return None


def _display_harness_integration(registry: Registry | None, template_name: str) -> str:
    """Resolve a session template to its concrete harness integration name for display.

    ``load_request_registry`` and ``resolve_template`` are config-only (no SSH),
    so this is cheap enough to show in listings. Returns ``"-"`` when the
    registry is unavailable (see :func:`_display_registry`) or the
    template fails to resolve (unknown name, bad harness_integration), so one bad
    template never aborts a whole listing or a describe. The resolved
    ``harness_integration`` is always a concrete string (defaulting to ``shell``).
    """
    if registry is None:
        return "-"
    from agentworks.sessions.templates import resolve_template

    try:
        return resolve_template(registry, template_name).harness_integration
    except AgentworksError:
        return "-"


def _display_live_harness_integration(
    db: Database,
    registry: Registry | None,
    instance_name: str,
    template_name: str,
) -> str:
    """Resolve a live session's integration for read-only display, or degrade."""
    if registry is None:
        return "-"
    from agentworks.sessions.templates import resolve_live_template

    try:
        return resolve_live_template(db, registry, instance_name, template_name).harness_integration
    except AgentworksError:
        return "-"


def _substitute_template_vars(text: str, variables: dict[str, str]) -> str:
    """Replace {{var}} placeholders in a string with their values."""

    def replace(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in _KNOWN_TEMPLATE_VARS:
            raise ValidationError(f"unknown template variable '{{{{{name}}}}}'")
        return variables[name]

    return _TEMPLATE_VAR_RE.sub(replace, text)


def _substitute_template_vars_in_env(
    env: dict[str, EnvEntry],
    variables: dict[str, str],
) -> dict[str, EnvEntry]:
    """Apply ``{{session_name}}`` / ``{{workspace_name}}`` substitution to
    plaintext env entry values.

    Preserves the legacy template-variable hook the session-command build
    carried before the EnvEntry migration (the pane command itself now
    substitutes at the harness integration op call site). Secret-ref entries pass
    through unchanged (variable substitution applies to the resolved
    string at resolution time, not the secret name).
    """
    from agentworks.env import EnvEntry as _EnvEntry
    from agentworks.env import PlaintextEnvEntry as _PlaintextEnvEntry

    result: dict[str, _EnvEntry] = {}
    for key, entry in env.items():
        if entry.value is None:
            result[key] = entry
            continue
        new_val = _substitute_template_vars(entry.value, variables)
        if new_val == entry.value:
            result[key] = entry
        else:
            result[key] = _EnvEntry(_PlaintextEnvEntry(value=new_val))
    return result


class _SessionEnvScopes(NamedTuple):
    """Per-scope env dicts for session create, start, and restart.

    Named-tuple shape (rather than a 5-tuple) keeps callers readable and
    leaves room for a new scope without breaking unpacking sites.
    """

    vm: dict[str, EnvEntry]
    workspace: dict[str, EnvEntry]
    admin: dict[str, EnvEntry] | None
    agent: dict[str, EnvEntry] | None
    session: dict[str, EnvEntry]


def _resolve_session_env_scopes(
    registry: Registry,
    *,
    db: Database,
    vm: VMRow,
    ws: WorkspaceRow,
    session_name: str,
    session_template: ResolvedSessionTemplate,
    mode: SessionMode,
    agent_name: str | None,
) -> _SessionEnvScopes:
    """Resolve the per-scope env dicts (vm, workspace, admin, agent, session)
    for session create, start, and restart.

    Returns the dicts ``effective_env`` would consume. Shared by
    ``_resolve_session_env`` (which composes them through
    ``compose_env`` into the rendered shell env) and the eager-prompting
    orchestration helper ``_session_secret_target`` (which wraps them as
    a ``SecretTarget`` for resolve_for_command, before any state
    mutation). Sharing this helper avoids duplicate template resolution
    and guarantees the two consumers see identical scope state.
    """
    from agentworks.agents.templates import resolve_live_template as _resolve_agent_template
    from agentworks.vms.admin_templates import resolve_live_template as _resolve_admin_template
    from agentworks.vms.templates import resolve_live_template as _resolve_vm_template
    from agentworks.workspaces.templates import resolve_live_template as _resolve_ws_template

    vm_template = _resolve_vm_template(db, registry, vm.name, vm.template)
    workspace_template = _resolve_ws_template(db, registry, ws.name, ws.template)

    admin_env: dict[str, EnvEntry] | None
    agent_env: dict[str, EnvEntry] | None
    if mode == SessionMode.ADMIN:
        admin_env = _resolve_admin_template(db, registry, vm.name, vm.admin_template).env
        agent_env = None
    else:
        assert agent_name is not None  # caller enforces; agent mode requires an agent
        admin_env = None
        agent_row = db.get_agent(agent_name)
        if agent_row is None:
            raise NotFoundError(
                f"agent '{agent_name}' not found",
                entity_kind="agent",
                entity_name=agent_name,
            )
        resolved_agent_template = _resolve_agent_template(db, registry, agent_row.name, agent_row.template)
        agent_env = resolved_agent_template.env

    session_env = _substitute_template_vars_in_env(
        session_template.env,
        variables={"session_name": session_name, "workspace_name": ws.name},
    )

    return _SessionEnvScopes(
        vm=vm_template.env,
        workspace=workspace_template.env,
        admin=admin_env,
        agent=agent_env,
        session=session_env,
    )


def _session_secret_target_pre_create(
    db: Database,
    registry: Registry,
    *,
    name: str,
    workspace_name: str,
    vm: VMRow,
    session_template: ResolvedSessionTemplate,
    new_workspace: bool,
    resolved_workspace_template: ResolvedWorkspaceTemplate | None,
    existing_workspace: WorkspaceRow | None,
    new_agent: bool,
    resolved_agent_template: ResolvedAgentTemplate | None,
    existing_agent: AgentRow | None,
    is_admin_mode: bool,
) -> SecretTarget:
    """Build a SecretTarget for ``create_session`` *before* any state mutation.

    Unlike :func:`_session_secret_target`, which takes the post-create
    workspace and agent rows, this resolves the env chain from a mix of
    template-name inputs (for ephemeral resources) and existing rows. Used
    once at the top of ``create_session`` so the eager-resolve runs before
    any of the optional ephemeral creates.
    """
    from agentworks.agents.templates import resolve_live_template as _resolve_live_agent_tmpl
    from agentworks.secrets import SecretTarget
    from agentworks.vms.admin_templates import resolve_live_template as _resolve_admin_template
    from agentworks.vms.templates import resolve_live_template as _resolve_vm_tmpl
    from agentworks.workspaces.templates import resolve_live_template as _resolve_live_ws_tmpl

    vm_template = _resolve_vm_tmpl(db, registry, vm.name, vm.template)

    if new_workspace:
        assert resolved_workspace_template is not None
        workspace_env = resolved_workspace_template.env
    else:
        assert existing_workspace is not None
        workspace_env = _resolve_live_ws_tmpl(
            db,
            registry,
            existing_workspace.name,
            existing_workspace.template,
        ).env

    agent_env: dict[str, EnvEntry] | None = None
    admin_scope: dict[str, EnvEntry] | None = None
    if is_admin_mode:
        admin_scope = _resolve_admin_template(db, registry, vm.name, vm.admin_template).env
    elif new_agent:
        assert resolved_agent_template is not None
        agent_env = resolved_agent_template.env
    elif existing_agent is not None:
        agent_env = _resolve_live_agent_tmpl(db, registry, existing_agent.name, existing_agent.template).env

    session_env = _substitute_template_vars_in_env(
        session_template.env,
        variables={"session_name": name, "workspace_name": workspace_name},
    )
    return SecretTarget(
        vm=vm_template.env,
        workspace=workspace_env,
        admin=admin_scope,
        agent=agent_env,
        session=session_env,
        label=f"session={name}",
    )


def _session_secret_target(
    registry: Registry,
    *,
    db: Database,
    vm: VMRow,
    ws: WorkspaceRow,
    session_name: str,
    session_template: ResolvedSessionTemplate,
    mode: SessionMode,
    agent_name: str | None,
) -> SecretTarget:
    """Build a SecretTarget for a session, for eager-prompting orchestration.

    Constructed from the same template chain that ``_resolve_session_env``
    would consume; substitution invariance guarantees the
    SecretDecl union is identical pre- vs post-substitution.
    """
    from agentworks.secrets import SecretTarget

    scopes = _resolve_session_env_scopes(
        registry,
        db=db,
        vm=vm,
        ws=ws,
        session_name=session_name,
        session_template=session_template,
        mode=mode,
        agent_name=agent_name,
    )
    return SecretTarget(
        vm=scopes.vm,
        workspace=scopes.workspace,
        admin=scopes.admin,
        agent=scopes.agent,
        session=scopes.session,
        label=f"session={session_name}",
    )


def _resolve_session_env(
    registry: Registry,
    *,
    values: Mapping[str, str],
    db: Database,
    vm: VMRow,
    ws: WorkspaceRow,
    session_name: str,
    session_template: ResolvedSessionTemplate,
    mode: SessionMode,
    agent_name: str | None,
    linux_user: str,
) -> dict[str, str]:
    """Compose the shell-open env for session create, start, and restart.

    Resolves the per-VM / per-workspace / per-agent templates, builds the
    ResourceContext, applies template-variable substitution to the session
    template's env values, and runs the merged dict through
    ``compose_env`` (which renders secrets from the command's
    pre-resolved ``values`` and overlays per-context identity vars).
    """
    from agentworks.env import ResourceContext, compose_env

    scopes = _resolve_session_env_scopes(
        registry,
        db=db,
        vm=vm,
        ws=ws,
        session_name=session_name,
        session_template=session_template,
        mode=mode,
        agent_name=agent_name,
    )

    from agentworks.vms.sites import site_platform_name

    ctx = ResourceContext(
        vm_name=vm.name,
        platform=site_platform_name(vm.site, registry),
        site=vm.site,
        user=linux_user,
        workspace_name=ws.name,
        workspace_dir=ws.workspace_path,
        agent_name=agent_name,
        session_name=session_name,
        session_kind="admin" if mode == SessionMode.ADMIN else "agent",
    )

    return compose_env(
        values=values,
        ctx=ctx,
        vm=scopes.vm,
        workspace=scopes.workspace,
        admin=scopes.admin,
        agent=scopes.agent,
        session=scopes.session,
    )
