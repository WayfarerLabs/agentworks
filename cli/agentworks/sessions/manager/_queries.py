"""Session describe, list, and attach operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import SessionStatus
from agentworks.db.projections import project_session_mode
from agentworks.errors import (
    AgentworksError,
    BrokenStateError,
    ConfigError,
    ConnectivityError,
    StateError,
    UserAbort,
)
from agentworks.execution.errors import TrustMissing
from agentworks.list_sorting import nullable_sort_value, sort_rows
from agentworks.runtime_time import derive_uptime_seconds, format_short_uptime, format_uptime

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, InstanceStateInspection, SessionRow
    from agentworks.execution.requests import TerminalCommand
    from agentworks.instance_description import InstanceStateDescription
    from agentworks.machine_output import JsonObject, JsonValue
    from agentworks.resources.registry import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.sessions.template import SessionTemplate

    from ._tmux_server import SessionTmuxServer


@dataclass(frozen=True)
class SessionListRow:
    name: str
    workspace_name: str
    vm_name: str | None
    template: str
    harness_integration: str | None
    mode: str
    agent_name: str | None
    status: str
    uptime_seconds: int | None = None
    desired_runtime_state: str | None = None


@dataclass(frozen=True)
class SessionListing:
    sessions: tuple[SessionListRow, ...]


@dataclass(frozen=True)
class SessionConsole:
    console_name: str
    position: int


@dataclass(frozen=True)
class SessionDescription:
    name: str
    workspace_name: str
    vm_name: str
    template: str
    harness_integration: str | None
    mode: str
    agent_name: str | None
    status: str
    pid: int | None
    created_at: str
    updated_at: str
    consoles: tuple[SessionConsole, ...]
    instance_state: InstanceStateDescription
    last_started_at: str | None = None
    uptime_seconds: int | None = None
    desired_runtime_state: str | None = None


def session_listing_data(listing: SessionListing) -> JsonObject:
    """Project session list facts into the closed JSON v1 shape."""
    rows: list[JsonValue] = []
    for session in listing.sessions:
        if session.vm_name is None:
            raise AssertionError("session list JSON requires a resolved VM name")
        rows.append(
            {
                "name": session.name,
                "workspace_name": session.workspace_name,
                "vm_name": session.vm_name,
                "template": session.template,
                "harness_integration": session.harness_integration,
                "mode": session.mode,
                "agent_name": session.agent_name,
                "status": session.status,
                "desired_runtime_state": session.desired_runtime_state,
            }
        )
    return {
        "sessions": rows,
    }


def session_description_data(description: SessionDescription) -> JsonObject:
    """Project session detail facts into the closed JSON v1 shape."""
    data: JsonObject = {
        "session": {
            "name": description.name,
            "workspace_name": description.workspace_name,
            "vm_name": description.vm_name,
            "template": description.template,
            "harness_integration": description.harness_integration,
            "mode": description.mode,
            "agent_name": description.agent_name,
            "status": description.status,
            "desired_runtime_state": description.desired_runtime_state,
            "pid": description.pid,
            "created_at": description.created_at,
            "last_started_at": description.last_started_at,
            "uptime_seconds": description.uptime_seconds,
            "updated_at": description.updated_at,
            "consoles": [
                {"console_name": console.console_name, "position": console.position} for console in description.consoles
            ],
        },
    }
    from agentworks.instance_description import instance_state_data

    session_data = cast("JsonObject", data["session"])
    session_data["instance_state"] = instance_state_data(description.instance_state)
    return data


def session_description(
    db: Database,
    config: Config,
    *,
    name: str,
) -> SessionDescription:
    """Collect session detail facts with non-activating live status."""
    session = _mgr._require_session(db, name)
    workspace = _mgr._require_workspace(db, session.workspace_name)
    vm = _mgr._require_vm_for_workspace(db, workspace)
    output.info(f"Checking session '{name}' runtime...")
    status = SessionStatus.UNKNOWN
    try:
        server = _mgr.resolve_session_tmux_server(db, session)
    except StateError as exc:
        # Never probed: a row the owner rule refuses stays UNKNOWN.
        output.warn(f"Session status is unavailable: {exc}.")
        server = None
    if session.socket_path is None:
        status = _mgr._legacy_session_status(session)
    elif server is not None:
        from agentworks.vms.applied_state import VMSSHIdentityPolicyError

        try:
            from agentworks.vms.manager import require_vm_ssh_boundary

            require_vm_ssh_boundary(db, config, vm)
        except UserAbort:
            raise
        except TrustMissing as exc:
            # Describe degrades to an unknown runtime status, as it does for the
            # other SSH preconditions, but says why for this VM.
            output.warn(f"{exc}. {exc.hint}")
        except (ConfigError, ConnectivityError, VMSSHIdentityPolicyError):
            pass
        else:
            try:
                status = _mgr.check_session_status(
                    session, admin=_mgr._identity_access(vm, config, None, logger=None), server=server
                )
            except UserAbort:
                raise
            except ConnectivityError:
                pass
    if status is SessionStatus.UNKNOWN:
        output.warn(f"Session '{name}' runtime status could not be determined.")
    return _session_structural_description(db, config, name, status)


def _session_structural_description(
    db: Database,
    config: Config,
    name: str,
    status: SessionStatus,
) -> SessionDescription:
    """Take the authoritative post-observation structural snapshot."""
    with db.snapshot():
        session = _mgr._require_session(db, name)
        workspace = _mgr._require_workspace(db, session.workspace_name)
        from agentworks.instance_description import load_instance_description_registry

        registry = load_instance_description_registry(db, config, "session", name)
        inspection = db.instance_state.inspect_owner_state("session", name)
        instance_state = _session_instance_state(registry, session, inspection)
        harness_integration = _session_harness_integration(instance_state)
        return SessionDescription(
            name=session.name,
            desired_runtime_state=session.desired_runtime_state,
            workspace_name=session.workspace_name,
            vm_name=workspace.vm_name,
            template=session.template,
            harness_integration=harness_integration,
            mode=project_session_mode(session.mode),
            agent_name=session.agent_name,
            status={
                SessionStatus.RUNNING: "running",
                SessionStatus.STOPPED: "stopped",
                SessionStatus.BROKEN: "broken",
                SessionStatus.RESIDUAL: "residual",
                SessionStatus.UNKNOWN: "unknown",
            }[status],
            pid=session.pid if session.pid is not None and session.pid > 0 else None,
            created_at=session.created_at,
            updated_at=session.updated_at,
            consoles=tuple(
                SessionConsole(console_name=console_name, position=position)
                for console_name, position in db.list_console_memberships_for_session(session.name)
            ),
            instance_state=instance_state,
            last_started_at=session.last_started_at,
            uptime_seconds=derive_uptime_seconds(
                session.last_started_at,
                running=status is SessionStatus.RUNNING,
                entity_kind="session",
                entity_name=session.name,
            ),
        )


def _session_instance_state(
    registry: Registry,
    session: SessionRow,
    inspection: InstanceStateInspection,
) -> InstanceStateDescription:
    from agentworks.instance_description import single_declaration_instance_state
    from agentworks.resources.access import ResourceIdentity
    from agentworks.sessions.templates import resolve_template_with_provenance

    selection = ResourceIdentity("session-template", session.template)
    return single_declaration_instance_state(
        instance_kind="session",
        selection=selection,
        inspection=inspection,
        resolve=lambda overlay: resolve_template_with_provenance(
            registry,
            session.template,
            overlay=cast("SessionTemplate | None", overlay),
            instance_name=session.name,
        ),
    )


def _session_harness_integration(state: InstanceStateDescription) -> str | None:
    """Read the harness integration from the same strict current declaration."""
    from agentworks.resources.resolved_spec import ResolvedSpec

    declaration = state.declarations[0].current
    if not isinstance(declaration, ResolvedSpec):
        return None
    value = declaration.spec["harness_integration"]
    if not isinstance(value, str):
        raise AssertionError("resolved session harness integration must be text")
    return value


def render_session_description(description: SessionDescription) -> None:
    """Render session detail facts with the legacy human layout."""
    status = description.status
    status_label = status
    if status == "running" and description.pid is not None:
        status_label = f"running (PID {description.pid})"
    elif status == "broken" and description.pid is not None:
        status_label = f"broken (PID {description.pid} alive, tmux unreachable)"
    mode = description.mode
    mode_label = (
        mode if mode == "unknown" else f"agent ({description.agent_name})" if description.agent_name else "admin"
    )
    output.info(f"Name:           {description.name}")
    output.info(f"Workspace:      {description.workspace_name}")
    output.info(f"VM:             {description.vm_name}")
    output.info(f"Template:       {description.template}")
    output.info(f"Harness integration: {description.harness_integration or '-'}")
    output.info(f"Mode:           {mode_label}")
    output.info(f"Desired state:  {description.desired_runtime_state or 'adoption required'}")
    output.info(f"Status:         {status_label}")
    output.info(f"Created:        {description.created_at}")
    output.info(f"Last Started:   {description.last_started_at or 'unknown'}")
    output.info(f"Uptime:         {format_uptime(description.uptime_seconds, running=status == 'running')}")
    output.info(f"Updated:        {description.updated_at}")
    from agentworks.instance_description import render_instance_state

    render_instance_state(description.instance_state)
    output.info(f"\nConsoles ({len(description.consoles)}):")
    if description.consoles:
        for console in description.consoles:
            output.detail(f"[{console.position}] {console.console_name}")
    else:
        output.detail("(none)")


def describe_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Show session details."""
    render_session_description(session_description(db, config, name=name))


