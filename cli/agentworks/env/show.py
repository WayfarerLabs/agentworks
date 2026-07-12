"""Service-layer implementation for ``agw env show``.

Resolves a context from the operator's flags, computes the effective env
with per-key provenance (which scope won, what value), and renders the
result. Secret-backed entries are redacted by default; ``reveal_secrets``
runs them through the configured backend chain.

Lives next to the env model so the inspection logic stays close to the
producers (``effective_env``, ``per_context_identity_env``,
``compose_env``) it inspects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks import output
from agentworks.env.identity import ResourceContext, per_context_identity_env
from agentworks.env.merge import effective_env
from agentworks.errors import ValidationError

if TYPE_CHECKING:
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.config import Config  # noqa: F401 - used in signatures
    from agentworks.db import AgentRow, Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.env.entry import EnvEntry
    from agentworks.resources.registry import Registry
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.vms.templates import ResolvedVMTemplate
    from agentworks.workspaces.templates import ResolvedTemplate as ResolvedWorkspaceTemplate


# Source scopes appear in this fixed order in the rendered output (most
# general to most specific), matching the FRD R2 precedence ladder. The
# "identity" scope sits last because per-context identity vars win on
# collision with user-defined env (FRD R1).
Scope = Literal["vm", "workspace", "admin", "agent", "session", "identity"]

_SCOPE_ORDER: tuple[Scope, ...] = (
    "vm",
    "workspace",
    "admin",
    "agent",
    "session",
    "identity",
)


@dataclass(frozen=True)
class ResolvedEnvRow:
    """One row of the rendered output for ``agw env show``."""

    key: str
    rendered_value: str
    scope: Scope
    is_secret: bool


@dataclass(frozen=True)
class _ResolvedContext:
    """The resource chain the operator's flags pinned, with auto-resolution
    applied (e.g. ``--session`` infers workspace / agent / vm)."""

    vm: VMRow
    workspace: WorkspaceRow | None
    agent: AgentRow | None
    session: SessionRow | None


def show_env(
    db: Database,
    config: Config,
    *,
    vm_name: str | None = None,
    workspace_name: str | None = None,
    agent_name: str | None = None,
    session_name: str | None = None,
    reveal_secrets: bool = False,
) -> list[ResolvedEnvRow]:
    """Resolve the context, compute provenance-aware env, render rows.

    Returns the rendered rows AND emits them via ``agentworks.output`` for
    the CLI to display. Returning the structured rows lets tests pin the
    contract without having to parse the formatted output.

    Raises ``ValidationError`` when no context flag is provided.
    """
    ctx = _resolve_context(
        db,
        vm_name=vm_name,
        workspace_name=workspace_name,
        agent_name=agent_name,
        session_name=session_name,
    )

    # Per-scope env dicts (each scope contributes only its own EnvEntry map;
    # template inheritance is already merged into the resolved templates).
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    vm_env, workspace_env, admin_env, agent_env, session_env = _resolve_scope_envs(
        registry, ctx
    )

    # Build the resource context for identity vars.
    resource_ctx = _build_resource_context(ctx, registry)
    identity_env = per_context_identity_env(resource_ctx)

    # Compute the merged user env (without identity overlay yet) so we can
    # report per-key provenance from the user-defined layers; identity is
    # applied last with its own scope label.
    user_env_merged = effective_env(
        vm=vm_env,
        workspace=workspace_env,
        admin=admin_env,
        agent=agent_env,
        session=session_env,
    )
    user_provenance = _per_key_provenance(
        vm=vm_env,
        workspace=workspace_env,
        admin=admin_env,
        agent=agent_env,
        session=session_env,
    )

    # Reveal mode resolves each referenced secret exactly once (deduped
    # by name; several keys may reference one secret), per-secret so one
    # failure renders inline as <error: ...> without aborting the table.
    values, errors = _reveal_values(
        config, registry, user_env_merged, reveal=reveal_secrets
    )

    rows: list[ResolvedEnvRow] = []
    for key in sorted(user_env_merged.keys() | identity_env.keys()):
        if key in identity_env:
            # Identity wins on collision; rendered as the identity row.
            rows.append(
                ResolvedEnvRow(
                    key=key,
                    rendered_value=identity_env[key],
                    scope="identity",
                    is_secret=False,
                )
            )
            continue
        entry = user_env_merged[key]
        scope = user_provenance[key]
        rendered, is_secret = _render_value(entry, values, errors, reveal_secrets)
        rows.append(
            ResolvedEnvRow(
                key=key,
                rendered_value=rendered,
                scope=scope,
                is_secret=is_secret,
            )
        )

    _print_table(rows, ctx=ctx, reveal_secrets=reveal_secrets)
    return rows


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


def _resolve_context(
    db: Database,
    *,
    vm_name: str | None,
    workspace_name: str | None,
    agent_name: str | None,
    session_name: str | None,
) -> _ResolvedContext:
    """Resolve the four context flags into concrete DB rows, applying
    auto-resolution from the most-specific flag.

    Operator-supplied flags take precedence over auto-resolved values when
    both are present and conflict.
    """
    if not any((vm_name, workspace_name, agent_name, session_name)):
        raise ValidationError(
            "agw env show requires a context",
            hint=(
                "Pass at least one of --vm / --workspace / --agent / "
                "--session so env can be resolved relative to a resource."
            ),
        )

    session = None
    if session_name is not None:
        session = db.get_session(session_name)
        if session is None:
            raise ValidationError(
                f"session {session_name!r} not found",
                entity_kind="session",
                entity_name=session_name,
            )

    workspace_n = workspace_name or (session.workspace_name if session else None)
    workspace = None
    if workspace_n is not None:
        workspace = db.get_workspace(workspace_n)
        if workspace is None:
            raise ValidationError(
                f"workspace {workspace_n!r} not found",
                entity_kind="workspace",
                entity_name=workspace_n,
            )

    agent_n = agent_name or (session.agent_name if session else None)
    agent = None
    if agent_n is not None:
        agent = db.get_agent(agent_n)
        if agent is None:
            raise ValidationError(
                f"agent {agent_n!r} not found",
                entity_kind="agent",
                entity_name=agent_n,
            )

    # VM is the union of: explicit flag, agent's vm, session's workspace's vm,
    # workspace's vm. The most-specific source that's available wins.
    vm_candidates = [
        vm_name,
        agent.vm_name if agent else None,
        workspace.vm_name if workspace else None,
    ]
    vm_n = next((v for v in vm_candidates if v), None)
    if vm_n is None:
        raise ValidationError(
            "could not resolve a VM from the supplied flags",
            hint="Pass --vm explicitly.",
        )
    vm = db.get_vm(vm_n)
    if vm is None:
        raise ValidationError(
            f"VM {vm_n!r} not found",
            entity_kind="VM",
            entity_name=vm_n,
        )

    return _ResolvedContext(vm=vm, workspace=workspace, agent=agent, session=session)


# ---------------------------------------------------------------------------
# Per-scope env resolution
# ---------------------------------------------------------------------------


def _resolve_scope_envs(
    registry: Registry,
    ctx: _ResolvedContext,
) -> tuple[
    dict[str, EnvEntry],
    dict[str, EnvEntry] | None,
    dict[str, EnvEntry] | None,
    dict[str, EnvEntry] | None,
    dict[str, EnvEntry] | None,
]:
    """Resolve each scope's contribution as a (possibly-empty) env dict.

    Returns (vm, workspace, admin, agent, session). ``admin`` / ``agent``
    are mutually exclusive: a context with an agent uses the agent scope,
    otherwise the admin scope.
    """
    from agentworks.agents.templates import resolve_template as _resolve_agent_template
    from agentworks.resources.access import admin_template as _admin_template
    from agentworks.sessions.templates import resolve_template as _resolve_session_template
    from agentworks.vms.templates import resolve_template as _resolve_vm_template
    from agentworks.workspaces.templates import resolve_template as _resolve_ws_template

    vm_template: ResolvedVMTemplate = _resolve_vm_template(
        registry, ctx.vm.template,
    )
    vm_env = vm_template.env

    workspace_env: dict[str, EnvEntry] | None = None
    if ctx.workspace is not None:
        ws_template: ResolvedWorkspaceTemplate = _resolve_ws_template(
            registry, ctx.workspace.template,
        )
        workspace_env = ws_template.env

    admin_env: dict[str, EnvEntry] | None = None
    agent_env: dict[str, EnvEntry] | None = None
    if ctx.agent is not None:
        agent_template: ResolvedAgentTemplate = _resolve_agent_template(
            registry, ctx.agent.template,
        )
        agent_env = agent_template.env
    else:
        admin_env = _admin_template(registry).env

    session_env: dict[str, EnvEntry] | None = None
    if ctx.session is not None:
        session_template: ResolvedSessionTemplate = _resolve_session_template(
            registry, ctx.session.template,
        )
        session_env = session_template.env

    return vm_env, workspace_env, admin_env, agent_env, session_env


def _build_resource_context(ctx: _ResolvedContext, registry: Registry) -> ResourceContext:
    """Build the ResourceContext that ``per_context_identity_env`` consumes."""
    from agentworks.vms.sites import site_platform_name

    session_kind: Literal["admin", "agent"] | None = None
    if ctx.session is not None:
        from agentworks.db import SessionMode

        session_kind = (
            "admin" if ctx.session.mode == SessionMode.ADMIN.value else "agent"
        )

    user = ctx.agent.linux_user if ctx.agent is not None else ctx.vm.admin_username

    return ResourceContext(
        vm_name=ctx.vm.name,
        platform=site_platform_name(ctx.vm.site, registry),
        site=ctx.vm.site,
        user=user,
        workspace_name=ctx.workspace.name if ctx.workspace else None,
        workspace_dir=ctx.workspace.workspace_path if ctx.workspace else None,
        agent_name=ctx.agent.name if ctx.agent else None,
        session_name=ctx.session.name if ctx.session else None,
        session_kind=session_kind,
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _per_key_provenance(
    *,
    vm: dict[str, EnvEntry],
    workspace: dict[str, EnvEntry] | None,
    admin: dict[str, EnvEntry] | None,
    agent: dict[str, EnvEntry] | None,
    session: dict[str, EnvEntry] | None,
) -> dict[str, Scope]:
    """Return ``{key: winning_scope}`` for the user-defined env layers.

    Walks the same precedence ladder as ``effective_env`` so the winning
    scope matches the winning value.
    """
    provenance: dict[str, Scope] = {}
    layers: list[tuple[Scope, dict[str, EnvEntry] | None]] = [
        ("vm", vm),
        ("workspace", workspace),
        ("admin", admin),
        ("agent", agent),
        ("session", session),
    ]
    for scope, env_dict in layers:
        if env_dict is None:
            continue
        for key in env_dict:
            provenance[key] = scope
    return provenance


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _reveal_values(
    config: Config,
    registry: Registry,
    merged: dict[str, EnvEntry],
    *,
    reveal: bool,
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve every secret referenced by ``merged`` when revealing.

    Returns ``(values, errors)`` keyed by secret name: ONE batched
    resolve for every referenced secret (deduped by name; several env
    keys may reference one secret) so interactive prompts arrive up
    front in a single interaction, run in the loop's collect mode --
    per-secret failures land in ``errors`` and render inline instead of
    aborting the table, while successfully-resolved values (including
    already-answered prompts) are kept. That covers the resolve loop's
    transport-safety guard too: a backend value containing newline / CR
    / NUL bytes renders as ``<error: secret 'X': resolved value
    contains a control character...>``.
    """
    if not reveal:
        return {}, {}
    from agentworks.resources.access import secret_decls
    from agentworks.secrets import SecretDecl, active_backends, resolve_secrets

    decls = secret_decls(registry)
    backends = active_backends(config, registry)
    needed: list[SecretDecl] = []
    seen: set[str] = set()
    for entry in merged.values():
        name = entry.secret
        if name is None or name in seen:
            continue
        seen.add(name)
        needed.append(decls.get(name) or SecretDecl(name=name, description=""))
    if not needed:
        return {}, {}

    errors: dict[str, str] = {}
    values = resolve_secrets(needed, backends, errors=errors)
    return values, errors


