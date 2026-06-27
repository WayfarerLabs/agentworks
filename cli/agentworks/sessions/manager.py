"""Session lifecycle orchestration."""

from __future__ import annotations

import contextlib
import re
import shlex
import sys
from dataclasses import dataclass
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
from agentworks.vms.manager import keep_vm_active, keep_vms_active

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Template variable substitution: {{var}} double-brace syntax.
_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
_KNOWN_TEMPLATE_VARS = {"session_name", "workspace_name"}

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.env import EnvEntry
    from agentworks.secrets import SecretTarget
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.sessions.tmux import RunCommand
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport


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
    if the agent's authorized_keys aren't provisioned (FRD R1 / Phase 3).
    For admin sessions, returns the admin target unchanged.

    Single-session paths use this to make kill / restart operations
    consistent with create: every destructive step on an agent session
    goes via direct agent SSH. Because the returned target always owns
    the session it will operate on, callers can issue destructive commands
    without sudo. Batch paths intentionally don't use this helper; they
    keep admin's target across all sessions and pass ``sudo=True`` to
    reach into agent tmux servers (FRD R1 carve-out for batch ops).
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


def _prepare_vm(
    db: Database, config: Config, workspace_name: str, *, operation: str | None = None
) -> tuple[WorkspaceRow, VMRow, RunCommand, RunCommand, Transport]:
    """Validate workspace/VM, ensure running, and return (ws, vm, run_command, run_as_root, target).

    If operation is set, creates an SSHLogger and attaches it to the Transport
    so all calls log automatically. run_command and run_as_root are bound from
    the target's methods for callers that consume RunCommand callables.
    """
    from agentworks.ssh import SSHLogger

    ws = _require_workspace(db, workspace_name)
    vm = _require_vm_for_workspace(db, ws)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm.name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=vm.name,
        )

    logger = SSHLogger(vm.name, operation) if operation else None
    target = transport(vm, config, logger=logger)
    run_command: RunCommand = target.run
    run_as_root: RunCommand = partial(target.run, sudo=True)
    return ws, vm, run_command, run_as_root, target


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
) -> list[SessionRow]:
    """Load sessions with optional workspace, VM, and/or agent filters.

    Each filter accepts a single name or a list of names; lists OR within
    a filter, filters AND across the call. See `Database.list_sessions`.
    """
    return db.list_sessions(
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
    )


