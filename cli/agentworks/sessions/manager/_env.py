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
    """Build the registry for an unfiltered read-only display, degrading to
    ``None`` when config validation fails.

    ``load_request_registry`` builds and validates the registry and renders
    manifest/deprecation warnings at most once in the current request. It can
    raise ``AgentworksError`` for reasons
    unrelated to session templates (a misconfigured secret source
    chain, a bad ``defaults.site``, an unrelated resource collision).
    Unfiltered ``session list`` is read-only and never built the registry before
    the HARNESS INTEGRATION column existed, so a bad registry must degrade the
    HARNESS INTEGRATION cell to ``"-"`` for every row rather than abort the whole
    command. Catching ``AgentworksError`` keeps the same breadth as the
    per-template guard below. An explicit harness-integration filter instead
    requires the registry for validation and fails closed when it cannot be
    loaded.
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

    scopes = _SessionEnvScopes(
        vm=vm_template.env,
        workspace=workspace_template.env,
        admin=admin_env,
        agent=agent_env,
        session=session_template.env,
    )
    from agentworks.secrets import SecretTarget

    SecretTarget(**scopes._asdict(), label=f"session={session_name}").validate_context(
        _session_context_values(db, vm, ws, session_name, mode, agent_name)
    )
    return scopes


def _session_context_values(
    db: Database,
    vm: VMRow,
    ws: WorkspaceRow,
    session_name: str,
    mode: SessionMode,
    agent_name: str | None,
) -> dict[str, str]:
    """Read the consuming identity for session interpolation without remote work."""
    from agentworks.interpolation import context_values

    user = vm.admin_username
    if mode == SessionMode.AGENT:
        assert agent_name is not None
        agent = db.get_agent(agent_name)
        if agent is None:
            raise NotFoundError(f"agent {agent_name!r} not found", entity_kind="agent", entity_name=agent_name)
        user = agent.linux_user
    return context_values(
        vm_name=vm.name,
        user=user,
        workspace_name=ws.name,
        workspace_dir=ws.workspace_path,
        agent_name=agent_name,
        session_name=session_name,
    )


def _session_secret_target_pre_create(
    db: Database,
    registry: Registry,
    *,
    name: str,
    context: Mapping[str, str],
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

    target = SecretTarget(
        vm=vm_template.env,
        workspace=workspace_env,
        admin=admin_scope,
        agent=agent_env,
        session=session_template.env,
        label=f"session={name}",
    )
    target.validate_context(context)
    return target


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
    would consume. Plaintext rendering does not change the secret-reference union.
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
    ResourceContext and runs the merged dict through
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

    resolved = compose_env(
        values=values,
        ctx=ctx,
        vm=scopes.vm,
        workspace=scopes.workspace,
        admin=scopes.admin,
        agent=scopes.agent,
        session=scopes.session,
    )
    if {"_AGW_SESSION_UUID", "_AGW_RUN_ID"}.intersection(resolved):
        raise ValidationError(
            "session environment conflicts with private tmux launch identity",
            entity_kind="session",
            entity_name=session_name,
            hint="Remove _AGW_SESSION_UUID and _AGW_RUN_ID from the session environment.",
        )
    return resolved
