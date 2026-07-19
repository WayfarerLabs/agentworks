"""Session lifecycle orchestration."""

from __future__ import annotations

import contextlib
import re
import shlex
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

import typer

from agentworks import output
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
from agentworks.errors import (
    AlreadyExistsError,
    BrokenStateError,
    ConnectivityError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.sessions.tmux import AGENT_SOCKET_ROOT
from agentworks.ssh import SSH_TRANSPORT_ERROR
from agentworks.transports import transport
from agentworks.vms.manager import gated_vm_boundary

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Template variable substitution: {{var}} double-brace syntax.
_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
_KNOWN_TEMPLATE_VARS = {"session_name", "workspace_name"}

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from agentworks.agents.nodes import (
        AgentTemplateNode,
        LiveAgentNode,
        PendingAgentNode,
    )
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.capabilities.base import OperationScope
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.env import EnvEntry
    from agentworks.resources.registry import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.sessions.tmux import RunCommand
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport
    from agentworks.vms.nodes import LiveVMNode
    from agentworks.workspaces.nodes import (
        LiveWorkspaceNode,
        PendingWorkspaceNode,
    )
    from agentworks.workspaces.templates import (
        ResolvedTemplate as ResolvedWorkspaceTemplate,
    )


# -- Helpers ---------------------------------------------------------------

# Grace period (seconds) to wait after sending C-c before killing a session
_STOP_GRACE_SECONDS = 5


def _resolve_session_linux_user(db: Database, session: SessionRow, vm: VMRow) -> str:
    """Resolve the Linux user for a session.

    Agent-mode sessions look up the agent by name. Admin-mode sessions use the VM admin.
    """
    if session.agent_name:
        agent = db.get_agent(session.agent_name)
        if agent is None:
            raise NotFoundError(
                f"agent '{session.agent_name}' not found "
                f"(referenced by session '{session.name}')",
                entity_kind="agent",
                entity_name=session.agent_name,
            )
        return agent.linux_user
    return vm.admin_username


def _kill_session(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None,
) -> bool:
    """Kill a session on its expected tmux server. Returns True if successful."""
    from agentworks.sessions.tmux import kill_session

    return kill_session(session_name, run_command=run_command, socket_path=socket_path)


def _build_session_target(
    session: SessionRow,
    *,
    vm: VMRow,
    config: Config,
    db: Database,
    admin_target: Transport,
) -> Transport:
    """Pick the SSH transport for destructive operations on a single session.

    Returns a ``Transport`` whose SSH user is the session's owning Linux user
    (admin for admin-mode, agent for agent-mode). For agent sessions, builds
    an agent ``Transport`` and probes it; raises StateError with a reinit hint
    if the agent's authorized_keys aren't provisioned.
    For admin sessions, returns the admin target unchanged.

    Single-session paths use this to make kill / restart operations
    consistent with create: every destructive step on an agent session
    goes via direct agent SSH. Because the returned target always owns
    the session it will operate on, callers can issue destructive commands
    without sudo. Batch paths intentionally don't use this helper; they
    keep admin's target across all sessions and pass ``sudo=True`` to
    reach into agent tmux servers (carve-out for batch ops).
    """
    if session.mode == SessionMode.ADMIN.value:
        return admin_target

    if session.agent_name is None:
        raise NotFoundError(
            f"session '{session.name}' is agent-mode but has no agent_name",
            entity_kind="session",
            entity_name=session.name,
        )
    agent = db.get_agent(session.agent_name)
    if agent is None:
        raise NotFoundError(
            f"agent '{session.agent_name}' (referenced by session '{session.name}') not found",
            entity_kind="agent",
            entity_name=session.agent_name,
        )
    from agentworks.agents.manager import _assert_agent_ssh_works
    from agentworks.transports import agent_transport

    agent_target = agent_transport(vm, config, agent)
    _assert_agent_ssh_works(agent_target, agent)
    return agent_target


def _repair_session_pid(
    session: SessionRow,
    *,
    target: Transport,
    db: Database,
) -> bool:
    """Core repair logic for a single session. Returns True if the DB was updated.

    Raises StateError if the session is alive but PID/boot_id can't be recovered,
    or ConnectivityError if the VM is unreachable.
    """
    from agentworks.sessions.tmux import get_tmux_server_pid, tmux_cmd

    sock = session.socket_path
    q_session = shlex.quote(session.name)

    # Step 1: try has-session (the primary liveness check)
    has_cmd = tmux_cmd(f"has-session -t {q_session}", sock) + " 2>/dev/null"
    has_result = target.run(has_cmd, check=False)
    if has_result.returncode == SSH_TRANSPORT_ERROR:
        raise ConnectivityError(
            f"cannot reach VM for session '{session.name}' (SSH connection failed)",
            entity_kind="session",
            entity_name=session.name,
        )
    if has_result.ok:
        # Session is alive -- recover PID + boot ID
        pid = get_tmux_server_pid(target=target, socket_path=sock)
        boot_id = _get_boot_id(target) if pid is not None else None
        if pid is not None and boot_id is not None:
            db.update_session_pid(session.name, pid, boot_id=boot_id)
            output.warn(f"Recovered PID {pid} for session '{session.name}'")
            return True
        raise StateError(
            f"session '{session.name}' is alive but PID/boot ID recovery failed.",
            entity_kind="session",
            entity_name=session.name,
            hint="Investigate the tmux server manually.",
        )

    # Step 2: has-session failed -- determine if genuinely stopped or ambiguous
    if sock and target.run(f"test -e {shlex.quote(sock)}", sudo=True, check=False).ok:
        # Socket exists. Probe with sudo to distinguish stale from unreachable.
        probe_cmd = tmux_cmd("list-sessions", sock, sudo=True) + " 2>/dev/null"
        if target.run(probe_cmd, check=False).ok:
            raise StateError(
                f"session '{session.name}' has a live tmux server but it is unreachable.",
                entity_kind="session",
                entity_name=session.name,
                hint="This may indicate a permissions issue. Investigate manually.",
            )
        # Stale socket, server is dead
        db.update_session_pid(session.name, PID_STOPPED)
        output.warn(f"Session '{session.name}' is not running, marked stopped")
        return True

    # No socket (or admin session) and has-session failed -- genuinely stopped
    db.update_session_pid(session.name, PID_STOPPED)
    output.warn(f"Session '{session.name}' is not running, marked stopped")
    return True


def _needs_repair(session: SessionRow) -> bool:
    """True if the session is missing PID or boot_id and needs auto-repair."""
    if session.pid == PID_STOPPED:
        return False
    return session.pid is None or session.boot_id is None


def _ensure_pid(session: SessionRow, *, target: Transport, db: Database) -> SessionRow:
    """Auto-recover PID + boot ID for a session missing either.

    Strict gate: after this returns, the session is guaranteed to be either
    PID_STOPPED or have valid PID + boot_id. Raises StateError if the
    session cannot be resolved.
    """
    if not _needs_repair(session):
        return session
    _repair_session_pid(session, target=target, db=db)  # raises on failure
    result = db.get_session(session.name)
    assert result is not None
    return result


def ensure_pids_batch(sessions: list[SessionRow], *, db: Database, config: Config) -> list[SessionRow]:
    """Auto-recover PID + boot ID for sessions missing either. Returns updated list."""
    need_repair = [s for s in sessions if _needs_repair(s)]
    if not need_repair:
        return sessions

    # Group by VM (not workspace) to reuse one Transport per VM
    by_vm: dict[str, list[SessionRow]] = {}
    vm_cache: dict[str, Transport] = {}
    for s in need_repair:
        ws = db.get_workspace(s.workspace_name)
        if not ws:
            continue
        if ws.vm_name not in vm_cache:
            vm = db.get_vm(ws.vm_name)
            if not vm or not vm.tailscale_host:
                continue
            try:
                vm_cache[ws.vm_name] = transport(vm, config)
            except Exception as exc:
                output.warn(f"Cannot reach VM '{ws.vm_name}': {exc}")
                continue
        by_vm.setdefault(ws.vm_name, []).append(s)

    repaired_names: set[str] = set()
    for vm_name, vm_sessions in by_vm.items():
        target = vm_cache[vm_name]
        for session in vm_sessions:
            try:
                if _repair_session_pid(session, target=target, db=db):
                    repaired_names.add(session.name)
            except (ConnectivityError, StateError) as exc:
                output.warn(str(exc))
            except Exception as exc:
                output.warn(f"Failed to repair session '{session.name}': {exc}")

    # Return original list with repaired sessions refreshed from DB
    if not repaired_names:
        return sessions
    result = []
    for s in sessions:
        if s.name in repaired_names:
            refreshed = db.get_session(s.name)
            result.append(refreshed if refreshed else s)
        else:
            result.append(s)
    return result


def _require_workspace(db: Database, name: str) -> WorkspaceRow:
    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )
    return ws


def _require_vm_for_workspace(db: Database, ws: WorkspaceRow) -> VMRow:
    vm = db.get_vm(ws.vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{ws.vm_name}' not found",
            entity_kind="vm",
            entity_name=ws.vm_name,
        )
    return vm


def _session_scope(
    db: Database, session: SessionRow, ws: WorkspaceRow, vm: VMRow
) -> OperationScope:
    """The singular session ops' SESSION-level operation scope: the
    operation is about the session (running as its agent, or as the
    admin), even though the composed graph is the live VM alone; pass
    the level of the entity the command is ABOUT, not of what it
    walks. The SESSION level's field rules (required vm + workspace +
    session; exactly one of agent/admin) are enforced by the scope's
    own constructor."""
    from agentworks.capabilities.base import OperationScope, ScopeLevel
    from agentworks.db import SYSTEM_SLUG_KEY

    return OperationScope(
        level=ScopeLevel.SESSION,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm.name,
        workspace=ws.name,
        session=session.name,
        agent=session.agent_name,
        admin=session.agent_name is None,
    )


@contextlib.contextmanager
def _prepare_vm(
    db: Database,
    config: Config,
    session: SessionRow,
    *,
    operation: str | None = None,
) -> Iterator[tuple[WorkspaceRow, VMRow, RunCommand, RunCommand, Transport]]:
    """The singular session ops' composition root (stop / delete /
    describe / attach / logs): validate the session's workspace and VM
    rows, then yield ``(ws, vm, run_command, run_as_root, target)``
    INSIDE ``gated_vm_boundary``'s held-active span, which replaces
    the imperative bind + point gate and the callers' own ``vm_active``
    holds. The scope is SESSION-level, built here from the session row
    once the vm/workspace rows it names are resolved: the ops are
    about the session, not the VM they walk. If ``operation`` is set,
    an SSHLogger attaches to the Transport so all calls log
    automatically.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHLogger

    ws = _require_workspace(db, session.workspace_name)
    vm = _require_vm_for_workspace(db, ws)

    # Cheap row validation stays pre-gate: a VM with no Tailscale
    # address can never serve a session op, so it must fail with zero
    # prompts and zero VM starts. (The imperative body checked this
    # after its gate; the gate cannot populate the address on the
    # already-loaded row, so the command's outcome is identical. The
    # hoist does forgo one accidental heal: the post-gate order could
    # start a stopped VM whose rejoin repopulated the row's address,
    # letting a RETRY succeed; now the retry keeps failing until an
    # explicit vm start or reinit.)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm.name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=vm.name,
        )

    registry = build_registry(config)
    with gated_vm_boundary(
        db, config, registry, vm, scope=_session_scope(db, session, ws, vm)
    ):
        logger = SSHLogger(vm.name, operation) if operation else None
        target = transport(vm, config, logger=logger)
        run_command: RunCommand = target.run
        run_as_root: RunCommand = partial(target.run, sudo=True)
        yield ws, vm, run_command, run_as_root, target


def _require_session(db: Database, name: str) -> SessionRow:
    session = db.get_session(name)
    if session is None:
        raise NotFoundError(
            f"session '{name}' not found",
            entity_kind="session",
            entity_name=name,
        )
    return session


def _regenerate_tmuxinator(
    db: Database,
    config: Config,
    vm: VMRow,
    ws: WorkspaceRow,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Regenerate the workspace tmuxinator config from current session state."""
    from agentworks.workspaces.tmuxinator import generate_config

    sessions = db.list_sessions(workspace_name=ws.name)
    # Build socket paths for tmuxinator (admin sessions have NULL, agent sessions always set)
    socket_paths = {s.name: s.socket_path for s in sessions}
    config_text = generate_config(ws.name, ws.workspace_path, sessions=sessions, socket_paths=socket_paths)
    target = transport(vm, config, logger=logger)
    target.write_file(f"{ws.workspace_path}/.tmuxinator.yml", config_text)


def filter_sessions(
    db: Database,
    *,
    workspace_name: str | list[str] | None = None,
    vm_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    admin_only: bool = False,
) -> list[SessionRow]:
    """Load sessions with optional workspace, VM, agent, and/or admin filters.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``admin_only``
    restricts to admin-mode sessions (no agent); it is mutually
    exclusive with ``agent_name`` at the caller level. See
    ``Database.list_sessions``.
    """
    return db.list_sessions(
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        admin_only=admin_only,
    )