def list_sessions(
    db: Database,
    config: Config,
    *,
    workspace_name: str | list[str] | None = None,
    vm_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    harness_integration_name: str | list[str] | None = None,
    admin_only: bool = False,
    include_status: bool = False,
    names_only: bool = False,
    sort_keys: tuple[str, ...] | None = None,
) -> None:
    """List sessions, optionally observing live status by backing VM.

    With ``names_only=True``, emit one session name per line and
    skip both the SSH status batch and the table render. Used by
    shell completion (see issue #147); the order matches the table's
    selected service ordering so completion stays stable.
    """
    if names_only:
        sessions, _registry = _sorted_session_rows(
            db,
            config=config,
            workspace_name=workspace_name,
            vm_name=vm_name,
            agent_name=agent_name,
            harness_integration_name=harness_integration_name,
            admin_only=admin_only,
            sort_keys=sort_keys,
        )
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No sessions found" line below is
        # for human readers only.
        for session in sessions:
            output.info(session.name)
        return
    listing = _mgr.session_listing(
        db,
        config,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        harness_integration_name=harness_integration_name,
        admin_only=admin_only,
        include_status=include_status,
        sort_keys=sort_keys,
    )
    render_session_listing(listing, include_status=include_status)


def session_listing(
    db: Database,
    config: Config,
    *,
    workspace_name: str | list[str] | None = None,
    vm_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    harness_integration_name: str | list[str] | None = None,
    admin_only: bool = False,
    include_status: bool = False,
    require_vm_names: bool = False,
    sort_keys: tuple[str, ...] | None = None,
) -> SessionListing:
    """Collect local session inventory, optionally enriched by live status.

    ``require_vm_names`` keeps closed projections from receiving the nullable
    VM fact used only by human recovery inventory.
    """
    sessions, filter_registry = _sorted_session_rows(
        db,
        config=config,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        harness_integration_name=harness_integration_name,
        admin_only=admin_only,
        sort_keys=sort_keys,
    )
    if not sessions:
        return SessionListing(sessions=())

    vm_names: dict[str, str | None] = {}
    observable_sessions: list[SessionRow] = []
    observable_vm_names: set[str] = set()
    for session in sessions:
        workspace = (
            _mgr._require_workspace(db, session.workspace_name)
            if require_vm_names
            else db.get_workspace(session.workspace_name)
        )
        if workspace is None:
            vm_names[session.name] = None
            continue
        vm_names[session.name] = workspace.vm_name
        if db.get_vm(workspace.vm_name) is None:
            continue
        observable_sessions.append(session)
        observable_vm_names.add(workspace.vm_name)

    status_map: dict[str, SessionStatus] = (
        {session.name: SessionStatus.UNKNOWN for session in sessions} if include_status else {}
    )
    if include_status:
        output.info(
            f"Checking status for {output.count(len(sessions), 'session')} "
            f"across {output.count(len(observable_vm_names), 'VM')}..."
        )
        if observable_sessions:
            status_map.update(
                _mgr.observe_session_statuses(
                    observable_sessions,
                    db=db,
                    config=config,
                )
            )
    observed_at = datetime.now(UTC) if include_status else None
    registry = filter_registry or _mgr._display_registry(config)
    harness_by_template: dict[str, str] = {}

    def harness_for(template_name: str) -> str | None:
        if template_name not in harness_by_template:
            harness_by_template[template_name] = _mgr._display_harness_integration(registry, template_name)
        label = harness_by_template[template_name]
        return None if label == "-" else label

    def live_harness_for(session: SessionRow) -> str | None:
        try:
            stored = db.instance_state.get_desired_overlay("session", session.name)
        except AgentworksError:
            return None
        if stored is None:
            return harness_for(session.template)
        label = _mgr._display_live_harness_integration(db, registry, session.name, session.template)
        return None if label == "-" else label

    facts: list[SessionListRow] = []
    for session in sessions:
        observed_status = status_map.get(session.name, SessionStatus.UNKNOWN) if include_status else None
        status = observed_status.value if observed_status is not None else "unavailable"
        uptime_seconds = None
        if observed_status is SessionStatus.RUNNING and session.last_started_at is not None:
            uptime_seconds = derive_uptime_seconds(
                session.last_started_at,
                running=True,
                entity_kind="session",
                entity_name=session.name,
                now=observed_at,
            )
        facts.append(
            SessionListRow(
                name=session.name,
                desired_runtime_state=session.desired_runtime_state,
                workspace_name=session.workspace_name,
                vm_name=vm_names[session.name],
                template=session.template,
                harness_integration=live_harness_for(session),
                mode=project_session_mode(session.mode),
                agent_name=session.agent_name,
                status=status,
                uptime_seconds=uptime_seconds,
            )
        )
    return SessionListing(sessions=tuple(facts))