def _distinct_vms_for_sessions(db: Database, sessions: list[SessionRow]) -> list[VMRow]:
    """Resolve the distinct set of VMs that host the given sessions.

    Used by the batch session operations (stop_all_sessions, restart_all_sessions,
    list_sessions) to feed `keep_vms_active` with exactly the VMs whose SSH
    transports will be touched. Order is insertion order keyed by VM name so
    keepalive entry messages render in a stable order.
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


def _resolve_template(config: Config, template_name: str | None) -> ResolvedSessionTemplate:
    """Resolve a session template by name, applying inheritance."""
    from agentworks.sessions.templates import resolve_template

    try:
        return resolve_template(config, template_name)
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
    config: Config,
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
    from agentworks.agents.templates import resolve_from_dict as _resolve_agent_template
    from agentworks.vms.templates import resolve_from_dict as _resolve_vm_template
    from agentworks.workspaces.templates import resolve_template as _resolve_ws_template

    vm_template = _resolve_vm_template(config.vm_templates, vm.template)
    workspace_template = _resolve_ws_template(config, ws.template)

    admin_env: dict[str, EnvEntry] | None
    agent_env: dict[str, EnvEntry] | None
    if mode == SessionMode.ADMIN:
        admin_env = config.admin.env
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
            config.agent_templates, agent_row.template
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
    config: Config,
    *,
    name: str,
    workspace_name: str,
    vm: VMRow,
    session_template: ResolvedSessionTemplate,
    new_workspace: NewWorkspaceArgs | None,
    existing_workspace: WorkspaceRow | None,
    new_agent: NewAgentArgs | None,
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
    from agentworks.secrets import SecretTarget
    from agentworks.vms.templates import resolve_from_dict as _resolve_vm_tmpl
    from agentworks.workspaces.templates import resolve_template as _resolve_ws_tmpl

    vm_template = _resolve_vm_tmpl(config.vm_templates, vm.template)

    if new_workspace is not None:
        workspace_env = _resolve_ws_tmpl(config, new_workspace.template_name).env
    else:
        assert existing_workspace is not None
        workspace_env = _resolve_ws_tmpl(config, existing_workspace.template).env

    agent_env: dict[str, EnvEntry] | None = None
    admin_scope: dict[str, EnvEntry] | None = None
    if is_admin_mode:
        admin_scope = config.admin.env
    elif new_agent is not None:
        agent_env = _resolve_agent_tmpl(config, new_agent.template_name).env
    elif existing_agent is not None:
        agent_env = _resolve_agent_tmpl(config, existing_agent.template).env

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
    config: Config,
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
    would consume; substitution invariance (Phase 6.1) guarantees the
    SecretDecl union is identical pre- vs post-substitution.
    """
    from agentworks.secrets import SecretTarget

    scopes = _resolve_session_env_scopes(
        config,
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
    config: Config,
    *,
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
    ``compose_env`` (which renders secrets via the config resolver and
    overlays per-context identity vars).
    """
    from agentworks.env import ResourceContext, compose_env

    scopes = _resolve_session_env_scopes(
        config,
        db=db,
        vm=vm,
        ws=ws,
        session_name=session_name,
        session_template=session_template,
        mode=mode,
        agent_name=agent_name,
    )

    ctx = ResourceContext(
        vm_name=vm.name,
        vm_host=vm.vm_host_name,
        platform=vm.platform,
        user=linux_user,
        workspace_name=ws.name,
        workspace_dir=ws.workspace_path,
        agent_name=agent_name,
        session_name=session_name,
        session_kind="admin" if mode == SessionMode.ADMIN else "agent",
    )

    return compose_env(
        resolver=config.secret_resolver,
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


def _assert_required_commands(
    run_command: RunCommand,
    template: ResolvedSessionTemplate,
    *,
    session_name: str,
    target_label: str,
) -> None:
    """Verify every command the template declares as required exists on the
    session's launch target, before any tmux work happens.

    A template lists the executables its command needs via ``required_commands``
    (e.g. the ``claude`` template requires ``claude``). Without this check, a
    missing binary surfaces only as a cryptic downstream failure: the pane
    command dies instantly, the fresh per-session tmux server exits, and the
    next ``server-access`` call fails against a now-dead socket (see
    ``sessions/tmux._grant_server_access``). Checking up front turns that into
    an actionable error with no partial state to roll back.

    Probes with ``$SHELL -lic 'command -v <cmd>'`` -- the same shell flags
    ``tmux._pane_command`` uses for the actual pane. Matters because PATH
    additions can live in any of the dotfiles those flags source:

    - ``-l`` (login): /etc/profile, ~/.profile, ~/.bash_profile -- where
      mise activation and the agentworks profile fragments live.
    - ``-i`` (interactive): ~/.bashrc, ~/.zshrc, and any user PATH addition
      guarded by ``[[ $- == *i* ]]`` or ``[ -n "$PS1" ]``.
    - ``-c``: run the probe and exit.

    The probe runs over the SSH command channel without a PTY, so shells
    may emit a "no job control in this shell" warning when started
    interactive. The warning lands on stderr and doesn't change the exit
    status; this call uses ``check=False`` so stderr is discarded.

    One residual gap: tools that gate PATH on ``[[ -t 0 ]]`` (real TTY
    check) won't be visible to the probe. Closing that would require
    requesting a PTY for the probe, which has its own side effects. PATH
    mutations gated on a real TTY are rare; leaving uncovered for now.
    """
    missing: list[str] = []
    for cmd in template.required_commands:
        inner = f"command -v {shlex.quote(cmd)} >/dev/null 2>&1"
        probe = run_command(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
        if not getattr(probe, "ok", False):
            missing.append(cmd)
    if not missing:
        return

    joined = ", ".join(repr(c) for c in missing)
    verb = "is" if len(missing) == 1 else "are"
    raise StateError(
        f"template '{template.name}' requires {joined}, which {verb} not "
        f"installed or not on PATH for {target_label}.",
        entity_kind="session",
        entity_name=session_name,
        hint=(
            f"Install the missing command(s) on {target_label}, or create the "
            "session with a different template (--template)."
        ),
    )


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


# -- Public API ------------------------------------------------------------


@dataclass(frozen=True)
class NewWorkspaceArgs:
    """Inputs for creating a workspace as part of ``create_session``.

    Bundled in a dataclass (rather than scattered as sibling kwargs) so the
    "create new" intent is one toggle and the related arguments travel
    together. ``workspace_name`` on ``create_session`` holds the resulting
    workspace's name; this struct carries the template choice.
    """

    template_name: str | None = None


@dataclass(frozen=True)
class NewAgentArgs:
    """Inputs for creating an agent as part of ``create_session``.

    Bundled in a dataclass for the same reason as ``NewWorkspaceArgs``.
    ``agent_name`` on ``create_session`` holds the resulting agent's name;
    this struct carries the template choice.
    """

    template_name: str | None = None


def create_session(
    db: Database,
    config: Config,
    *,
    name: str,
    template_name: str | None = None,
    workspace_name: str | None = None,
    new_workspace: NewWorkspaceArgs | None = None,
    agent_name: str | None = None,
    new_agent: NewAgentArgs | None = None,
    vm_name: str | None = None,
) -> None:
    """Create and start a session.

    Optionally provisions an ephemeral workspace and/or agent as part of
    the same call. When ``new_workspace`` / ``new_agent`` are set, the
    full flow (validate → resolve secrets → create resources → create
    session) runs atomically: validation failures and Ctrl-C at the
    secret prompt leave no state mutated, and any failure after
    state mutation begins rolls back every ephemeral resource that
    was created.

    Args:
        name: Session name.
        template_name: Session template (defaults to the operator's default).
        workspace_name: Name of the workspace this session lives in. When
            ``new_workspace`` is None, must reference an existing workspace.
            When ``new_workspace`` is set, names the workspace to create
            (defaults to ``name``).
        new_workspace: When set, create a new workspace before the session.
        agent_name: Existing agent for agent-mode sessions. When
            ``new_agent`` is set, names the agent to create (defaults to
            ``name``). When both ``agent_name`` and ``new_agent`` are None,
            the session runs in admin mode.
        new_agent: When set, create a new agent before the session.
        vm_name: Target VM. Optional when an existing workspace or agent
            pins the VM; required when no other anchor does
            (e.g. ``new_workspace`` + admin mode, or ``new_workspace`` +
            ``new_agent``). When specified alongside other anchors, must
            agree with them, else ``ValidationError``.
    """
    from agentworks.config import validate_name
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    # ===== Pure validation (no SSH, no mutations) ===========================

    if agent_name is not None and new_agent is not None:
        raise ValidationError(
            "agent_name and new_agent are mutually exclusive",
            entity_kind="session",
            entity_name=name,
        )
    if workspace_name is None and new_workspace is None:
        raise ValidationError(
            "must specify workspace_name or new_workspace",
            entity_kind="session",
            entity_name=name,
        )

    # Default ephemeral resource names to the session name when omitted.
    # After this block, ``workspace_name`` is always set; ``agent_name``
    # is None only in admin mode.
    if new_workspace is not None and workspace_name is None:
        workspace_name = name
    if new_agent is not None and agent_name is None:
        agent_name = name
    assert workspace_name is not None  # invariant after the defaulting above

    validate_name(name)
    if new_workspace is not None:
        validate_name(workspace_name)
    if new_agent is not None:
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
    if new_workspace is not None and db.get_workspace(workspace_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{workspace_name}' already exists",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    if new_agent is not None:
        assert agent_name is not None  # defaulted to ``name`` above
        if db.get_agent(agent_name) is not None:
            raise AlreadyExistsError(
                f"agent '{agent_name}' already exists",
                entity_kind="agent",
                entity_name=agent_name,
            )

    # VM anchor resolution. Each specified resource pins a candidate VM;
    # they must all agree. By the end of this block we have one VMRow.
    vm_anchors: list[tuple[str, str]] = []  # (source_label, vm_name)
    if vm_name is not None:
        vm_anchors.append(("vm_name", vm_name))

    existing_ws: WorkspaceRow | None = None
    if new_workspace is None:
        existing_ws = db.get_workspace(workspace_name)
        if existing_ws is None:
            raise NotFoundError(
                f"workspace '{workspace_name}' not found",
                entity_kind="workspace",
                entity_name=workspace_name,
            )
        vm_anchors.append((f"workspace '{workspace_name}'", existing_ws.vm_name))

    existing_agent: AgentRow | None = None
    if new_agent is None and agent_name is not None:
        existing_agent = db.get_agent(agent_name)
        if existing_agent is None:
            raise NotFoundError(
                f"agent '{agent_name}' not found",
                entity_kind="agent",
                entity_name=agent_name,
            )
        vm_anchors.append((f"agent '{agent_name}'", existing_agent.vm_name))

    if not vm_anchors:
        # No existing resources and no explicit vm_name: nothing pins the VM.
        # Happens for ``new_workspace + admin`` or ``new_workspace + new_agent``
        # without ``vm_name``.
        raise ValidationError(
            "VM anchor required: pass vm_name, or reference an existing workspace or agent",
            entity_kind="session",
            entity_name=name,
        )
    target_vm_name = vm_anchors[0][1]
    if any(candidate != target_vm_name for _, candidate in vm_anchors):
        detail = ", ".join(f"{src}={vm}" for src, vm in vm_anchors)
        raise ValidationError(
            f"VM mismatch: {detail}",
            entity_kind="session",
            entity_name=name,
        )
    vm = db.get_vm(target_vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{target_vm_name}' not found",
            entity_kind="vm",
            entity_name=target_vm_name,
        )

    # ===== Template resolution (no SSH, no mutations) =======================

    template = _resolve_template(config, template_name)

    # ===== Ensure VM running + Tailscale reachable (SSH, no mutations) ======

    from agentworks.workspaces.manager import _ensure_vm_running as _ensure_vm_up

    _ensure_vm_up(db, config, vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm.name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=vm.name,
        )

    # ===== Eager-resolve secrets (single call, before any state mutation) ===

    from agentworks.secrets import resolve_for_command

    resolve_for_command(
        [
            _session_secret_target_pre_create(
                config,
                name=name,
                workspace_name=workspace_name,
                vm=vm,
                session_template=template,
                new_workspace=new_workspace,
                existing_workspace=existing_ws,
                new_agent=new_agent,
                existing_agent=existing_agent,
                is_admin_mode=(agent_name is None),
            ),
        ],
        config,
    )
    # If we reach here, every secret prompt is done and the resolver cache
    # is warm. The downstream create_workspace / create_agent / inner
    # session-internal block will not re-prompt the operator.

    # ===== Atomic state mutations with rollback =============================

    workspace_created = False
    agent_created = False

    def _rollback_ephemerals() -> None:
        """Undo ephemeral resource creates on later failure. Order is
        reverse-of-create (agent before workspace) so the agent's
        workspace-group membership is cleaned up before the group
        itself goes away. Each step wrapped so a rollback failure
        doesn't mask the original exception."""
        if agent_created:
            assert agent_name is not None  # set when new_agent is not None
            try:
                from agentworks.agents.manager import delete_agent

                delete_agent(db, config, name=agent_name, force=True, yes=True)
            except Exception as e:
                output.warn(
                    f"rollback: failed to delete ephemeral agent '{agent_name}': {e}"
                )
        if workspace_created:
            try:
                from agentworks.workspaces.manager import delete_workspace

                delete_workspace(db, config, workspace_name, force=True, yes=True)
            except Exception as e:
                output.warn(
                    f"rollback: failed to delete ephemeral workspace '{workspace_name}': {e}"
                )

    try:
        # ---- Ephemeral creates -------------------------------------------------
        if new_workspace is not None:
            from agentworks.workspaces.manager import create_workspace

            create_workspace(
                db,
                config,
                name=workspace_name,
                vm_name=vm.name,
                template_name=new_workspace.template_name,
            )
            workspace_created = True
        if new_agent is not None:
            assert agent_name is not None  # defaulted to ``name`` above
            from agentworks.agents.manager import create_agent

            create_agent(
                db,
                config,
                name=agent_name,
                vm_name=vm.name,
                template=new_agent.template_name,
            )
            agent_created = True

        # ---- Session-internal mutations ---------------------------------------
        ws, vm_check, run_command, run_as_root, target = _prepare_vm(
            db, config, workspace_name, operation="session-create"
        )
        with keep_vm_active(db, config, vm_check):
            # Resolve mode and linux user (no side effects; safe outside the try).
            resolved_agent_name: str | None = None
            agent_target = None
            if agent_name is not None:
                mode = SessionMode.AGENT
                agent = db.get_agent(agent_name)
                if agent is None:
                    raise NotFoundError(
                        f"agent '{agent_name}' not found",
                        entity_kind="agent",
                        entity_name=agent_name,
                    )
                # Cross-VM defense-in-depth (already validated upfront for
                # existing agents; trivially true for fresh ephemeral agents).
                if agent.vm_name != vm_check.name:
                    raise ValidationError(
                        f"agent '{agent_name}' is on VM '{agent.vm_name}', "
                        f"but workspace '{workspace_name}' is on VM '{vm_check.name}'",
                        entity_kind="session",
                        entity_name=name,
                    )
                linux_user = agent.linux_user
                resolved_agent_name = agent_name

                # Probe direct agent SSH BEFORE any state mutation (group add,
                # DB inserts, restricted-config write). A pre-rollout agent
                # surfaces here as an actionable StateError; without this,
                # the rollback path would unwind the mutations but the
                # operator's view would just see "session create failed".
                from agentworks.agents.manager import _assert_agent_ssh_works
                from agentworks.transports import agent_transport

                agent_target = agent_transport(vm_check, config, agent)
                _assert_agent_ssh_works(agent_target, agent)
            else:
                mode = SessionMode.ADMIN
                linux_user = vm_check.admin_username

            # Pre-flight: verify the template's required commands exist on the
            # launch target BEFORE any state mutation. A missing binary otherwise
            # only surfaces as a cryptic tmux server-access failure downstream
            # (see _assert_required_commands). For agent mode the probe runs over
            # the agent's own SSH (already proven by _assert_agent_ssh_works);
            # for admin mode over the admin connection.
            if mode == SessionMode.AGENT:
                assert agent_target is not None  # set in the agent_name branch above
                _assert_required_commands(
                    agent_target.run,
                    template,
                    session_name=name,
                    target_label=f"agent '{resolved_agent_name}'",
                )
            else:
                _assert_required_commands(
                    run_command,
                    template,
                    session_name=name,
                    target_label=f"VM '{vm_check.name}'",
                )

            # Compute socket path up front (deterministic from linux_user + session name).
            # Needed for the DB insert since the CHECK constraint requires agent sessions
            # to have a socket_path.
            expected_socket: str | None = None
            if mode == SessionMode.AGENT:
                from agentworks.sessions.tmux import agent_socket_path

                expected_socket = agent_socket_path(linux_user, name)

            def _rollback() -> None:
                # Best-effort rollback for the session-internal mutations only;
                # ephemeral resources (workspace / agent created above) are
                # unwound by the outer ``_rollback_ephemerals``. Each step
                # wrapped so a cleanup failure surfaces as a warning instead of
                # masking the original exception.
                try:
                    db.delete_session(name)
                except Exception as e:
                    output.warn(f"rollback: failed to delete session row '{name}': {e}")
                if not resolved_agent_name:
                    return
                try:
                    db.delete_agent_grant(
                        resolved_agent_name, workspace_name, "implicit", session_name=name
                    )
                    remaining = db.has_any_grant(resolved_agent_name, workspace_name)
                except Exception as e:
                    output.warn(
                        f"rollback: failed to revoke implicit grant for agent "
                        f"'{resolved_agent_name}' on workspace '{workspace_name}': {e}"
                    )
                    return
                if not remaining:
                    try:
                        from agentworks.agents.manager import _remove_from_workspace_group

                        _remove_from_workspace_group(
                            vm_check, config, db, linux_user, workspace_name, logger=None
                        )
                    except Exception as e:
                        output.warn(
                            f"rollback: failed to remove agent '{resolved_agent_name}' from "
                            f"workspace '{workspace_name}' group: {e}"
                        )

            try:
                # Everything that creates partial session state (on-VM group
                # membership, implicit-grant row, session row, restricted-config
                # write, tmux session) runs inside this block so a KI /
                # exception anywhere here triggers ``_rollback()``.
                if resolved_agent_name is not None:
                    # Auto-grant implicit workspace access if the agent has no
                    # existing grant on this workspace.
                    if not db.has_any_grant(resolved_agent_name, workspace_name):
                        from agentworks.agents.manager import _add_to_workspace_group

                        _add_to_workspace_group(
                            vm_check, config, db, linux_user, workspace_name
                        )
                    db.insert_agent_grant(
                        resolved_agent_name, workspace_name, "implicit", session_name=name
                    )

                # Insert DB record before any tmux work so a crash mid-create
                # leaves a recoverable row (and ``_rollback`` can find it to
                # delete).
                db.insert_session(
                    name,
                    workspace_name,
                    template.name,
                    mode,
                    agent_name=resolved_agent_name,
                    created_workspace=workspace_created,
                    created_agent=agent_created,
                    socket_path=expected_socket,
                )

                deploy_restricted_config(run_command, history_limit=config.session.history_limit)
                command = _build_session_command(
                    template, session_name=name, workspace_name=workspace_name
                )
                session_env = _resolve_session_env(
                    config,
                    db=db,
                    vm=vm_check,
                    ws=ws,
                    session_name=name,
                    session_template=template,
                    mode=mode,
                    agent_name=resolved_agent_name,
                    linux_user=linux_user,
                )
                # Pick the SSH transport for tmux operations:
                # - admin sessions: admin's run_command (unchanged)
                # - agent sessions: agent's run_command (FRD R1, direct
                #   target-user SSH). agent_target was built and probed above
                #   so a pre-rollout agent never reaches this point. admin's
                #   ``target`` is still passed for socket-root setup which
                #   requires root.
                session_run_command: RunCommand
                if mode == SessionMode.AGENT:
                    assert agent_target is not None  # set in the agent_name branch above
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
                    admin_username=vm_check.admin_username,
                    is_admin=(mode == SessionMode.ADMIN),
                    env=session_env,
                )
            except KeyboardInterrupt:
                output.warn(f"Cancelling session create '{name}'... rolling back.")
                _rollback()
                raise
            except Exception:
                _rollback()
                raise

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
            _regenerate_tmuxinator(db, config, vm_check, ws)
            from agentworks.sessions.console import add_session_to_console

            add_session_to_console(name, run_command=run_command, socket_path=sock)
    except KeyboardInterrupt:
        _rollback_ephemerals()
        raise
    except Exception:
        _rollback_ephemerals()
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
    _ws, vm, _run_command, _, admin_target = _prepare_vm(
        db, config, session.workspace_name, operation="session-stop"
    )
    with keep_vm_active(db, config, vm):
        session = _ensure_pid(session, target=admin_target, db=db)
        status = check_session_status(session, target=admin_target)

        if status == SessionStatus.STOPPED:
            output.info(f"Session '{name}' is already stopped")
            return
        # UNKNOWN is impossible here -- _ensure_pid raises on unresolvable sessions

        # Pick the destructive-op transport BEFORE doing anything destructive.
        # For agent sessions this also probes the agent's direct SSH so a
        # pre-rollout agent surfaces as an actionable StateError up front
        # rather than mid-kill (FRD R1, Phase 3). _build_session_target
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
    """Restart a session. Prompts if running (--yes to skip). --force for BROKEN."""
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    session = _require_session(db, name)
    ws, vm, run_command, _run_as_root, admin_target = _prepare_vm(
        db, config, session.workspace_name, operation="session-restart",
    )
    with keep_vm_active(db, config, vm):
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
        # direct agent SSH (FRD R1, Phase 3). _build_session_target always
        # returns a same-uid target, so no sudo is needed for kill.
        is_admin = session.mode == SessionMode.ADMIN.value
        session_target = _build_session_target(
            session, vm=vm, config=config, db=db, admin_target=admin_target
        )
        session_run_command: RunCommand = session_target.run
        kill_sudo = False

        # Bail-before-prompt: refuse the operation up front in the cases
        # where the operator either lacks the right flag (BROKEN + no
        # --force) or declines the confirm (OK + interactive 'no').
        # Eager-resolve runs AFTER these checks so we don't ask for
        # secrets the command was about to discard.
        # UNKNOWN is impossible here -- _ensure_pid raises on unresolvable
        # sessions. Legacy sessions short-circuit at ``status =
        # SessionStatus.STOPPED`` above, so neither gate fires for them --
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

        # Eager-prompting orchestration (FRD R4 / Phase 6): resolve every
        # secret referenced by this session's env chain BEFORE any kill /
        # destructive step. Non-interactive failures surface as
        # SecretUnavailableError with no partial state to clean up.
        template = _resolve_template(config, session.template)

        # Pre-flight the template's required commands before the destructive
        # kill below, so a missing binary aborts the restart with a clear
        # error instead of tearing down the old session and then failing to
        # bring up the new one (see _assert_required_commands).
        _assert_required_commands(
            session_run_command,
            template,
            session_name=name,
            target_label=(f"agent '{session.agent_name}'" if session.agent_name else f"VM '{vm.name}'"),
        )

        from agentworks.secrets import resolve_for_command

        resolve_for_command(
            [
                _session_secret_target(
                    config,
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
        )

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
            config,
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
    vm_name: str | None = None,
    workspace_name: str | None = None,
    force: bool = False,
) -> None:
    """Stop all running sessions, optionally filtered by VM or workspace."""
    sessions = filter_sessions(db, workspace_name=workspace_name, vm_name=vm_name)

    # Resolve distinct VMs from the filtered session set and enter the
    # keepalive BEFORE the SSH probes. The probes (ensure_pids_batch,
    # batch_check_all_sessions) issue per-VM round-trips; on WSL2 they
    # would race the idle timer without the anchor. No-op on non-WSL2.
    distinct_vms = _distinct_vms_for_sessions(db, sessions)
    with keep_vms_active(db, config, distinct_vms):
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
        # (FRD R1 carve-out): admin's path into agent tmux servers requires
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
    vm_name: str | None = None,
    workspace_name: str | None = None,
    include_running: bool = False,
    force: bool = False,
) -> None:
    """Restart sessions, optionally filtered by VM or workspace.

    With include_running=False (--all-stopped), only stopped sessions are
    restarted. With include_running=True (--all), all sessions are targeted;
    if any are running, the caller should have prompted or passed yes=True.
    """
    sessions = filter_sessions(db, workspace_name=workspace_name, vm_name=vm_name)

    # Resolve distinct VMs from the filtered set and anchor them BEFORE the
    # SSH probes. Each restart_session call also enters its own keepalive;
    # the redundant inner wrap is a no-op on already-active VMs and a cheap
    # extra subprocess on WSL2 (accepted, see PR description).
    distinct_vms = _distinct_vms_for_sessions(db, sessions)

    failed: list[tuple[str, str]] = []
    with keep_vms_active(db, config, distinct_vms):
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
    ws, vm, _run_command, _run_as_root, admin_target = _prepare_vm(
        db, config, session.workspace_name, operation="session-delete"
    )
    with keep_vm_active(db, config, vm):
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
        # SSH (FRD R1, Phase 3); a pre-rollout agent surfaces here as an
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
                from agentworks.agents.manager import _remove_from_workspace_group

                agent = db.get_agent(session.agent_name)
                if agent:
                    _remove_from_workspace_group(vm, config, db, agent.linux_user, session.workspace_name)

        _regenerate_tmuxinator(db, config, vm, ws)

        # Best-effort console cleanup runs after all DB / tmuxinator state has
        # settled. Stale tmux windows are recoverable cosmetic noise; if the
        # helper raises AgentworksError we skip the success message and any
        # created_workspace / created_agent cleanup below -- those would re-use
        # the same broken transport and just compound errors.
        if member_consoles:
            from agentworks.sessions.multi_console import kill_session_windows

            # Consoles are admin-owned (FRD R1 carve-out): admin manages
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
    """Show session details."""
    session = _require_session(db, name)
    ws, vm, run_command, _, target = _prepare_vm(db, config, session.workspace_name, operation=None)
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
) -> None:
    """List sessions with batched status checks (one SSH call per VM, parallel).

    Status resolution is has-session-first; PID/boot_id are only used as a
    follow-up when agent checks fail.
    """
    sessions = db.list_sessions(
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        admin_only=admin_only,
    )
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
    with keep_vms_active(db, config, status_keepalive_vms):
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
) -> None:
    """Attach to a session's tmux session (interactive)."""
    from agentworks.sessions.tmux import tmux_cmd

    session = _require_session(db, name)
    _ws, vm, _run_command, _, target = _prepare_vm(db, config, session.workspace_name, operation="session-attach")
    with keep_vm_active(db, config, vm):
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
        sys.exit(target.interactive(tmux_cmd(f"attach -t {q_session}", session.socket_path)))


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
    _ws, vm, run_command, _, target = _prepare_vm(db, config, session.workspace_name, operation="session-logs")
    with keep_vm_active(db, config, vm):
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