def _distinct_vms_for_sessions(db: Database, sessions: list[SessionRow]) -> list[VMRow]:
    """Resolve the distinct set of VMs that host the given sessions.

    Used by the batch session operations (stop_all_sessions, restart_all_sessions,
    list_sessions) to feed `_batch_vm_boundary` with exactly the VMs whose SSH
    transports will be touched. Order is insertion order keyed by VM name so
    gate and keepalive entry messages render in a stable order.
    """
    distinct: list[VMRow] = []
    seen: set[str] = set()
    for s in sessions:
        ws = db.get_workspace(s.workspace_name)
        if ws is None or ws.vm_name in seen:
            continue
        vm = db.get_vm(ws.vm_name)
        if vm is None:
            continue
        distinct.append(vm)
        seen.add(ws.vm_name)
    return distinct


@contextlib.contextmanager
def _batch_vm_boundary(
    db: Database, config: Config, vms: Sequence[VMRow]
) -> Iterator[None]:
    """The batch session ops' composition root (stop_all_sessions,
    restart_all_sessions, list_sessions' status pass): ONE boundary
    over the distinct VMs, then each VM's activation gate and
    held-active span.

    Mirrors the imperative batch order exactly (preflight + one
    resolve covering the whole batch, THEN per-VM gate + hold),
    orchestrated: one multi-root walk over the live VM nodes, with one
    shared site node per distinct site via the factory's ``site_nodes``
    memo (one held platform instance per site, the old by-site dedup),
    the walk union registered once on ONE resolver, a SYSTEM-level
    scope (one level per COMMAND, never per node; each VM's identity
    comes from its own node), one preflight sweep, ONE boundary
    resolve, and only then the gates in VM order on an ExitStack. A
    failing gate (an operator-stopped VM) propagates and aborts the
    whole batch, exactly as the imperative per-VM gate loop did.

    The gate callback SERVES the boundary's cached values
    (``resolver.get``): the union already covers every gate secret, so
    two stopped VMs sharing a site cost exactly ONE backend pass
    (a per-gate just-in-time resolve would re-resolve the shared
    site's secret, and ``Resolver.seed`` after the boundary raises by
    design). The one exception is the repair path's Tailscale rejoin
    key: it is inherently outside the boundary union (lazy, read only
    when a started VM fails to reconnect) and resolves late through
    the backend chain, the same documented conditional-need exception
    the imperative repair path carried; it cannot seed, because the
    boundary has already resolved. The callback is built PER TARGET
    and refuses any other outside-the-union name unless the target
    declares it in ``repair_secret_refs`` (see the invariant comment
    inline), so no future gate target can silently late-resolve per
    VM.

    An empty VM set stays a complete no-op (no registry, no resolver,
    no gate), the imperative lazy-bind property: ``session list
    --no-status`` and empty filter results must cost nothing here.
    """
    if not vms:
        yield
        return

    from agentworks.bootstrap import build_registry
    from agentworks.capabilities.base import (
        OperationScope,
        RunContext,
        ScopeLevel,
    )
    from agentworks.db import SYSTEM_SLUG_KEY
    from agentworks.orchestration.activation import activation_gate
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import VMSiteNode, live_vm_node

    registry = build_registry(config)
    resolver = Resolver(config, registry)
    site_nodes: dict[str, VMSiteNode] = {}
    vm_nodes = [
        live_vm_node(db, config, registry, vm, site_nodes=site_nodes)
        for vm in vms
    ]
    nodes = walk(*vm_nodes)
    union = secret_union(nodes)
    for secret_name in union:
        resolver.register_name(secret_name)
    scope = OperationScope(
        level=ScopeLevel.SYSTEM,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
    )
    preflight_all(nodes, RunContext(config=config, operation_scope=scope))
    resolver.resolve()

    covered = set(union)

    def _gate_resolver(vm_node: LiveVMNode) -> Callable[[str], str]:
        """Per-target gate callback: serve the boundary's cache; guard
        the late-resolve branch against the target's own declaration."""

        def _resolve(secret_name: str) -> str:
            if secret_name in covered:
                return resolver.get(secret_name)
            # INVARIANT: the ONLY sanctioned post-boundary resolution
            # is the repair path's rejoin key. Gate secrets ride the
            # boundary union structurally (gate_secret_refs delegates
            # to the site's declared secret_refs, which the walk
            # unions), so a name outside the covered union must be one
            # of THIS target's declared repair secrets; anything else
            # refuses loudly (the declare/receive contract) rather
            # than silently late-resolving per VM.
            if secret_name not in vm_node.repair_secret_refs():
                raise StateError(
                    f"secret '{secret_name}' is outside the batch "
                    f"boundary union and was not declared in this "
                    f"activation target's repair_secret_refs, so the "
                    f"batch gate will not resolve it late (the "
                    f"declare/receive contract): gate secrets must "
                    f"ride the boundary union; only the lazy rejoin "
                    f"repair key resolves after it."
                )
            # The rejoin repair key: resolve late through the backend
            # chain, never seed (the boundary already resolved).
            from agentworks.orchestration.secrets import secret_declarations
            from agentworks.secrets.resolve import (
                active_backends,
                resolve_secrets,
            )

            (decl,) = secret_declarations([secret_name], registry)
            return resolve_secrets([decl], active_backends(config, registry))[
                secret_name
            ]

        return _resolve

    with contextlib.ExitStack() as stack:
        for vm_node in vm_nodes:
            stack.enter_context(
                activation_gate(vm_node, _gate_resolver(vm_node))
            )
        yield


def _resolve_template(registry: Registry, template_name: str | None) -> ResolvedSessionTemplate:
    """Resolve a session template by name, applying inheritance."""
    from agentworks.sessions.templates import resolve_template

    try:
        return resolve_template(registry, template_name)
    except ValueError as e:
        raise ValidationError(
            str(e),
            entity_kind="session-template",
            entity_name=template_name,
        ) from None


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

    Preserves the legacy template-variable hook that ``_build_session_command``
    carried before the EnvEntry migration. Secret-ref entries pass through
    unchanged (variable substitution applies to the resolved string at
    backend time, not the secret name).
    """
    from agentworks.env import EnvEntry as _EnvEntry

    result: dict[str, _EnvEntry] = {}
    for key, entry in env.items():
        if entry.value is None:
            result[key] = entry
            continue
        new_val = _substitute_template_vars(entry.value, variables)
        if new_val == entry.value:
            result[key] = entry
        else:
            result[key] = _EnvEntry(key=key, value=new_val)
    return result


class _SessionEnvScopes(NamedTuple):
    """Per-scope env dicts for a session create / restart.

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
    for a session create / restart.

    Returns the dicts ``effective_env`` would consume. Shared by
    ``_resolve_session_env`` (which composes them through
    ``compose_env`` into the rendered shell env) and the eager-prompting
    orchestration helper ``_session_secret_target`` (which wraps them as
    a ``SecretTarget`` for resolve_for_command, before any state
    mutation). Sharing this helper avoids duplicate template resolution
    and guarantees the two consumers see identical scope state.
    """
    from agentworks.agents.templates import resolve_template as _resolve_agent_template
    from agentworks.resources.access import admin_template as _admin_template
    from agentworks.vms.templates import resolve_template as _resolve_vm_template
    from agentworks.workspaces.templates import resolve_template as _resolve_ws_template

    vm_template = _resolve_vm_template(registry, vm.template)
    workspace_template = _resolve_ws_template(registry, ws.template)

    admin_env: dict[str, EnvEntry] | None
    agent_env: dict[str, EnvEntry] | None
    if mode == SessionMode.ADMIN:
        admin_env = _admin_template(registry).env
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
        resolved_agent_template = _resolve_agent_template(
            registry, agent_row.template
        )
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
    registry: Registry,
    *,
    name: str,
    workspace_name: str,
    vm: VMRow,
    session_template: ResolvedSessionTemplate,
    new_workspace: bool,
    workspace_template: str | None,
    existing_workspace: WorkspaceRow | None,
    new_agent: bool,
    agent_template: str | None,
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
    from agentworks.agents.templates import resolve_template as _resolve_agent_tmpl
    from agentworks.resources.access import admin_template as _admin_template
    from agentworks.secrets import SecretTarget
    from agentworks.vms.templates import resolve_template as _resolve_vm_tmpl
    from agentworks.workspaces.templates import resolve_template as _resolve_ws_tmpl

    vm_template = _resolve_vm_tmpl(registry, vm.template)

    if new_workspace:
        workspace_env = _resolve_ws_tmpl(registry, workspace_template).env
    else:
        assert existing_workspace is not None
        workspace_env = _resolve_ws_tmpl(registry, existing_workspace.template).env

    agent_env: dict[str, EnvEntry] | None = None
    admin_scope: dict[str, EnvEntry] | None = None
    if is_admin_mode:
        admin_scope = _admin_template(registry).env
    elif new_agent:
        agent_env = _resolve_agent_tmpl(registry, agent_template).env
    elif existing_agent is not None:
        agent_env = _resolve_agent_tmpl(registry, existing_agent.template).env

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
    """Compose the shell-open env for a session create / restart.

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


def _build_session_command(
    template: ResolvedSessionTemplate,
    *,
    session_name: str,
    workspace_name: str,
    restart: bool = False,
) -> str:
    """Build the command for a session from its template.

    Returns an empty string when the template has no command (login shell
    only). Uses restart_command (if defined) when restart=True. The
    ``exec`` wrapping that lets the command replace the login shell is
    applied downstream in ``sessions/tmux._pane_command``; this function
    just returns the operator's command string (after template-var
    substitution). Env injection is a separate concern handled by the
    SSH layer (SetEnv) and tmux's ``-e`` flag on new-session; see
    ``sessions/tmux.create_session``.
    """
    variables = {
        "session_name": session_name,
        "workspace_name": workspace_name,
    }

    raw_command = template.restart_command if restart and template.restart_command else template.command
    return _substitute_template_vars(raw_command, variables)


# -- Liveness checks -------------------------------------------------------


def _pid_alive(pid: int, *, target: Transport) -> bool:
    """Check if a PID is alive via /proc."""
    return target.run(f"test -d /proc/{pid}", check=False).ok


def _get_boot_id(target: Transport) -> str | None:
    """Read the current VM boot ID. Returns None on failure."""
    result = target.run("cat /proc/sys/kernel/random/boot_id", check=False)
    boot_id = (getattr(result, "stdout", "") or "").strip()
    return boot_id or None


def check_session_status(
    session: SessionRow,
    *,
    target: Transport,
) -> SessionStatus:
    """Determine session status. Dispatches by session type.

    No DB side effects. Raises ``StateError`` when the session row predates
    the per-session-socket model introduced by the env-and-secrets SDD
    (``socket_path is None`` for an admin session). The hint points the
    operator at ``agw session restart <name>``, which migrates the row to
    the new shape via a surgical kill of the named session on the default
    tmux server + a fresh ``create_tmux_session`` under a per-session
    socket. Callers that aren't ``restart_session`` (attach, stop, etc.)
    can't safely migrate, so they surface the typed error and let the
    operator restart.
    """
    if session.pid == PID_STOPPED:
        return SessionStatus.STOPPED
    if session.pid is None or session.boot_id is None:
        return SessionStatus.UNKNOWN

    if session.socket_path is not None:
        return _check_dedicated_session(session, target=target)
    # Legacy admin session predating per-session sockets. Surface as a
    # typed StateError so the CLI's top-level error wrapper renders it
    # as a one-liner; the new admin-mode path always stores a
    # socket_path.
    raise StateError(
        f"session '{session.name}' has no socket_path",
        entity_kind="session",
        entity_name=session.name,
        hint=(
            "This session predates the per-session-socket model introduced by "
            f"the env-and-secrets SDD. Run `agw session restart {session.name}` "
            "to migrate it to the new shape."
        ),
    )


def _check_dedicated_session(session: SessionRow, *, target: Transport) -> SessionStatus:
    """Sessions with their own tmux server and socket. Applies uniformly to
    admin and agent sessions after the env-and-secrets SDD migrated admin
    sessions to per-session sockets.
    """
    from agentworks.sessions.tmux import tmux_cmd

    q_session = shlex.quote(session.name)
    cmd = tmux_cmd(f"has-session -t {q_session}", session.socket_path) + " 2>/dev/null"
    result = target.run(cmd, check=False)
    if result.returncode == SSH_TRANSPORT_ERROR:
        return SessionStatus.UNKNOWN  # SSH transport failure, not a session state
    if result.ok:
        return SessionStatus.OK

    # has-session failed -- STOPPED or BROKEN?
    assert session.pid is not None and session.pid > 0
    current_boot = _get_boot_id(target)
    if current_boot is None:
        return SessionStatus.UNKNOWN  # can't verify boot cycle, unsafe to offer --force
    if session.boot_id is not None and session.boot_id != current_boot:
        return SessionStatus.STOPPED  # stale boot, PID is meaningless
    if not _pid_alive(session.pid, target=target):
        return SessionStatus.STOPPED  # process is dead
    return SessionStatus.BROKEN  # same boot, process alive, socket unreachable