def _sorted_session_rows(
    db: Database,
    *,
    config: Config,
    workspace_name: str | list[str] | None,
    vm_name: str | list[str] | None,
    agent_name: str | list[str] | None,
    harness_integration_name: str | list[str] | None,
    admin_only: bool,
    sort_keys: tuple[str, ...] | None,
) -> tuple[tuple[SessionRow, ...], Registry | None]:
    if harness_integration_name is None:
        sessions = _mgr.filter_sessions(
            db,
            workspace_name=workspace_name,
            vm_name=vm_name,
            agent_name=agent_name,
            admin_only=admin_only,
        )
        registry = None
    else:
        sessions, registry = _mgr._filter_sessions_with_registry(
            db,
            config=config,
            workspace_name=workspace_name,
            vm_name=vm_name,
            agent_name=agent_name,
            harness_integration_name=harness_integration_name,
            admin_only=admin_only,
        )
    vm_names: dict[str, str | None] = {}

    def session_vm_name(session: SessionRow) -> str | None:
        if session.name not in vm_names:
            workspace = db.get_workspace(session.workspace_name)
            vm_names[session.name] = None if workspace is None else workspace.vm_name
        return vm_names[session.name]

    return (
        sort_rows(
            sessions,
            sort_keys=sort_keys,
            key_functions={
                "alpha": lambda session: (session.name,),
                "creation": lambda session: nullable_sort_value(session.created_at),
                "vm": lambda session: nullable_sort_value(session_vm_name(session)),
                "agent": lambda session: nullable_sort_value(session.agent_name),
                "workspace": lambda session: nullable_sort_value(session.workspace_name),
            },
            entity_kind="session",
        ),
        registry,
    )