def _render_value(
    entry: EnvEntry,
    values: dict[str, str],
    errors: dict[str, str],
    reveal_secrets: bool,
) -> tuple[str, bool]:
    """Return ``(rendered_value, is_secret)`` for one EnvEntry.

    Plaintext entries render as their value verbatim. Secret entries
    render as ``<from secret: NAME>`` by default; ``reveal_secrets``
    shows the pre-resolved value (or its inline error) from
    ``_reveal_values``.
    """
    if entry.secret is not None:
        if not reveal_secrets:
            return f"<from secret: {entry.secret}>", True
        if entry.secret in errors:
            return f"<error: {errors[entry.secret]}>", True
        return values[entry.secret], True
    assert entry.value is not None  # EnvEntry invariant
    return entry.value, False


def _print_table(
    rows: list[ResolvedEnvRow],
    *,
    ctx: _ResolvedContext,
    reveal_secrets: bool,
) -> None:
    """Render the rows as a header + table."""
    context_parts = [f"vm={ctx.vm.name}"]
    if ctx.workspace is not None:
        context_parts.append(f"workspace={ctx.workspace.name}")
    if ctx.agent is not None:
        context_parts.append(f"agent={ctx.agent.name}")
    if ctx.session is not None:
        context_parts.append(f"session={ctx.session.name}")
    output.info(f"Effective env for {' '.join(context_parts)}")

    if not rows:
        output.info("(no env entries; identity vars only appear when scope applies)")
        return

    if not reveal_secrets and any(r.is_secret for r in rows):
        output.detail(
            "Secret values redacted. Pass --reveal-secrets to resolve and print."
        )

    # Sort by (scope-order, key) so the table groups by precedence ladder
    # and keys within each scope are alphabetical.
    scope_idx = {s: i for i, s in enumerate(_SCOPE_ORDER)}
    sorted_rows = sorted(rows, key=lambda r: (scope_idx[r.scope], r.key))

    key_w = max((len(r.key) for r in sorted_rows), default=3)
    scope_w = max((len(r.scope) for r in sorted_rows), default=5)
    header = f"  {'KEY':<{key_w}}  {'SCOPE':<{scope_w}}  VALUE"
    output.info(header)
    output.info(f"  {'-' * key_w}  {'-' * scope_w}  {'-' * 5}")
    for r in sorted_rows:
        output.info(f"  {r.key:<{key_w}}  {r.scope:<{scope_w}}  {r.rendered_value}")