def batch_check_status(
    sessions: list[SessionRow],
    *,
    target: Transport,
) -> dict[str, SessionStatus]:
    """Check status for multiple sessions in one SSH call per VM.

    Returns {session_name: SessionStatus}. Sessions with pid=None or PID_STOPPED
    are excluded (callers handle those via the enum directly).
    """
    from agentworks.sessions.tmux import tmux_cmd

    checkable = [s for s in sessions if s.pid is not None and s.pid > 0 and s.boot_id is not None]
    if not checkable:
        return {}

    # Build compound command: has-session with inline boot_id + PID for any
    # session whose has-session probe fails. Admin and agent sessions now
    # follow the same dedicated-socket model after the env-and-secrets SDD.
    # Legacy admin sessions with socket_path=None are skipped here with a
    # one-time warning so that `agw session list` against a VM with a mix of
    # legacy and new sessions still surfaces the new ones cleanly; the
    # operator-facing single-session paths (`session attach`, etc.) go
    # through `check_session_status`, which raises a typed StateError
    # pointing at `agw session restart` (the primitive that auto-migrates).
    legacy = [s.name for s in checkable if s.socket_path is None]
    if legacy:
        names = ", ".join(sorted(legacy))
        output.warn(
            f"{len(legacy)} session(s) predate the per-session-socket model; "
            f"`agw session restart` migrates them to the new shape: {names}"
        )

    parts = []
    for s in checkable:
        if s.socket_path is None:
            continue
        q_session = shlex.quote(s.name)  # quoted for tmux -t argument
        name = s.name  # raw for output field (names are validated, no shell-special chars)
        has_cmd = tmux_cmd(f"has-session -t {q_session}", s.socket_path)
        parts.append(
            f"{has_cmd} 2>/dev/null; "
            f"if [ $? -ne 0 ]; then "
            f"BOOT=$(cat /proc/sys/kernel/random/boot_id); "
            f"test -d /proc/{s.pid}; "
            f"echo \"S:{name}:1:$BOOT:$?\"; "
            f"else echo \"S:{name}:0\"; fi"
        )
    if not parts:
        return {}
    cmd = "; ".join(parts)

    result = target.run(cmd, check=False)
    stdout = getattr(result, "stdout", "") or ""

    status_map: dict[str, SessionStatus] = {}
    # Build a quick lookup for stored boot_ids
    boot_ids = {s.name: s.boot_id for s in checkable}

    for line in stdout.strip().splitlines():
        if not line.startswith("S:"):
            continue
        fields = line.split(":", maxsplit=4)
        if len(fields) < 3:
            continue
        name = fields[1]
        exit_code = fields[2]

        if exit_code == "0":
            status_map[name] = SessionStatus.OK
        elif len(fields) == 5:
            # Agent session failure: S:name:1:<boot_id>:<pid_exit>
            current_boot = fields[3]
            pid_exit = fields[4]
            if not current_boot:
                # Boot ID read failed -- can't safely determine STOPPED vs BROKEN
                pass  # omit from map, callers treat missing entries as unknown
            else:
                stored_boot = boot_ids.get(name)
                if stored_boot and stored_boot != current_boot:
                    status_map[name] = SessionStatus.STOPPED  # stale boot
                elif pid_exit == "0":
                    status_map[name] = SessionStatus.BROKEN  # PID alive, socket unreachable
                else:
                    status_map[name] = SessionStatus.STOPPED  # PID dead
        else:
            # Admin session failure
            status_map[name] = SessionStatus.STOPPED

    return status_map


# -- Interactive prompts (used by create_session) --------------------------


def _prompt_workspace_choice(
    db: Database, vm_filter: str | None
) -> tuple[str | None, bool]:
    """Pick an existing workspace or commit to creating a new one.

    Returns ``(workspace_name, new_workspace)`` where exactly one is the
    operator's choice: either an existing-workspace name (and
    ``new_workspace=False``) or ``new_workspace=True`` with no name (the
    caller defaults the new workspace's name to the session name).

    Always prompts -- no single-workspace auto-select. Including
    ``[Create new]`` as the last option makes interactive mode the
    functional equivalent of passing ``--new-workspace`` / ``--workspace``
    on the CLI.

    ``vm_filter`` narrows the chooser to workspaces on that VM when any
    other anchor (``--vm`` or ``--agent``) has already pinned one. The
    info line above the chooser tells the operator the filter is active
    so a missing workspace doesn't look like a bug.
    """
    if not output.is_interactive():
        raise ValidationError(
            "workspace is required in non-interactive mode",
            entity_kind="session",
            hint="pass --workspace <name> or --new-workspace",
        )
    all_workspaces = db.list_workspaces()
    if vm_filter is not None:
        workspaces = [w for w in all_workspaces if w.vm_name == vm_filter]
        if len(workspaces) < len(all_workspaces):
            output.info(f"Only showing workspaces on VM '{vm_filter}'")
    else:
        workspaces = all_workspaces
    options = [
        f"{ws.name}  (vm: {ws.vm_name}, template: {ws.template or '<none>'})"
        for ws in workspaces
    ]
    options.append("[Create new workspace]")
    idx = output.choose("Select a workspace:", options)
    if idx == len(options) - 1:
        return None, True
    return workspaces[idx].name, False


def _prompt_mode_choice(
    db: Database, vm: VMRow | None
) -> tuple[str | None, bool, bool]:
    """Pick admin, an existing agent, or commit to creating a new agent.

    Returns ``(agent_name, new_agent, admin)``. Exactly one of these
    encodes the operator's choice:
    - ``agent_name=<name>, new_agent=False, admin=False`` for an
      existing agent.
    - ``agent_name=None, new_agent=True, admin=False`` for ``[Create new]``.
    - ``agent_name=None, new_agent=False, admin=True`` for ``admin``.

    When ``vm`` is known, lists only agents on that VM (and prints an
    info line if other-VM agents got filtered out). When ``vm`` is
    ``None`` -- the VM hasn't been determined yet -- lists agents
    across all VMs, labeling each with its VM so an operator's pick
    of an existing agent pins the VM downstream. This is the path
    that lets ``agw session create my-sess --new-workspace`` resolve
    the VM via the mode prompt's agent pick rather than a separate
    VM prompt.

    Always prompts -- no single-option auto-select.
    """
    if not output.is_interactive():
        raise ValidationError(
            "session mode is required in non-interactive mode",
            entity_kind="session",
            hint="pass --admin, --agent <name>, or --new-agent",
        )
    all_agents = db.list_agents()
    if vm is not None:
        candidates = [a for a in all_agents if a.vm_name == vm.name]
        if len(candidates) < len(all_agents):
            output.info(f"Only showing agents on VM '{vm.name}'")
    else:
        candidates = all_agents
    options = ["admin"]
    for a in candidates:
        options.append(
            f"agent: {a.name}  (vm: {a.vm_name}, template: {a.template or '<none>'})"
        )
    options.append("[Create new agent]")
    idx = output.choose("Run session as:", options)
    if idx == 0:
        return None, False, True  # admin
    if idx == len(options) - 1:
        return None, True, False  # new agent
    return candidates[idx - 1].name, False, False  # existing agent


def _prompt_vm(db: Database) -> VMRow:
    """Pick a VM when nothing else pins it.

    The only auto-resolution helper that survives for sessions:
    workspace and mode are session-semantic and demand explicit
    operator intent (``--workspace``/``--new-workspace``,
    ``--admin``/``--agent``/``--new-agent``). VM is infrastructure --
    pick a host; the choice doesn't change what the session IS, just
    where it runs. Reached only when the operator hasn't passed
    ``--vm`` and no workspace / agent anchor was available to
    pin the VM (e.g. ``--new-workspace --admin`` or
    ``--new-workspace --new-agent``, both without ``--vm``). Filters
    out VMs whose init is incomplete: a session on a half-initialized
    VM would just fail downstream.
    """
    from agentworks.db import InitStatus

    vms = db.list_vms()
    usable = [v for v in vms if v.init_status in {InitStatus.COMPLETE.value, InitStatus.PARTIAL.value}]
    if not usable:
        raise NotFoundError(
            "no VMs available",
            entity_kind="vm",
            hint="Create one with 'agw vm create', or pass --vm to override.",
        )
    if len(usable) == 1:
        output.info(f"Using VM '{usable[0].name}'")
        return usable[0]
    if not output.is_interactive():
        raise ValidationError(
            "--vm is required in non-interactive mode when no workspace or agent pins the VM",
            entity_kind="session",
        )
    options = [f"{v.name}  ({v.site})" for v in usable]
    idx = output.choose("Select a VM:", options)
    return usable[idx]


# -- Public API ------------------------------------------------------------