def render_session_listing(listing: SessionListing, *, include_status: bool = False) -> None:
    """Render session inventory, adding status only when requested."""
    if not listing.sessions:
        output.info("No sessions found.")
        return

    rows: list[tuple[str, ...]] = []
    broken_names: list[str] = []
    unknown_by_vm: dict[str, list[str]] = {}
    unknown_without_vm: list[str] = []
    for session in listing.sessions:
        status = session.status
        status_label = "-" if status == "unavailable" else status
        if status == "running" and session.uptime_seconds is not None:
            status_label = f"running ({format_short_uptime(session.uptime_seconds)})"
        if session.mode == "agent" and session.agent_name:
            user_label = session.agent_name
        elif session.mode == "admin" and session.agent_name is None:
            user_label = "--admin--"
        else:
            user_label = "unknown"
        row = (
            session.name,
            session.workspace_name,
            session.vm_name or "-",
            session.template,
            session.harness_integration or "-",
            user_label,
            session.desired_runtime_state or "adoption required",
        )
        rows.append((*row, status_label) if include_status else row)
        if include_status and status == "broken":
            broken_names.append(session.name)
        elif include_status and status == "unknown":
            if session.vm_name is None:
                unknown_without_vm.append(session.name)
            else:
                unknown_by_vm.setdefault(session.vm_name, []).append(session.name)

    headers = ["NAME", "WORKSPACE", "VM", "TEMPLATE", "HARNESS INT.", "USER", "DESIRED STATE"]
    if include_status:
        headers.append("STATUS")
    for line in output.render_table(headers, rows, max_col_widths={headers.index("USER"): 40}):
        output.info(line)

    if broken_names or unknown_by_vm or unknown_without_vm:
        output.info("")
        if broken_names:
            output.warn(
                f"{len(broken_names)} session(s) are broken (tmux unreachable): "
                f"{', '.join(broken_names)}. Use start/restart/stop/delete --force."
            )
        if unknown_by_vm:
            groups = "; ".join(f"{vm}: {', '.join(names)}" for vm, names in unknown_by_vm.items())
            output.warn(f"Session status is unknown by VM: {groups}.")
        if unknown_without_vm:
            output.warn(f"Session status is unknown with no resolved VM: {', '.join(unknown_without_vm)}.")


def attach_session(
    db: Database,
    config: Config,
    *,
    name: str,
    interaction: TtyInteractionPolicy,
) -> int:
    """Attach to a session's tmux session (interactive).

    Returns the interactive session's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service),
    mirroring :func:`agentworks.vms.manager.exec_vm`.
    """
    session = _mgr._require_session(db, name)
    # Refuse a socket outside its owner's directory before any VM work. A
    # legacy row without a socket meets the status check's own refusal.
    server = _mgr.resolve_session_tmux_server(db, session)
    with _mgr._prepare_vm(
        db,
        config,
        session,
        operation="session-attach",
        interaction=interaction,
    ) as (_ws, _vm_node, admin, _log):
        status = _mgr.check_session_status(session, admin=admin, server=server)

        if status == SessionStatus.STOPPED:
            raise StateError(
                f"session '{name}' is not running",
                entity_kind="session",
                entity_name=name,
            )
        if status == SessionStatus.UNKNOWN:
            raise StateError(
                f"session '{name}' runtime state is unknown",
                entity_kind="session",
                entity_name=name,
                hint="Retry after transport access is reliable.",
            )
        if status == SessionStatus.RESIDUAL:
            raise StateError(
                f"session '{name}' has a residual tmux server but no canonical session",
                entity_kind="session",
                entity_name=name,
                hint=f"Run `agw session start {name}` or `agw session restart {name}` to recover it.",
            )
        if status == SessionStatus.BROKEN:
            raise BrokenStateError(
                f"session '{name}' is broken (PID alive but tmux unreachable).",
                entity_kind="session",
                entity_name=name,
            )

        from agentworks.terminal import clear_screen_on_detach

        # A session attach is a full-screen tmux; clear the local screen on
        # detach where we don't trust the terminal to restore cleanly.
        return admin.attach(
            _attach_command(session, server),
            clear_screen_on_exit=clear_screen_on_detach(config.terminal.clear_on_detach),
        )


def _attach_command(session: SessionRow, server: SessionTmuxServer | None) -> TerminalCommand:
    """The admin's terminal command that attaches to ``session``, reaching its tmux server only as its owner.

    A legacy row's session is on the admin's own default server and an admin
    session's server is the admin's, so the admin attaches directly. An agent
    session's server is the agent's, and no other account is ever its client:
    the admin elevates only to switch to the agent behind the socket guard
    (``as_socket_owner_line``), which says on the terminal why it refuses a
    socket. The agent's client runs on ``runuser``'s relay terminal, never on
    the admin's.
    """
    from agentworks.execution.requests import TerminalCommand
    from agentworks.sessions.tmux import as_socket_owner_line, exact_tmux_target, tmux_cmd

    target = "=" + session.name
    if server is None:
        return TerminalCommand(("tmux", "attach", "-t", target))
    if server.owner is None:
        return TerminalCommand(("tmux", "-S", server.socket_path, "attach", "-t", target))
    line = as_socket_owner_line(
        tmux_cmd(f"attach -t {exact_tmux_target(session.name)}", server.socket_path),
        socket_path=server.socket_path,
        owner=server.owner,
        switch_user=True,
        terminal=True,
        refusal=(
            f"Not attaching to session '{session.name}': its tmux socket is not a socket that '{server.owner}' owns."
        ),
    )
    return TerminalCommand(("sudo", "-n", "/bin/sh", "-c", line))