def create_session(
    db: Database,
    config: Config,
    *,
    name: str,
    template_name: str | None = None,
    # Workspace selection (CLI-flag-shaped; service consolidates):
    workspace: str | None = None,
    new_workspace: bool = False,
    workspace_name: str | None = None,
    workspace_template: str | None = None,
    # Agent / admin selection (CLI-flag-shaped; service consolidates):
    agent: str | None = None,
    new_agent: bool = False,
    agent_name: str | None = None,
    agent_template: str | None = None,
    admin: bool = False,
    # VM anchor (validated against workspace/agent VMs when both specified):
    vm_name: str | None = None,
) -> None:
    """Create and start a session.

    Accepts the same flag combinations the ``agw session create`` CLI
    surfaces, validates them, prompts the operator for anything left
    unspecified (where interactive), and atomically provisions whichever
    ephemeral resources (workspace, agent) the operator requested
    alongside the session itself. On any failure after a mutation
    begins, every ephemeral resource created during the call is rolled
    back.

    Args:
        name: Session name.
        template_name: Session template (defaults to the operator's default).
        workspace: Existing workspace to attach this session to. Mutex
            with ``new_workspace``.
        new_workspace: When ``True``, create a new workspace.
        workspace_name: Name for the new workspace (defaults to ``name``
            when omitted). Requires ``new_workspace=True``.
        workspace_template: Template for the new workspace. Requires
            ``new_workspace=True``.
        agent: Existing agent name. Mutex with ``new_agent`` and ``admin``.
        new_agent: When ``True``, create a new agent.
        agent_name: Name for the new agent (defaults to ``name`` when
            omitted). Requires ``new_agent=True``.
        agent_template: Template for the new agent. Requires
            ``new_agent=True``.
        admin: When ``True``, run the session as the VM admin (no agent).
            Mutex with ``agent`` and ``new_agent``.
        vm_name: Target VM. Optional when an existing workspace or agent
            pins the VM; required when no other anchor does. When
            specified alongside other anchors, must agree with them.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import validate_name
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    # build_registry runs first so framework miss-policies (e.g. typos
    # in agent template's git_credentials list, future TemplateReference
    # typos on inherits) surface as clean framework errors before any
    # flag validation, DB lookup, or ephemeral-resource creation. The
    # registry isn't yet consumed by create_session's flow (operator-env
    # secrets resolve via resolve_for_command's SecretTarget shape later),
    # but constructing it here makes the entry point's error-surface
    # consistent with create_vm / create_agent / reinit_*.
    registry = build_registry(config)

    # ===== Flag-shape validation (mutexes + ephemeral-arg gating) ===========

    if workspace and new_workspace:
        raise ValidationError(
            "specify --workspace or --new-workspace, not both",
            entity_kind="session",
            entity_name=name,
        )
    if not new_workspace and (workspace_name or workspace_template):
        raise ValidationError(
            "--workspace-name and --workspace-template require --new-workspace",
            entity_kind="session",
            entity_name=name,
        )
    agent_modes = sum(1 for x in (bool(agent), new_agent, admin) if x)
    if agent_modes > 1:
        raise ValidationError(
            "specify at most one of --agent, --new-agent, or --admin",
            entity_kind="session",
            entity_name=name,
        )
    if not new_agent and (agent_name or agent_template):
        raise ValidationError(
            "--agent-name and --agent-template require --new-agent",
            entity_kind="session",
            entity_name=name,
        )

    # ===== Canonicalize CLI-flag shape into internal form ===================
    #
    # After this block:
    #   workspace_name : str | None   -- the workspace's name (None until
    #                                    DB lookup / default-to-session-name)
    #   new_workspace  : bool         -- True iff we're creating it
    #   workspace_template : str | None
    #   agent_name : str | None       -- the agent's name (None == admin mode)
    #   new_agent  : bool
    #   agent_template : str | None
    #
    # ``workspace`` / ``agent`` / ``admin`` are consumed here and unused below.

    if workspace:
        workspace_name = workspace
    if agent:
        agent_name = agent
    if admin:
        agent_name = None
        new_agent = False

    # ===== Early VM-anchor narrowing for the workspace prompt ===============
    #
    # If ``--vm`` and/or ``--agent`` were specified, they already pin a VM.
    # Load the agent row now (rather than in the later VM-anchor block) so
    # we can:
    #   1. Cross-check ``--vm`` against the agent's VM before any prompt
    #      fires (no point prompting for a workspace when we know the
    #      command is inconsistent).
    #   2. Filter the workspace chooser to workspaces on the known VM,
    #      so the operator doesn't have to mentally exclude irrelevant
    #      entries (and so picking one on the wrong VM isn't reachable).
    existing_agent: AgentRow | None = None
    known_vm: str | None = vm_name
    if not new_agent and agent_name is not None:
        existing_agent = db.get_agent(agent_name)
        if existing_agent is None:
            raise NotFoundError(
                f"agent '{agent_name}' not found",
                entity_kind="agent",
                entity_name=agent_name,
            )
        if known_vm is not None and known_vm != existing_agent.vm_name:
            raise ValidationError(
                f"VM mismatch: --vm={known_vm}, agent '{agent_name}'={existing_agent.vm_name}",
                entity_kind="session",
                entity_name=name,
            )
        known_vm = existing_agent.vm_name

    # ===== Workspace prompt (force explicit choice even with one option) ===
    #
    # No auto-select: workspace is part of the session's identity, and a
    # single-workspace "shortcut" today silently changes behavior the day
    # the operator adds a second one. Always prompt. Include a
    # ``[Create new]`` option so the interactive UX is fully equivalent
    # to passing ``--new-workspace`` on the CLI. Filter to ``known_vm``
    # when other anchors pin one. Non-interactive: raise.

    if not workspace_name and not new_workspace:
        chosen_existing, new_workspace = _prompt_workspace_choice(db, known_vm)
        if chosen_existing is not None:
            workspace_name = chosen_existing

    # ===== Pure validation (no SSH, no mutations) ===========================

    # Default ephemeral resource names to the session name when omitted.
    if new_workspace and workspace_name is None:
        workspace_name = name
    if new_agent and agent_name is None:
        agent_name = name
    assert workspace_name is not None  # invariant after canonicalize + prompt

    validate_name(name)
    if new_workspace:
        validate_name(workspace_name)
    if new_agent:
        assert agent_name is not None
        validate_name(agent_name)

    # DB existence checks. Session must not exist. Ephemeral workspace /
    # agent must not exist; existing workspace / agent must exist.
    if db.get_session(name) is not None:
        raise AlreadyExistsError(
            f"session '{name}' already exists",
            entity_kind="session",
            entity_name=name,
        )
    if new_workspace and db.get_workspace(workspace_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{workspace_name}' already exists",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    if new_agent:
        assert agent_name is not None  # defaulted to ``name`` above
        if db.get_agent(agent_name) is not None:
            raise AlreadyExistsError(
                f"agent '{agent_name}' already exists",
                entity_kind="agent",
                entity_name=agent_name,
            )

    # ===== Existing-workspace lookup + VM-anchor accretion =================
    #
    # If the operator named an existing workspace, load it now -- both
    # to validate it exists and to contribute its VM to ``known_vm``
    # before the mode prompt fires. This lets the mode prompt filter
    # agents by the workspace's VM, and lets a downstream VM mismatch
    # surface before the mode prompt rather than after.
    existing_ws: WorkspaceRow | None = None
    if not new_workspace:
        existing_ws = db.get_workspace(workspace_name)
        if existing_ws is None:
            raise NotFoundError(
                f"workspace '{workspace_name}' not found",
                entity_kind="workspace",
                entity_name=workspace_name,
            )
        if known_vm is not None and known_vm != existing_ws.vm_name:
            anchor_label = "--vm" if vm_name is not None else f"agent '{agent_name}'"
            raise ValidationError(
                f"VM mismatch: {anchor_label}={known_vm}, "
                f"workspace '{workspace_name}'={existing_ws.vm_name}",
                entity_kind="session",
                entity_name=name,
            )
        known_vm = existing_ws.vm_name

    # ===== Mode prompt (force explicit choice; no silent default) ==========
    #
    # Fires before VM resolution so the operator's pick of an existing
    # agent can pin the VM (one less prompt for the common case). When
    # ``known_vm`` is set, the chooser filters to that VM's agents; when
    # not, it shows agents across all VMs (each labeled with its VM) and
    # picking one sets the VM. ``admin`` and ``[Create new agent]`` don't
    # pin a VM -- those paths fall through to the VM-prompt at the end.

    if agent_name is None and not new_agent and not admin:
        vm_for_mode_prompt: VMRow | None = None
        if known_vm is not None:
            vm_for_mode_prompt = db.get_vm(known_vm)
            assert vm_for_mode_prompt is not None  # known_vm was sourced from a real row

        chosen_agent, new_agent, admin = _prompt_mode_choice(db, vm_for_mode_prompt)
        if chosen_agent is not None:
            # Existing-agent pick: the prompt already filtered by
            # ``known_vm`` (if set) OR the picked agent's VM becomes
            # the new known_vm. No vm-anchor cross-check needed -- the
            # filter / pick path enforces agreement by construction.
            agent_name = chosen_agent
            existing_agent = db.get_agent(agent_name)
            assert existing_agent is not None  # came from list_agents
            known_vm = existing_agent.vm_name

        # Re-run the agent-specific default / validation / existence
        # checks that the upfront block did for the flag path. The
        # workspace equivalents ran already because the workspace
        # prompt sits BEFORE that block; the mode prompt sits AFTER
        # because it may need to pin the VM via an existing-agent
        # pick. Without this, a ``[Create new agent]`` pick lands at
        # the eager-resolve SecretTarget with ``is_admin_mode=True``
        # (wrong scope) and asserts ``agent_name is not None``
        # inside the ephemeral-create block.
        if new_agent:
            if agent_name is None:
                agent_name = name
            validate_name(agent_name)
            if db.get_agent(agent_name) is not None:
                raise AlreadyExistsError(
                    f"agent '{agent_name}' already exists",
                    entity_kind="agent",
                    entity_name=agent_name,
                )

    # ===== VM resolution (final step; prompts only if nothing pinned it) ===
    #
    # By this point every anchor (vm_name, existing workspace, existing
    # agent -- whether passed as a flag or picked from a prompt) has
    # contributed to ``known_vm`` and the cross-checks fired as each
    # anchor was loaded. If ``known_vm`` is still ``None`` we genuinely
    # have no anchor (e.g. ``--new-workspace --admin`` with no ``--vm``),
    # so prompt for VM. The cross-check below is defense-in-depth: a
    # future refactor that adds a new anchor without piping it through
    # ``known_vm`` would trip it.
    if known_vm is None:
        vm = _prompt_vm(db)
    else:
        loaded_vm = db.get_vm(known_vm)
        if loaded_vm is None:
            raise NotFoundError(
                f"VM '{known_vm}' not found",
                entity_kind="vm",
                entity_name=known_vm,
            )
        vm = loaded_vm
    target_vm_name = vm.name

    vm_anchors: list[tuple[str, str]] = []
    if vm_name is not None:
        vm_anchors.append(("--vm", vm_name))
    if existing_ws is not None:
        vm_anchors.append((f"workspace '{workspace_name}'", existing_ws.vm_name))
    if existing_agent is not None:
        vm_anchors.append((f"agent '{agent_name}'", existing_agent.vm_name))
    if any(candidate != target_vm_name for _, candidate in vm_anchors):
        detail = ", ".join(f"{src}={v}" for src, v in vm_anchors)
        raise ValidationError(
            f"VM mismatch: {detail}",
            entity_kind="session",
            entity_name=name,
        )

    # ===== Template resolution (no SSH, no mutations) =======================

    template = _resolve_template(registry, template_name)

    # ===== Build: the derived node graph ====================================
    #
    # The orchestrated composition: the command names only its direct
    # resources (this VM, the chosen workspace/agent, the session
    # template) and constructs each node ONCE; everything else enters
    # through declared edges (the VM row's site field, an ephemeral
    # agent template's git_credentials), and every edge holder shares
    # the same object (the walk enforces one-object-per-key loudly).
    # Construction is cheap and touches no secret machinery; the
    # walk union below is the boundary's source. Nothing resolves
    # yet.
    from agentworks.agents.nodes import (
        agent_template_node,
        credential_tokens,
        live_agent_node,
        pending_agent_node,
    )
    from agentworks.capabilities.base import (
        OperationScope,
        RunContext,
        ScopeLevel,
    )
    from agentworks.db import SYSTEM_SLUG_KEY
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.orchestration.unwind import RealizationLog
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.nodes import pending_session_node
    from agentworks.vms.nodes import live_vm_node
    from agentworks.workspaces.nodes import (
        live_workspace_node,
        pending_workspace_node,
    )

    resolver = Resolver(config, registry)

    vm_node = live_vm_node(db, config, registry, vm)

    workspace_node: LiveWorkspaceNode | PendingWorkspaceNode
    pending_workspace: PendingWorkspaceNode | None = None
    workspace_tmpl: ResolvedWorkspaceTemplate | None = None
    if new_workspace:
        # Cheap validation now, before the gate and before any secret
        # is touched: template resolution, the repo advisories
        # (config-only, no tokens), and the VM init-status guard fail
        # with zero prompts and zero VM starts, the bail-early
        # precedence every migrated command keeps.
        from agentworks.workspaces.manager import _guard_vm_status
        from agentworks.workspaces.templates import (
            resolve_template as _resolve_ws_tmpl,
        )

        workspace_tmpl = _resolve_ws_tmpl(registry, workspace_template)
        if workspace_tmpl.repo:
            from agentworks.git_credentials import remote_advisories

            for advisory in remote_advisories(registry, workspace_tmpl.repo):
                output.warn(advisory)
        _guard_vm_status(vm)
        pending_workspace = pending_workspace_node(
            db, config, workspace_name, vm_node, workspace_template,
        )
        workspace_node = pending_workspace
    else:
        assert existing_ws is not None  # loaded by the existing-workspace block
        workspace_node = live_workspace_node(existing_ws, vm_node)

    # The agent node: live (existing agent), pending (ephemeral), or
    # none (admin mode). A pending agent's declared git credentials
    # become edges through its template node: the graph replaces the
    # hand-rolled ephemeral provider fold, and the SAME agent object is
    # both the session's dep and the required-commands check's target
    # (the one-object contract), so the realization flip below is
    # observed without rewiring.
    agent_node: LiveAgentNode | PendingAgentNode | None = None
    pending_agent: PendingAgentNode | None = None
    agent_tmpl: ResolvedAgentTemplate | None = None
    agent_tmpl_node: AgentTemplateNode | None = None
    if new_agent:
        from agentworks.agents.templates import (
            resolve_template as _resolve_agent_tmpl,
        )

        assert agent_name is not None  # defaulted to ``name`` above
        agent_tmpl = _resolve_agent_tmpl(registry, agent_template)
        agent_tmpl_node = agent_template_node(registry, agent_tmpl)
        pending_agent = pending_agent_node(
            db, config, agent_name, agent_tmpl_node, vm_node,
        )
        agent_node = pending_agent
    elif agent_name is not None:
        assert existing_agent is not None  # loaded by the anchor / prompt blocks
        agent_node = live_agent_node(existing_agent, vm_node)

    session_node = pending_session_node(
        db,
        config,
        name,
        template,
        agent=agent_node,
        admin=agent_name is None,
        workspace=workspace_node,
        vm=vm_node,
    )
    nodes = walk(session_node)

    # The walk supplies the boundary union, and the session's
    # runtime env chain joins the SAME pass through the pre-create
    # SecretTarget seam, so the env-chain secrets and the graph's
    # config/token secrets stay ONE prompt session. Hermeticity is
    # unchanged: exactly what the target's env references prompts here,
    # and what rides the shells' own composition roots still does.
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    resolver.register_targets(
        [
            _session_secret_target_pre_create(
                registry,
                name=name,
                workspace_name=workspace_name,
                vm=vm,
                session_template=template,
                new_workspace=new_workspace,
                workspace_template=workspace_template,
                existing_workspace=existing_ws,
                new_agent=new_agent,
                agent_template=agent_template,
                existing_agent=existing_agent,
                is_admin_mode=(agent_name is None),
            ),
        ]
    )

    scope = OperationScope(
        level=ScopeLevel.SESSION,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=target_vm_name,
        workspace=workspace_name,
        session=name,
        agent=agent_name,
        admin=agent_name is None,
    )

    # The activation gate replaces this command's imperative
    # ensure_active + vm_active holds: opened once, before the
    # preflight sweep (so every probe reaches a live target), held
    # through the whole command, with its just-in-time values seeding
    # the boundary resolver so nothing resolves or prompts twice.
    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # Reload the VM row: the gate may have rejoined Tailscale (only
        # when the VM was stopped/deallocated) and updated
        # ``vms.tailscale_host``. The in-memory ``vm`` from our pre-check
        # would otherwise read stale and the check below could spuriously
        # raise. (The SecretTarget above read only vm.template, which a
        # refresh cannot change, so the pre-refresh row was safe to
        # target; the nodes keep their construction row, whose identity
        # fields a refresh cannot change either.)
        refreshed_vm = db.get_vm(target_vm_name)
        assert refreshed_vm is not None  # existed above; the gate cannot remove it
        vm = refreshed_vm
        if vm.tailscale_host is None:
            raise StateError(
                f"VM '{vm.name}' has no Tailscale address",
                entity_kind="vm",
                entity_name=vm.name,
            )

        from agentworks.ssh import SSHLogger

        logger = SSHLogger(vm.name, "session-create")
        target = transport(vm, config, logger=logger)
        run_command: RunCommand = target.run

        # Preflight phase: name the resources this create touches (the
        # session template, any ephemeral workspace / agent templates, and
        # the ephemeral agent's git credentials) in the same
        # <kind>/<name> form vm/agent create use, then run the readiness
        # sweep. Framed as a phase so session create reads like a plan
        # executing, matching vm create.
        output.phase("Preflight")
        output.detail(f"Checking session-template/{template.name}...")
        if new_workspace:
            assert workspace_tmpl is not None  # resolved at build above
            output.detail(f"Checking workspace-template/{workspace_tmpl.name}...")
        if new_agent:
            assert agent_tmpl is not None  # resolved at build above
            output.detail(f"Checking agent-template/{agent_tmpl.name}...")
        if agent_tmpl_node is not None:
            from agentworks.vms.initializer import announce_git_credentials

            announce_git_credentials(
                {
                    cred.provider.owner_name: cred.provider
                    for cred in agent_tmpl_node.credentials
                }
            )

        # Probe direct agent SSH for an EXISTING agent before any
        # prompt or mutation: a pre-rollout agent surfaces as an
        # actionable StateError with nothing to roll back (the
        # orchestrated flow moves this probe, and the required-commands
        # probe below, ahead of the resolve boundary: the
        # earlier-failure win). An ephemeral agent's probe runs right
        # after its realization below.
        agent_target: Transport | None = None
        if agent_node is not None and not new_agent:
            from agentworks.agents.manager import _assert_agent_ssh_works
            from agentworks.transports import agent_transport

            assert existing_agent is not None
            agent_target = agent_transport(vm, config, existing_agent)
            _assert_agent_ssh_works(agent_target, existing_agent)

        # PREFLIGHT-ALL against the one command-start context: the
        # required-commands check probes a realized (existing) agent or
        # the admin target NOW and defers on a pending one; each
        # git-credential provider predicts its token's resolvability.
        # Then the boundary resolve: the walk-away point.
        preflight_all(
            nodes,
            RunContext(
                config=config,
                operation_scope=scope,
                admin_target=target,
                agent_target=agent_target,
            ),
        )

        output.phase("Resolving Secrets")
        resolver.resolve()
        secret_values = resolver.values

        def scoped_ctx(secret_names: tuple[str, ...]) -> RunContext:
            return RunContext(
                config=config,
                operation_scope=scope,
                secrets=ScopedSecrets(secret_values, secret_names),
            )

        # ===== Dependency-ordered roll-forward ==============================
        #
        # Realize the pending nodes in dependency order, recording each
        # completed realization; on any later failure the log unwinds
        # them in reverse (agent before workspace, today's proven
        # rollback order). The session's own partial state is cleaned by
        # its node's teardown in the slice below, and a COMPLETED
        # session (tmux up) is deliberately never rolled back.
        log = RealizationLog()
        try:
            # ---- Ephemeral realizations (each its own plan stage) ----------
            if pending_workspace is not None:
                from agentworks.workspaces.realize import realize_workspace

                assert workspace_tmpl is not None  # resolved at build above
                output.phase("Creating Workspace")
                output.detail(
                    f"Creating workspace '{workspace_name}' on VM '{vm.name}' "
                    f"(template: {workspace_tmpl.name})..."
                )
                realize_workspace(
                    db,
                    config,
                    registry,
                    name=workspace_name,
                    vm=vm,
                    template=workspace_tmpl,
                )
                log.mark_realized(pending_workspace)
            if pending_agent is not None:
                from agentworks.agents.realize import realize_agent

                assert agent_name is not None  # defaulted to ``name`` above
                assert agent_tmpl is not None and agent_tmpl_node is not None
                output.phase("Creating Agent")
                output.detail(
                    f"Creating agent '{agent_name}' on VM '{vm.name}' "
                    f"(template: {agent_tmpl.name})..."
                )
                # Each credential's token, read through its node's
                # SCOPED delivery (the boundary pass above covered
                # them; the graph-derived fold replaces the nested
                # create_agent's git_tokens hand-off).
                git_tokens = credential_tokens(agent_tmpl_node, scoped_ctx)
                realize_agent(
                    db,
                    config,
                    registry,
                    name=agent_name,
                    vm=vm,
                    template=agent_tmpl,
                    git_tokens=git_tokens,
                )
                log.mark_realized(pending_agent)

            # ---- The session's own realizing slice -------------------------
            ws = _require_workspace(db, workspace_name)

            resolved_agent_name: str | None = None
            agent_row: AgentRow | None = None
            if agent_name is not None:
                mode = SessionMode.AGENT
                agent_row = db.get_agent(agent_name)
                if agent_row is None:
                    raise NotFoundError(
                        f"agent '{agent_name}' not found",
                        entity_kind="agent",
                        entity_name=agent_name,
                    )
                # Unreachable in practice: existing-agent VM was already
                # cross-checked in the upfront anchor block, and a fresh
                # ephemeral agent was just created on this same VM. Kept as
                # a tripwire so a future refactor that reorders or drops the
                # upfront check fails loudly rather than silently corrupting
                # cross-VM state.
                if agent_row.vm_name != vm.name:
                    raise ValidationError(
                        f"agent '{agent_name}' is on VM '{agent_row.vm_name}', "
                        f"but workspace '{workspace_name}' is on VM '{vm.name}'",
                        entity_kind="session",
                        entity_name=name,
                    )
                linux_user = agent_row.linux_user
                resolved_agent_name = agent_name
                if agent_target is None:
                    # The ephemeral agent just realized: probe its direct
                    # SSH BEFORE any session mutation, same contract as
                    # the existing-agent probe above.
                    from agentworks.agents.manager import _assert_agent_ssh_works
                    from agentworks.transports import agent_transport

                    agent_target = agent_transport(vm, config, agent_row)
                    _assert_agent_ssh_works(agent_target, agent_row)
            else:
                mode = SessionMode.ADMIN
                linux_user = vm.admin_username

            # Op-start runup: the required-commands check probes a
            # just-realized ephemeral agent here (it deferred at
            # preflight; the log's flip above is what it observed). For
            # targets that were realized at preflight the check already
            # fired and this is a no-op.
            session_node.runup(
                RunContext(
                    config=config,
                    operation_scope=scope,
                    admin_target=target,
                    agent_target=agent_target,
                )
            )

            # Compute socket path up front (deterministic from linux_user +
            # session name). Needed for the DB insert since the CHECK
            # constraint requires agent sessions to have a socket_path.
            expected_socket: str | None = None
            if mode == SessionMode.AGENT:
                from agentworks.sessions.tmux import agent_socket_path

                expected_socket = agent_socket_path(linux_user, name)

            mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
            output.phase("Starting Session")
            output.detail(
                f"Starting session '{name}' on workspace '{workspace_name}' "
                f"({mode_label}, template: {template.name})..."
            )

            try:
                # Everything that creates partial session state (on-VM group
                # membership, implicit-grant row, session row, restricted-config
                # write, tmux session) runs inside this block so a KI /
                # exception anywhere here triggers the session node's
                # partial-state teardown.
                if resolved_agent_name is not None:
                    # Auto-grant implicit workspace access if the agent has no
                    # existing grant on this workspace.
                    if not db.has_any_grant(resolved_agent_name, workspace_name):
                        from agentworks.agents.grants import add_to_workspace_group

                        add_to_workspace_group(
                            vm, config, db, linux_user, workspace_name
                        )
                    db.insert_agent_grant(
                        resolved_agent_name, workspace_name, "implicit", session_name=name
                    )

                # Insert DB record before any tmux work so a crash mid-create
                # leaves a recoverable row (and the teardown can find it to
                # delete).
                db.insert_session(
                    name,
                    workspace_name,
                    template.name,
                    mode,
                    agent_name=resolved_agent_name,
                    created_workspace=pending_workspace is not None,
                    created_agent=pending_agent is not None,
                    socket_path=expected_socket,
                )

                deploy_restricted_config(run_command, history_limit=config.session.history_limit)
                command = _build_session_command(
                    template, session_name=name, workspace_name=workspace_name
                )
                session_env = _resolve_session_env(
                    registry,
                    values=secret_values,
                    db=db,
                    vm=vm,
                    ws=ws,
                    session_name=name,
                    session_template=template,
                    mode=mode,
                    agent_name=resolved_agent_name,
                    linux_user=linux_user,
                )
                # Pick the SSH transport for tmux operations:
                # - admin sessions: admin's run_command (unchanged)
                # - agent sessions: agent's run_command (direct
                #   target-user SSH). agent_target was built and probed above
                #   so a pre-rollout agent never reaches this point. admin's
                #   ``target`` is still passed for socket-root setup which
                #   requires root.
                session_run_command: RunCommand
                if mode == SessionMode.AGENT:
                    assert agent_target is not None  # built in the agent branches above
                    session_run_command = agent_target.run
                else:
                    session_run_command = run_command
                sock, pid = create_tmux_session(
                    name,
                    ws.workspace_path,
                    command,
                    linux_user,
                    run_command=session_run_command,
                    target=target,
                    admin_username=vm.admin_username,
                    is_admin=(mode == SessionMode.ADMIN),
                    env=session_env,
                )
            except (KeyboardInterrupt, Exception):
                # Session-internal cleanup only (DB row, grant, group
                # membership: the node's partial-state teardown). The
                # realized ephemerals are unwound by the outer handlers,
                # whose warn prints one clean reason line before the
                # rollback's delete messages start landing.
                session_node.teardown()
                raise

            # The session's realizing slice is complete: flip the node.
            # Deliberately NOT via the realization log: a completed
            # session (tmux up, row written) is never rolled back, so
            # failures past this point unwind only the ephemerals, and
            # the session survives them. That pins the completed-session
            # window as non-rollbackable.
            session_node.mark_realized()

            # Persist socket path, PID, and boot ID
            if sock:
                db.update_session_socket_path(name, sock)
            if pid is not None:
                boot_id = _get_boot_id(target)
                if boot_id is not None:
                    db.update_session_pid(name, pid, boot_id=boot_id)
                else:
                    output.warn(f"Could not read boot ID for session '{name}', PID not stored")
            else:
                output.warn(
                    f"Could not capture PID for session '{name}', will auto-repair on next access"
                )

            mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
            output.info(f"Session '{name}' started ({mode_label}, template: {template.name})")

            # Update tmuxinator config and add to console if it exists
            _regenerate_tmuxinator(db, config, vm, ws)
            from agentworks.sessions.console import add_session_to_console

            add_session_to_console(name, run_command=run_command, socket_path=sock)
        except KeyboardInterrupt:
            output.warn(f"Cancelling session create '{name}'... rolling back.")
            log.unwind()
            raise
        except Exception as e:
            # Print the reason BEFORE the rollback's delete-* messages so the
            # operator sees the failure context first, not after a stream of
            # 'Agent deleted' / 'Workspace deleted' lines. The CLI's
            # exception handler still prints the canonical 'Error: ...' line
            # with the typed hint at the very end; this warn just bridges
            # the silence between "thing X created" and the rollback output.
            output.warn(f"Session create '{name}' failed; rolling back. Reason: {e}")
            log.unwind()
            raise


def _execute_stop(
    targets: list[tuple[SessionRow, Transport, bool]],
    *,
    db: Database,
    force: bool = False,
) -> list[tuple[str, str]]:
    """Core stop logic: C-c all, single grace period, kill survivors.

    ``targets`` is ``[(session, target, target_owns_session)]``. When
    ``target_owns_session`` is True, the SSH user is the same uid that owns
    the tmux server (admin sessions over admin SSH, or agent sessions over
    agent SSH) and no sudo is needed for kill / socket cleanup. When False
    (admin SSH for an agent session in batch ops), sudo is needed.

    Handles both single and batch stops. Returns list of (name, error) failures.
    """
    import time

    from agentworks.sessions.tmux import force_kill_tmux_server, send_keys

    if not targets:
        return []

    # Phase 1: send C-c to all sessions (best effort).
    # This gives processes that handle SIGINT gracefully (save state, flush)
    # a chance to clean up before we kill the session. In practice, tmux
    # kill-session sends SIGHUP which cascades through the shell to children,
    # so the C-c is rarely necessary. Consider removing the C-c + grace
    # period if the 5-second wait becomes a pain point.
    output.detail("Sending C-c to stop any running commands...")
    for session, target, _ in targets:
        sock = session.socket_path
        with contextlib.suppress(Exception):
            send_keys(session.name, "C-c", run_command=target.run, socket_path=sock)

    # Phase 2: single grace period
    output.detail(f"Waiting {_STOP_GRACE_SECONDS}s for graceful exit...")
    time.sleep(_STOP_GRACE_SECONDS)

    # Phase 3: check survivors per VM (reuse existing targets). Status checks
    # only read /proc; sudo not relevant here. Group by target identity for
    # one batch-check SSH per (VM, transport).
    by_target: dict[int, tuple[Transport, list[SessionRow]]] = {}
    for session, target, _ in targets:
        tid = id(target)
        if tid not in by_target:
            by_target[tid] = (target, [])
        by_target[tid][1].append(session)

    survivor_map: dict[str, SessionStatus] = {}
    for target, group in by_target.values():
        survivor_map.update(batch_check_status(group, target=target))

    failed: list[tuple[str, str]] = []

    for session, target, target_owns_session in targets:
        # Cross-uid kill/cleanup (admin SSH against an agent session) needs
        # sudo. Same-uid ops do not.
        kill_sudo = not target_owns_session
        status = survivor_map.get(session.name)
        if status is None:
            # Status check failed (SSH error or parse issue) -- don't assume stopped
            failed.append((session.name, "could not verify session status after stop"))
            output.warn(f"Could not verify status of '{session.name}', not marking as stopped")
            continue
        if status == SessionStatus.OK or status == SessionStatus.BROKEN:
            output.detail(f"Killing session '{session.name}'")
            sock = session.socket_path
            killed = _kill_session(session.name, run_command=target.run, socket_path=sock)
            if not killed:
                # Race condition: session may have exited between survivor check and kill.
                # Recheck before treating as failure.
                recheck = check_session_status(session, target=target)
                if recheck == SessionStatus.STOPPED:
                    pass  # session exited on its own, that's success
                elif force and session.socket_path is not None and session.pid and session.pid > 0:
                    # Escalate to PID kill for agent sessions only (admin shares PID)
                    output.detail(f"tmux kill failed for '{session.name}', force-killing PID {session.pid}")
                    if not force_kill_tmux_server(
                        session.pid,
                        target=target,
                        socket_path=session.socket_path,
                        log=output.detail,
                        use_sudo=kill_sudo,
                    ):
                        failed.append((session.name, f"PID {session.pid} survived force-kill"))
                        continue
                else:
                    failed.append((session.name, f"tmux kill-session failed for '{session.name}'"))
                    if session.socket_path is not None and session.pid and session.pid > 0:
                        output.warn(f"Failed to stop '{session.name}' (tmux unreachable, use --force)")
                    else:
                        output.warn(f"Failed to stop '{session.name}' (tmux unreachable)")
                    continue

        # Clean up agent socket only after confirming the server process is dead
        if (
            session.socket_path
            and session.socket_path.startswith(AGENT_SOCKET_ROOT + "/")
            and session.pid
            and session.pid > 0
            and not _pid_alive(session.pid, target=target)
        ):
            target.run(f"rm -f {shlex.quote(session.socket_path)}", sudo=kill_sudo, check=False)

        db.update_session_pid(session.name, PID_STOPPED)
        output.info(f"Session '{session.name}' stopped")

    return failed


def stop_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
) -> None:
    """Stop a running session. Sends C-c first, then kills after a grace period."""
    from agentworks.sessions.tmux import force_kill_tmux_server

    session = _require_session(db, name)
    with _prepare_vm(db, config, session, operation="session-stop") as (
        _ws,
        vm,
        _run_command,
        _run_as_root,
        admin_target,
    ):
        session = _ensure_pid(session, target=admin_target, db=db)
        status = check_session_status(session, target=admin_target)

        if status == SessionStatus.STOPPED:
            output.info(f"Session '{name}' is already stopped")
            return
        # UNKNOWN is impossible here -- _ensure_pid raises on unresolvable sessions

        # Pick the destructive-op transport BEFORE doing anything destructive.
        # For agent sessions this also probes the agent's direct SSH so a
        # pre-rollout agent surfaces as an actionable StateError up front
        # rather than mid-kill. _build_session_target
        # always returns a same-uid target, so no sudo is needed for the
        # destructive ops below.
        target = _build_session_target(
            session, vm=vm, config=config, db=db, admin_target=admin_target
        )
        kill_sudo = False

        if status == SessionStatus.BROKEN:
            if not force:
                raise BrokenStateError(
                    f"session '{name}' is broken (PID alive but tmux unreachable).",
                    entity_kind="session",
                    entity_name=name,
                    hint="Use --force to kill the process.",
                )
            output.warn(f"Session '{name}' is broken (tmux unreachable), force-killing via PID")
            assert session.pid is not None
            killed = force_kill_tmux_server(
                session.pid,
                target=target,
                socket_path=session.socket_path,
                log=output.detail,
                use_sudo=kill_sudo,
            )
            if not killed:
                raise ExternalError(
                    f"failed to kill PID {session.pid} for session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                )
            db.update_session_pid(name, PID_STOPPED)
            output.info(f"Session '{name}' force-stopped")
            return

        # OK: delegate to shared stop logic. target_owns_session=True
        # because _build_session_target returned a same-uid target.
        failed = _execute_stop([(session, target, True)], db=db, force=force)
        if failed:
            raise ExternalError(
                f"failed to stop session '{name}': {failed[0][1]}",
                entity_kind="session",
                entity_name=name,
            )


def restart_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Restart a session. Prompts if running (--yes to skip). --force for BROKEN.

    Orchestrated: the live graph derives from the session's rows, the
    activation gate replaces the imperative ensure_active + hold, and
    the preflight sweep fires the required-commands probe BEFORE the
    kill (a missing binary aborts with the old session still running).
    Nothing here is created, so no realization log exists; the window
    after the kill is deliberately non-rollbackable (no unwind is
    consulted there), exactly the imperative shape.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    registry = build_registry(config)

    session = _require_session(db, name)
    ws = _require_workspace(db, session.workspace_name)
    vm = _require_vm_for_workspace(db, ws)
    template = _resolve_template(registry, session.template)

    # ===== Build: the live node graph from the rows =========================
    #
    # Everything exists, so every node is live and nothing is realized
    # or unwound: the session row names its agent, workspace, and VM,
    # and the domain factories construct one node per row (the VM row's
    # site field is its edge to the vm-site node, which holds the
    # platform instance). Construction registers the site's declared
    # secrets on the resolver; nothing resolves yet.
    from agentworks.agents.nodes import live_agent_node
    from agentworks.capabilities.base import (
        OperationScope,
        RunContext,
        ScopeLevel,
    )
    from agentworks.db import SYSTEM_SLUG_KEY
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.nodes import live_session_node
    from agentworks.vms.nodes import live_vm_node
    from agentworks.workspaces.nodes import live_workspace_node

    resolver = Resolver(config, registry)

    vm_node = live_vm_node(db, config, registry, vm)
    workspace_node = live_workspace_node(ws, vm_node)
    agent_node: LiveAgentNode | None = None
    if session.agent_name is not None:
        agent_row = db.get_agent(session.agent_name)
        if agent_row is None:
            raise NotFoundError(
                f"agent '{session.agent_name}' (referenced by session '{session.name}') not found",
                entity_kind="agent",
                entity_name=session.agent_name,
            )
        agent_node = live_agent_node(agent_row, vm_node)
    session_node = live_session_node(
        session,
        template,
        agent=agent_node,
        workspace=workspace_node,
        vm=vm_node,
    )
    nodes = walk(session_node)
    # The walk supplies the boundary union (the site's config secrets;
    # live nodes declare nothing else). The session's env chain is
    # deliberately NOT part of this boundary: it resolves after the
    # BROKEN/confirm gates below, the recorded bail-before-prompt
    # exception.
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)

    scope = OperationScope(
        level=ScopeLevel.SESSION,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm.name,
        workspace=ws.name,
        session=name,
        agent=session.agent_name,
        admin=session.agent_name is None,
    )

    # The activation gate replaces this command's imperative
    # ensure_active + vm_active hold: opened once, before the preflight
    # sweep, held through the whole command, its just-in-time values
    # seeding the boundary resolver.
    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        if vm.tailscale_host is None:
            raise StateError(
                f"VM '{vm.name}' has no Tailscale address",
                entity_kind="vm",
                entity_name=vm.name,
            )

        from agentworks.ssh import SSHLogger

        logger = SSHLogger(vm.name, "session-restart")
        admin_target = transport(vm, config, logger=logger)
        run_command: RunCommand = admin_target.run

        session = _ensure_pid(session, target=admin_target, db=db)

        # Legacy migration: sessions predating the per-session-socket model
        # have ``socket_path=None`` (they lived on the admin's default tmux
        # server, where session.pid identifies the server, not this
        # session). ``check_session_status`` would raise a typed StateError
        # for these; instead we recognize the shape, run a surgical
        # ``tmux kill-session -t <name>`` on the default server (no socket
        # path), and fall through to the create step. The downstream
        # ``create_tmux_session`` produces a per-session socket and the
        # subsequent ``db.update_session_socket_path`` lands the migration.
        is_legacy = session.socket_path is None and session.pid is not None and session.pid > 0
        if is_legacy:
            output.info(
                f"Session '{name}' uses the legacy default-tmux-server model; "
                "migrating to per-session socket."
            )
            status = SessionStatus.STOPPED  # placeholder; legacy branch owns the kill below
        else:
            status = check_session_status(session, target=admin_target)

        # Pick the destructive-op transport BEFORE any destructive action.
        # For agent sessions this builds an agent Transport and probes it
        # so a pre-rollout agent surfaces as an actionable StateError up
        # front rather than leaving us with a stopped session we can't
        # restart. Same transport is used for kill (above) and create
        # (below): every destructive step on an agent session goes via
        # direct agent SSH. _build_session_target always returns a
        # same-uid target, so no sudo is needed for kill.
        is_admin = session.mode == SessionMode.ADMIN.value
        session_target = _build_session_target(
            session, vm=vm, config=config, db=db, admin_target=admin_target
        )
        session_run_command: RunCommand = session_target.run
        kill_sudo = False

        # PREFLIGHT-ALL over the walk rooted at the live session node,
        # against the one command-start context: the required-commands
        # check's target (an existing agent, or the admin) is realized,
        # so it probes NOW, pre-resolve and PRE-KILL, and a missing
        # binary aborts the restart with the old session still running.
        # Then the boundary resolve for the graph's union (gate-resolved
        # values are already seeded, so nothing resolves twice).
        preflight_all(
            nodes,
            RunContext(
                config=config,
                operation_scope=scope,
                admin_target=admin_target,
                agent_target=None if is_admin else session_target,
            ),
        )
        resolver.resolve()

        # Bail-before-prompt: refuse the operation up front in the cases
        # where the operator either lacks the right flag (BROKEN + no
        # --force) or declines the confirm (OK + interactive 'no').
        # Eager-resolve of the env chain runs AFTER these checks so we
        # don't ask for secrets the command was about to discard.
        # UNKNOWN is impossible here (_ensure_pid raises on unresolvable
        # sessions). Legacy sessions short-circuit at ``status =
        # SessionStatus.STOPPED`` above, so neither gate fires for them;
        # migration is implicit in the operator's restart opt-in.
        if status == SessionStatus.BROKEN and not force:
            raise BrokenStateError(
                f"session '{name}' is broken (PID alive but tmux unreachable).",
                entity_kind="session",
                entity_name=name,
                hint="Use --force to restart.",
            )
        if status == SessionStatus.OK and not yes and not output.confirm(
            f"Session '{name}' is running. Restart?"
        ):
            raise UserAbort("restart cancelled")

        # Eager-prompting orchestration: resolve every secret referenced
        # by this session's env chain BEFORE any kill / destructive step.
        # Non-interactive failures surface as SecretUnavailableError with
        # no partial state to clean up. This is the recorded
        # bail-before-prompt exception to the one-boundary-resolve
        # contract: the graph's union (the site's config secrets)
        # resolved at the preflight boundary above, but the env chain
        # deliberately resolves HERE (after the BROKEN/--force refusal
        # and the "Restart?" confirm) so a declined restart never
        # prompts for secrets it was about to discard. Folding it into
        # the boundary would trade that operator protection for one
        # fewer prompt session on proxmox only.

        from agentworks.secrets import resolve_for_command

        secret_values = resolve_for_command(
            [
                _session_secret_target(
                    registry,
                    db=db,
                    vm=vm,
                    ws=ws,
                    session_name=name,
                    session_template=template,
                    mode=SessionMode(session.mode),
                    agent_name=session.agent_name,
                ),
            ],
            config,
            registry,
        )

        output.info(f"Restarting session '{name}'...")

        if is_legacy:
            # Surgical kill of the named session on the default tmux
            # server (no socket path). ``session.pid`` identifies the
            # SERVER for legacy admin rows, not this session, so the
            # BROKEN path's ``force_kill_tmux_server(pid)`` would nuke
            # every other tmux session sharing the server -- including
            # ad-hoc tmux work and other legacy Agentworks rows. The
            # ``kill-session -t <name>`` primitive is surgical. Failure
            # is best-effort: if the session is already gone (only the
            # DB row survived), kill returns False and we proceed to
            # create the new shape.
            _kill_session(name, run_command=session_run_command, socket_path=None)
        elif status == SessionStatus.BROKEN:
            from agentworks.sessions.tmux import force_kill_tmux_server

            output.warn(f"Session '{name}' is broken (tmux unreachable), force-killing via PID")
            assert session.pid is not None
            killed = force_kill_tmux_server(
                session.pid,
                target=session_target,
                socket_path=session.socket_path,
                log=output.detail,
                use_sudo=kill_sudo,
            )
            if not killed:
                raise ExternalError(
                    f"failed to kill PID {session.pid} for session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                )
        elif status == SessionStatus.OK:
            # Confirm already happened above (before eager-resolve), so we
            # know the operator opted in.
            sock = session.socket_path
            if not _kill_session(name, run_command=session_run_command, socket_path=sock):
                raise ExternalError(
                    f"failed to stop session '{name}' for restart",
                    entity_kind="session",
                    entity_name=name,
                )

        deploy_restricted_config(run_command, history_limit=config.session.history_limit)

        # Use restart_command if available, otherwise fall back to command
        command = _build_session_command(
            template,
            session_name=name,
            workspace_name=session.workspace_name,
            restart=True,
        )
        linux_user = _resolve_session_linux_user(db, session, vm)
        session_env = _resolve_session_env(
            registry,
            values=secret_values,
            db=db,
            vm=vm,
            ws=ws,
            session_name=name,
            session_template=template,
            mode=SessionMode(session.mode),
            agent_name=session.agent_name,
            linux_user=linux_user,
        )

        try:
            new_sock, pid = create_tmux_session(
                name,
                ws.workspace_path,
                command,
                linux_user,
                run_command=session_run_command,
                target=admin_target,
                admin_username=vm.admin_username,
                is_admin=is_admin,
                env=session_env,
            )
        except RuntimeError as exc:
            if "already has an active tmux server" in str(exc):
                raise StateError(
                    f"session '{name}' has an active tmux server that was not detected by the status check.",
                    entity_kind="session",
                    entity_name=name,
                    hint="Use 'session stop --force' to kill it, then retry.",
                ) from exc
            raise

        # Persist socket path if it differs from what's stored.
        if new_sock != session.socket_path:
            db.update_session_socket_path(name, new_sock)
        if pid is not None:
            # boot_id is /proc/sys/kernel/random/boot_id (world-readable);
            # admin's target is fine and convenient.
            boot_id = _get_boot_id(admin_target)
            if boot_id is not None:
                db.update_session_pid(name, pid, boot_id=boot_id)
            else:
                output.warn(f"Could not read boot ID for session '{name}', PID not stored")
        else:
            output.warn(f"Could not capture PID for session '{name}', will auto-repair on next access")

        output.info(f"Session '{name}' restarted")

        _regenerate_tmuxinator(db, config, vm, ws)
        # Don't re-add the session to the legacy vm-console here. The existing
        # window's wrapper polls the session's socket indefinitely and re-attaches
        # when the new tmux server comes back. Adding a new window here would
        # create a duplicate.


def stop_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    admin_only: bool = False,
    force: bool = False,
) -> None:
    """Stop all running sessions, optionally filtered by VM, workspace, agent, and/or mode.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``agent_name``
    and ``admin_only`` are mutually exclusive; the caller enforces
    the mutex.
    """
    sessions = filter_sessions(
        db,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        admin_only=admin_only,
    )

    # Resolve distinct VMs from the filtered session set and open the
    # batch boundary + per-VM gates BEFORE the SSH probes. The probes
    # (ensure_pids_batch, batch_check_all_sessions) issue per-VM
    # round-trips; on WSL2 they would race the idle timer without the
    # held-active anchor (a no-op hold on other platforms).
    distinct_vms = _distinct_vms_for_sessions(db, sessions)
    with _batch_vm_boundary(db, config, distinct_vms):
        # Auto-repair NULL-PID sessions, then batch check
        sessions = ensure_pids_batch(sessions, db=db, config=config)
        status_map = batch_check_all_sessions(sessions, db=db, config=config)

        # Error if any sessions are still unknown after auto-repair.
        # PID_STOPPED sessions are known-stopped (excluded from status_map by design).
        unknown = [
            s for s in sessions
            if s.pid != PID_STOPPED
            and (s.pid is None or s.boot_id is None or s.name not in status_map)
        ]
        if unknown:
            names = ", ".join(s.name for s in unknown)
            raise StateError(
                f"{len(unknown)} session(s) have unknown status after auto-repair ({names}).",
                hint="Resolve the listed sessions manually before retrying.",
            )

        broken = [s for s in sessions if status_map.get(s.name) == SessionStatus.BROKEN]
        if broken and not force:
            names = ", ".join(s.name for s in broken)
            output.warn(f"Skipping {len(broken)} broken session(s) ({names}). Use --force to kill.")

        ok_statuses = {SessionStatus.OK}
        if force:
            ok_statuses.add(SessionStatus.BROKEN)
        alive_sessions = [s for s in sessions if status_map.get(s.name) in ok_statuses]

        if not alive_sessions:
            output.info("No running sessions to stop.")
            return

        output.info(f"Stopping {len(alive_sessions)} session(s)...")

        # Resolve VM targets (reuse across sessions on the same VM)
        vm_targets: dict[str, Transport] = {}
        for s in alive_sessions:
            ws = db.get_workspace(s.workspace_name)
            if ws and ws.vm_name not in vm_targets:
                vm = db.get_vm(ws.vm_name)
                if vm and vm.tailscale_host:
                    vm_targets[ws.vm_name] = transport(vm, config)

        # Build (session, target, target_owns_session) tuples for _execute_stop.
        # Batch ops keep admin's target across all sessions for efficiency
        # (carve-out): admin's path into agent tmux servers requires
        # sudo. target_owns_session is True only for admin's own sessions.
        stop_targets: list[tuple[SessionRow, Transport, bool]] = []
        for s in alive_sessions:
            ws = db.get_workspace(s.workspace_name)
            if ws and ws.vm_name in vm_targets:
                target_owns_session = s.mode == SessionMode.ADMIN.value
                stop_targets.append((s, vm_targets[ws.vm_name], target_owns_session))

        failed = _execute_stop(stop_targets, db=db, force=force)
        if failed:
            raise ExternalError(f"{len(failed)} session(s) failed to stop.")


def restart_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    admin_only: bool = False,
    include_running: bool = False,
    force: bool = False,
) -> None:
    """Restart sessions, optionally filtered by VM, workspace, agent, and/or mode.

    With include_running=False (--all-stopped), only stopped sessions are
    restarted. With include_running=True (--all), all sessions are targeted;
    if any are running, the caller should have prompted or passed yes=True.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``agent_name``
    and ``admin_only`` are mutually exclusive; the caller enforces
    the mutex.
    """
    sessions = filter_sessions(
        db,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        admin_only=admin_only,
    )

    # Resolve distinct VMs from the filtered set and anchor them BEFORE the
    # SSH probes. Each restart_session call also opens its own gate span;
    # the redundant inner gate is a no-op on already-active VMs and a cheap
    # extra subprocess on WSL2 (accepted, see PR description).
    distinct_vms = _distinct_vms_for_sessions(db, sessions)

    failed: list[tuple[str, str]] = []
    with _batch_vm_boundary(db, config, distinct_vms):
        # Auto-repair NULL-PID sessions, then batch check
        sessions = ensure_pids_batch(sessions, db=db, config=config)
        status_map = batch_check_all_sessions(sessions, db=db, config=config)

        # Error if any sessions are still unknown after auto-repair.
        # PID_STOPPED sessions are known-stopped (excluded from status_map by design).
        # Legacy sessions (``socket_path is None``) are also excluded from
        # status_map by ``batch_check_status``; restart_session migrates them
        # to the new model, so don't treat them as "unknown" here.
        unknown = [
            s for s in sessions
            if s.pid != PID_STOPPED
            and s.socket_path is not None
            and (s.pid is None or s.boot_id is None or s.name not in status_map)
        ]
        if unknown:
            names = ", ".join(s.name for s in unknown)
            raise StateError(
                f"{len(unknown)} session(s) have unknown status after auto-repair ({names}).",
                hint="Resolve the listed sessions manually before retrying.",
            )

        if not include_running:
            # Only stopped sessions. Legacy sessions are alive-ish (PID set,
            # socket_path None) -- we can't tell whether they're stopped
            # from status_map alone (batch_check_status skips them), so we
            # filter them out under ``--all-stopped`` and tell the operator
            # how to migrate (``--all``). The batch_check_status warning
            # already named them; this second message ties that warning to
            # an actionable next step from the command they just ran.
            legacy_skipped = [
                s.name
                for s in sessions
                if s.socket_path is None
                and s.pid is not None
                and s.pid > 0
            ]
            if legacy_skipped:
                names = ", ".join(legacy_skipped)
                output.warn(
                    f"Skipping {len(legacy_skipped)} legacy session(s) under "
                    f"--all-stopped (can't determine state without a per-session "
                    f"socket). Use `--all` to migrate them: {names}"
                )
            sessions = [
                s
                for s in sessions
                if s.pid == PID_STOPPED
                or status_map.get(s.name) == SessionStatus.STOPPED
            ]

        if not sessions:
            output.info("No matching sessions to restart.")
            return

        output.info(f"Restarting {len(sessions)} session(s)...")

        for session in sessions:
            try:
                restart_session(db, config, name=session.name, force=force, yes=include_running)
            except UserAbort:
                # A confirm-cancellation aborts the whole batch operation, not
                # just this one session. Propagate so the outer wrapper renders
                # "Aborted." once and exits.
                raise
            except BrokenStateError as exc:
                if not force:
                    output.warn(f"Skipping '{session.name}': {exc}")
                else:
                    failed.append((session.name, str(exc)))
                    output.warn(f"Error restarting '{session.name}': {exc}")
            except StateError as exc:
                failed.append((session.name, str(exc)))
                output.warn(f"Error restarting '{session.name}': {exc}")
            except Exception as exc:
                failed.append((session.name, str(exc)))
                output.warn(f"Error restarting '{session.name}': {exc}")

    if failed:
        raise ExternalError(f"{len(failed)} session(s) failed to restart.")


def delete_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Delete a session. Prompts if running/unknown (--yes to skip). --force for BROKEN."""
    session = _require_session(db, name)
    with _prepare_vm(db, config, session, operation="session-delete") as (
        ws,
        vm,
        _run_command,
        _run_as_root,
        admin_target,
    ):
        session = _ensure_pid(session, target=admin_target, db=db)
        status = check_session_status(session, target=admin_target)

        # UNKNOWN is impossible here -- _ensure_pid raises on unresolvable sessions
        if status == SessionStatus.BROKEN and not force:
            raise BrokenStateError(
                f"session '{name}' is broken (PID alive but tmux unreachable).",
                entity_kind="session",
                entity_name=name,
                hint="Use --force to delete.",
            )

        # Pick the destructive-op transport BEFORE prompting the operator.
        # For agent sessions, ``_build_session_target`` probes direct agent
        # SSH; a pre-rollout agent surfaces here as an
        # actionable error rather than after the operator has already
        # confirmed the delete. The helper returns a same-uid target, so
        # no sudo is needed for the destructive ops below.
        session_target = _build_session_target(
            session, vm=vm, config=config, db=db, admin_target=admin_target
        )
        session_run_command: RunCommand = session_target.run
        kill_sudo = False

        # Confirm before any destructive action
        if not yes and not output.confirm(f"Delete session '{name}'?"):
            raise UserAbort("delete cancelled")

        # Now kill if needed
        if status == SessionStatus.OK:
            sock = session.socket_path
            if not _kill_session(name, run_command=session_run_command, socket_path=sock):
                # Race: session may have exited between check and kill. Recheck.
                recheck = check_session_status(session, target=admin_target)
                if recheck != SessionStatus.STOPPED:
                    raise ExternalError(
                        f"failed to stop session '{name}' for deletion",
                        entity_kind="session",
                        entity_name=name,
                    )
        elif status == SessionStatus.BROKEN:
            from agentworks.sessions.tmux import force_kill_tmux_server

            output.warn(f"Session '{name}' is broken (tmux unreachable), force-killing via PID")
            assert session.pid is not None
            killed = force_kill_tmux_server(
                session.pid,
                target=session_target,
                socket_path=session.socket_path,
                log=output.detail,
                use_sudo=kill_sudo,
            )
            if not killed:
                raise ExternalError(
                    f"failed to kill PID {session.pid} for session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                )

        # Clean up socket if the server is dead (don't remove a live socket)
        sock = session.socket_path
        if sock and sock.startswith(AGENT_SOCKET_ROOT + "/"):
            post_status = check_session_status(session, target=admin_target)
            if post_status == SessionStatus.STOPPED:
                session_target.run(f"rm -f {shlex.quote(sock)}", sudo=kill_sudo, check=False)
            else:
                output.warn(f"Session '{name}' status is {post_status.value} after delete, socket preserved at {sock}")

        # Capture console memberships before delete; the FK cascade on
        # console_sessions zeroes the join table the moment the session row goes.
        member_consoles = [c.name for c in db.list_consoles_for_session(name)]

        db.delete_session(name)

        # Clean up implicit grant for this session
        if session.agent_name:
            db.delete_agent_grant(session.agent_name, session.workspace_name, "implicit", session_name=name)
            # If no grants remain, remove from workspace group
            if not db.has_any_grant(session.agent_name, session.workspace_name):
                from agentworks.agents.grants import remove_from_workspace_group

                agent = db.get_agent(session.agent_name)
                if agent:
                    remove_from_workspace_group(vm, config, db, agent.linux_user, session.workspace_name)

        _regenerate_tmuxinator(db, config, vm, ws)

        # Best-effort console cleanup runs after all DB / tmuxinator state has
        # settled. Stale tmux windows are recoverable cosmetic noise; if the
        # helper raises AgentworksError we skip the success message and any
        # created_workspace / created_agent cleanup below -- those would re-use
        # the same broken transport and just compound errors.
        if member_consoles:
            from agentworks.sessions.multi_console import kill_session_windows

            # Consoles are admin-owned (carve-out): admin manages
            # admin's tmux server. Use admin_target regardless of session mode.
            kill_session_windows(
                admin_target, pairs=[(c, name) for c in member_consoles]
            )

        output.info(f"Session '{name}' deleted")

        # If this session created its workspace, offer to delete it
        if session.created_workspace:
            remaining = db.list_sessions(workspace_name=session.workspace_name)
            if remaining:
                output.detail(
                    f"Workspace '{session.workspace_name}' was created with this session but has "
                    f"{len(remaining)} other session(s), not offering to delete."
                )
            elif not yes:
                if output.confirm(
                    f"Workspace '{session.workspace_name}' was created with this session "
                    f"and has no other sessions. Delete it?",
                ):
                    from agentworks.workspaces.manager import delete_workspace

                    delete_workspace(db, config, session.workspace_name, yes=True)
            else:
                from agentworks.workspaces.manager import delete_workspace

                output.detail(f"Deleting workspace '{session.workspace_name}' (created with this session)...")
                delete_workspace(db, config, session.workspace_name, yes=True)

        # If this session created its agent, offer to delete it unless the agent
        # is still in use elsewhere (other sessions on the agent, or any explicit
        # workspace grants). Implicit grants are tied to sessions and were cleaned
        # up above, so they don't count.
        if session.created_agent and session.agent_name:
            other_sessions = [s for s in db.list_sessions() if s.agent_name == session.agent_name]
            explicit_grants = [
                ws
                for (ws, has_explicit, _) in db.list_granted_workspaces_with_types(session.agent_name)
                if has_explicit
            ]
            if other_sessions or explicit_grants:
                reasons: list[str] = []
                if other_sessions:
                    reasons.append(f"{len(other_sessions)} other session(s)")
                if explicit_grants:
                    reasons.append(f"{len(explicit_grants)} explicit grant(s)")
                output.detail(
                    f"Agent '{session.agent_name}' was created with this session but still has "
                    f"{' and '.join(reasons)}, not offering to delete."
                )
            elif not yes:
                if output.confirm(
                    f"Agent '{session.agent_name}' was created with this session "
                    f"and is not in use elsewhere. Delete it?",
                ):
                    from agentworks.agents.manager import delete_agent

                    delete_agent(db, config, name=session.agent_name, yes=True)
            else:
                from agentworks.agents.manager import delete_agent

                output.detail(f"Deleting agent '{session.agent_name}' (created with this session)...")
                delete_agent(db, config, name=session.agent_name, yes=True)


def describe_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Show session details.

    Runs inside ``_prepare_vm``'s gate span: a hold the imperative
    body did not take (it gated and discarded the platform). The
    superset is a no-op everywhere but WSL2, where it anchors the
    status probes against the idle timer.
    """
    session = _require_session(db, name)
    with _prepare_vm(db, config, session, operation=None) as (
        _ws,
        vm,
        _run_command,
        _run_as_root,
        target,
    ):
        session = _ensure_pid(session, target=target, db=db)

        status = check_session_status(session, target=target)

        # Build status label with PID if running and current boot
        if status == SessionStatus.OK and session.pid and session.pid > 0:
            status_label = f"running (PID {session.pid})"
        elif status == SessionStatus.BROKEN and session.pid and session.pid > 0:
            status_label = f"broken (PID {session.pid} alive, tmux unreachable)"
        else:
            status_label = {
                SessionStatus.OK: "running",
                SessionStatus.STOPPED: "stopped",
                SessionStatus.BROKEN: "broken",
                SessionStatus.UNKNOWN: "unknown",
            }[status]

        mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"

        output.info(f"Name:       {session.name}")
        output.info(f"Workspace:  {session.workspace_name}")
        output.info(f"VM:         {vm.name}")
        output.info(f"Template:   {session.template}")
        output.info(f"Mode:       {mode_label}")
        output.info(f"Status:     {status_label}")
        output.info(f"Created:    {session.created_at}")
        output.info(f"Updated:    {session.updated_at}")


def batch_check_all_sessions(
    sessions: list[SessionRow],
    *,
    db: Database,
    config: Config,
) -> dict[str, SessionStatus]:
    """Batch status check grouped by VM, parallel across VMs (capped at 8).

    Returns {session_name: SessionStatus}. Sessions with no reachable VM or
    pid=None/PID_STOPPED are excluded from the result.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Resolve each session's VM and group
    by_vm: dict[str, list[SessionRow]] = {}
    vm_targets: dict[str, Transport] = {}

    for s in sessions:
        ws = db.get_workspace(s.workspace_name)
        if not ws:
            continue
        if ws.vm_name not in vm_targets:
            vm = db.get_vm(ws.vm_name)
            if not vm or not vm.tailscale_host:
                continue
            vm_targets[ws.vm_name] = transport(vm, config)
        by_vm.setdefault(ws.vm_name, []).append(s)

    if not by_vm:
        return {}

    result_map: dict[str, SessionStatus] = {}

    def _check_vm(vm_name: str) -> dict[str, SessionStatus]:
        return batch_check_status(by_vm[vm_name], target=vm_targets[vm_name])

    with ThreadPoolExecutor(max_workers=min(8, len(by_vm))) as executor:
        futures = {executor.submit(_check_vm, name): name for name in by_vm}
        for future in as_completed(futures):
            vm_name = futures[future]
            try:
                result_map.update(future.result())
            except Exception as exc:
                output.warn(f"Failed to check sessions on VM '{vm_name}': {exc}")

    return result_map


def list_sessions(
    db: Database,
    config: Config,
    *,
    workspace_name: str | list[str] | None = None,
    vm_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    admin_only: bool = False,
    no_status: bool = False,
    names_only: bool = False,
) -> None:
    """List sessions with batched status checks (one SSH call per VM, parallel).

    Status resolution is has-session-first; PID/boot_id are only used as a
    follow-up when agent checks fail.

    With ``names_only=True``, emit one session name per line and
    skip both the SSH status batch and the table render. Used by
    shell completion (see issue #147); the order matches the table's
    workspace-grouped order so completion stays stable.
    """
    sessions = filter_sessions(
        db,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        admin_only=admin_only,
    )

    if names_only:
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No sessions found" line below is
        # for human readers only. Match the table's workspace-grouped
        # order so completion stays stable across renderers.
        names_by_ws: dict[str, list[SessionRow]] = {}
        for session in sessions:
            names_by_ws.setdefault(session.workspace_name, []).append(session)
        for ws_name in sorted(names_by_ws):
            for session in names_by_ws[ws_name]:
                output.info(session.name)
        return

    if not sessions:
        output.info("No sessions found.")
        return

    # Auto-repair sessions with missing PIDs, then batch check.
    # The status path SSHes to every involved VM; anchor each one (no-op
    # on non-WSL2) so the probe doesn't lose them mid-check.
    status_keepalive_vms: list[VMRow] = (
        [] if no_status else _distinct_vms_for_sessions(db, sessions)
    )

    status_map: dict[str, SessionStatus] = {}
    with _batch_vm_boundary(db, config, status_keepalive_vms):
        if not no_status:
            sessions = ensure_pids_batch(sessions, db=db, config=config)
            status_map = batch_check_all_sessions(sessions, db=db, config=config)

    # Build table rows grouped by workspace
    by_workspace: dict[str, list[SessionRow]] = {}
    for session in sessions:
        by_workspace.setdefault(session.workspace_name, []).append(session)

    rows: list[tuple[str, str, str, str, str, str]] = []
    for ws_name, ws_sessions in sorted(by_workspace.items()):
        ws = db.get_workspace(ws_name)
        vm_name = ws.vm_name if ws else "-"

        for session in ws_sessions:
            if no_status:
                status = "-"
            elif session.pid == PID_STOPPED:
                status = "stopped"
            elif session.pid is None or session.boot_id is None:
                status = "unknown"
            elif session.name in status_map:
                s_status = status_map[session.name]
                status = {
                    SessionStatus.OK: "running",
                    SessionStatus.STOPPED: "stopped",
                    SessionStatus.BROKEN: "broken",
                    SessionStatus.UNKNOWN: "unknown",
                }[s_status]
            else:
                # No status available (VM unreachable or SSH failure during batch check)
                status = "-"
            mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"
            rows.append((session.name, ws_name, vm_name, session.template, mode_label, status))

    if not rows:
        output.info("No sessions found.")
        return

    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    ws_w = max(len("WORKSPACE"), max(len(r[1]) for r in rows))
    vm_w = max(len("VM"), max(len(r[2]) for r in rows))
    tpl_w = max(len("TEMPLATE"), max(len(r[3]) for r in rows))
    mode_w = max(len("MODE"), max(len(r[4]) for r in rows))

    header = (
        f"{'NAME':<{name_w}}  {'WORKSPACE':<{ws_w}}  {'VM':<{vm_w}}  {'TEMPLATE':<{tpl_w}}  {'MODE':<{mode_w}}  STATUS"
    )
    output.info(header)
    output.info("-" * len(header))
    broken_names = []
    unknown_names = []
    for sname, ws_name, vm_col, tpl, mode, status in rows:
        output.info(
            f"{sname:<{name_w}}  {ws_name:<{ws_w}}  {vm_col:<{vm_w}}  {tpl:<{tpl_w}}  {mode:<{mode_w}}  {status}"
        )
        if status == "broken":
            broken_names.append(sname)
        elif status == "unknown":
            unknown_names.append(sname)

    if broken_names or unknown_names:
        output.info("")
        if broken_names:
            output.warn(
                f"{len(broken_names)} session(s) are broken (tmux unreachable): "
                f"{', '.join(broken_names)}. Use restart/stop/delete --force."
            )
        if unknown_names:
            output.warn(
                f"{len(unknown_names)} session(s) have unknown status: "
                f"{', '.join(unknown_names)}. Status could not be determined."
            )


def attach_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> int:
    """Attach to a session's tmux session (interactive).

    Returns the interactive session's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service),
    mirroring :func:`agentworks.vms.manager.exec_vm`.
    """
    from agentworks.sessions.tmux import tmux_cmd

    session = _require_session(db, name)
    with _prepare_vm(db, config, session, operation="session-attach") as (
        _ws,
        _vm,
        _run_command,
        _run_as_root,
        target,
    ):
        session = _ensure_pid(session, target=target, db=db)
        status = check_session_status(session, target=target)

        if status == SessionStatus.STOPPED:
            raise StateError(
                f"session '{name}' is not running",
                entity_kind="session",
                entity_name=name,
            )
        if status == SessionStatus.BROKEN:
            raise BrokenStateError(
                f"session '{name}' is broken (PID alive but tmux unreachable).",
                entity_kind="session",
                entity_name=name,
            )

        q_session = shlex.quote(name)
        return target.interactive(tmux_cmd(f"attach -t {q_session}", session.socket_path))


def session_logs(
    db: Database,
    config: Config,
    *,
    name: str,
    lines: int | None = None,
) -> None:
    """Dump the scrollback buffer for a session."""
    from agentworks.sessions.tmux import capture_output

    session = _require_session(db, name)
    with _prepare_vm(db, config, session, operation="session-logs") as (
        _ws,
        _vm,
        run_command,
        _run_as_root,
        target,
    ):
        session = _ensure_pid(session, target=target, db=db)
        status = check_session_status(session, target=target)

        if status == SessionStatus.STOPPED:
            raise StateError(
                f"session '{name}' is not running",
                entity_kind="session",
                entity_name=name,
            )
        if status == SessionStatus.BROKEN:
            raise BrokenStateError(
                f"session '{name}' is broken (PID alive but tmux unreachable).",
                entity_kind="session",
                entity_name=name,
            )

        sock = session.socket_path
        captured = capture_output(
            name,
            run_command=run_command,
            lines=lines or config.session.history_limit,
            socket_path=sock,
        )
        # Raw data pipe (opaque tmux capture-pane output), not a structured message.
        # Intentionally not routed through the output handler.
        typer.echo(captured, nl=False)


