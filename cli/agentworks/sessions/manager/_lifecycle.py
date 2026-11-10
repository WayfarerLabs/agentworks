"""Session start, stop, and restart operations."""

from __future__ import annotations

import shlex
from contextlib import contextmanager
from functools import partial
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.capabilities.harness_integration import HarnessLaunchIntent
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
from agentworks.errors import (
    AgentworksError,
    BrokenStateError,
    ExternalError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.sessions.tmux import ADMIN_SOCKET_ROOT, AGENT_SOCKET_ROOT

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.transports import Transport


@contextmanager
def _redacted_launch_errors(redactions: Iterable[str]) -> Iterator[None]:
    """Sanitize operation failures before the CLI renders or logs them."""
    from agentworks.harness_setup.outcomes import sanitize_setup_error

    values = tuple(redactions)
    try:
        yield
    except AgentworksError as error:
        sanitize_setup_error(error, redactions=values)
        raise


def _requested_launch_intent(*, force_new: bool, resume_only: bool) -> HarnessLaunchIntent:
    """Translate the service flags into one mutually exclusive launch policy."""
    if force_new and resume_only:
        raise ValidationError("--resume-only and --force-new are mutually exclusive")
    if force_new:
        return HarnessLaunchIntent.FORCE_NEW
    if resume_only:
        return HarnessLaunchIntent.RESUME_ONLY
    return HarnessLaunchIntent.RESUME_OR_NEW


def _confirm_running_restart(
    names: list[str],
    *,
    yes: bool,
    interaction: TtyInteractionPolicy,
) -> None:
    """Require consent before replacing live session runtimes."""
    from agentworks.secrets.policy import TtyInteractionPolicy, require_exact_tty_interaction_policy

    require_exact_tty_interaction_policy(interaction)
    if not names or yes:
        return

    if interaction is TtyInteractionPolicy.REFUSE:
        raise StateError(
            "restarting running sessions requires confirmation",
            hint="Pass --yes to skip confirmation.",
        )

    if len(names) == 1:
        confirmed = output.confirm(f"Session '{names[0]}' is running. Restart it?")
    else:
        preview = ", ".join(names[:5])
        if len(names) > 5:
            preview = f"{preview} (and {len(names) - 5} more)"
        output.warn(f"{len(names)} running sessions will be restarted: {preview}.")
        confirmed = output.confirm("Restart these sessions?")
    if not confirmed:
        raise UserAbort("restart cancelled")


def _require_selected_session(db: Database, name: str, session_uuid: str) -> SessionRow:
    """Re-fetch one selected session and refuse a same-name replacement."""
    session = _mgr._require_session(db, name)
    if session.session_uuid != session_uuid:
        raise StateError(
            f"session '{name}' was replaced while the operation was preparing",
            entity_kind="session",
            entity_name=name,
            hint="Retry the operation against the current session.",
        )
    return session


def _validated_socket_path(db: Database, session: SessionRow) -> str:
    """Validate persisted socket identity before destructive use."""
    socket_path = session.socket_path
    if socket_path is None:
        raise StateError(
            f"session '{session.name}' has no dedicated tmux socket",
            entity_kind="session",
            entity_name=session.name,
        )
    if session.mode == SessionMode.AGENT.value:
        agent = db.get_agent(session.agent_name or "")
        owner_dir = f"{AGENT_SOCKET_ROOT}/{agent.linux_user}" if agent is not None else ""
    else:
        workspace = db.get_workspace(session.workspace_name)
        vm = db.get_vm(workspace.vm_name) if workspace is not None else None
        owner_dir = f"{ADMIN_SOCKET_ROOT}/{vm.admin_username}" if vm is not None else ""
    path = PurePosixPath(socket_path)
    if not path.is_absolute() or str(path.parent) != owner_dir or path.name in {"", ".", ".."}:
        raise StateError(
            f"session '{session.name}' has an invalid managed tmux socket path",
            entity_kind="session",
            entity_name=session.name,
            hint="Repair the persisted session row before retrying destructive cleanup.",
        )
    return socket_path


def _mark_stopped(db: Database, session: SessionRow) -> None:
    db.update_session_runtime(
        session.name,
        socket_path=session.socket_path,
        pid=PID_STOPPED,
        boot_id=None,
        tmux_server_start_ticks=None,
    )


def _mark_runtime_unknown(db: Database, session: SessionRow, *, socket_path: str, boot_id: str) -> None:
    """Persist an addressable runtime whose process identity is not yet known."""
    db.update_session_runtime(
        session.name,
        socket_path=socket_path,
        pid=None,
        boot_id=boot_id,
        tmux_server_start_ticks=None,
    )


def _remove_stale_socket_and_mark_stopped(
    session: SessionRow,
    *,
    target: Transport,
    target_owns_session: bool,
    db: Database,
) -> None:
    """Remove an exact stale socket only after the caller proved server absence."""
    from agentworks.sessions.tmux import ProbeStatus, _test_presence_from_result

    if session.socket_path is not None:
        socket_path = _validated_socket_path(db, session)
        q_socket = shlex.quote(socket_path)
        removed = target.run(
            f"if test -L {q_socket} || {{ test -e {q_socket} && ! test -S {q_socket}; }}; then exit 2; fi; "
            f"if test -S {q_socket}; then rm -f -- {q_socket}; fi",
            sudo=not target_owns_session,
            check=False,
        )
        if _test_presence_from_result(removed) is not ProbeStatus.PRESENT:
            raise ExternalError(
                f"failed to remove stale tmux socket for session '{session.name}'",
                entity_kind="session",
                entity_name=session.name,
            )
        remains = _test_presence_from_result(
            target.run(f"test -e {q_socket}", sudo=not target_owns_session, check=False)
        )
        if remains is not ProbeStatus.ABSENT:
            raise ExternalError(
                f"could not verify stale tmux socket removal for session '{session.name}'",
                entity_kind="session",
                entity_name=session.name,
            )
    _mark_stopped(db, session)


def _recover_broken_session(
    session: SessionRow,
    *,
    target: Transport,
    target_owns_session: bool,
    db: Database,
) -> None:
    """Clean stale state only after proving the prior server is absent."""
    if not _mgr._prove_stored_runtime_absent(
        session,
        target=target,
        sudo=not target_owns_session,
    ):
        raise BrokenStateError(
            f"session '{session.name}' may still own a live tmux server; refusing stale cleanup",
            entity_kind="session",
            entity_name=session.name,
            hint="Recover the tmux runtime manually before retrying.",
        )
    _remove_stale_socket_and_mark_stopped(
        session,
        target=target,
        target_owns_session=target_owns_session,
        db=db,
    )


def _teardown_legacy_session(
    session: SessionRow,
    *,
    target: Transport,
    target_owns_session: bool,
    db: Database,
    running_authorized: bool,
) -> None:
    """Destroy one exact session on a reachable legacy shared tmux server."""
    from agentworks.sessions.tmux import (
        ProbeStatus,
        kill_session,
        probe_tmux_session,
        probe_tmux_session_after_teardown,
    )

    def run_runtime(command: str, *, check: bool = True, env: dict[str, str] | None = None) -> object:
        return target.run(command, sudo=not target_owns_session, check=check, env=env)

    presence = probe_tmux_session(session.name, run_command=run_runtime, socket_path=None)
    if presence is ProbeStatus.UNKNOWN:
        raise ExternalError(
            f"could not determine legacy runtime state for session '{session.name}'",
            entity_kind="session",
            entity_name=session.name,
        )
    if presence is ProbeStatus.PRESENT:
        if not running_authorized:
            raise StateError(
                f"session '{session.name}' was not observed running before lifecycle preparation",
                entity_kind="session",
                entity_name=session.name,
                hint="Retry the restart to confirm its current running state.",
            )
        kill_session(session.name, run_command=run_runtime, socket_path=None)
        presence = probe_tmux_session_after_teardown(session.name, run_command=run_runtime, socket_path=None)
    if presence is not ProbeStatus.ABSENT:
        raise ExternalError(
            f"failed to verify legacy session '{session.name}' stopped",
            entity_kind="session",
            entity_name=session.name,
        )
    _mark_stopped(db, session)


def _teardown_session(
    session: SessionRow,
    *,
    target: Transport,
    target_owns_session: bool,
    db: Database,
    force: bool,
    legacy_running_authorized: bool,
) -> None:
    """Destroy one managed session runtime and verify its absence."""
    from agentworks.sessions.tmux import (
        ProbeStatus,
        _matches_launch_attempt,
        capture_tmux_server_fingerprint,
        kill_server,
        require_socket_path,
    )

    if session.socket_path is None:
        if session.pid == PID_STOPPED:
            return
        _teardown_legacy_session(
            session,
            target=target,
            target_owns_session=target_owns_session,
            db=db,
            running_authorized=legacy_running_authorized,
        )
        return

    socket_path = _validated_socket_path(db, session)
    require_socket_path(socket_path, run_command=partial(target.run, sudo=not target_owns_session))
    probe = capture_tmux_server_fingerprint(
        target=target,
        socket_path=socket_path,
        sudo=not target_owns_session,
    )
    if session.pid == PID_STOPPED and probe.status is ProbeStatus.ABSENT:
        _remove_stale_socket_and_mark_stopped(session, target=target, target_owns_session=target_owns_session, db=db)
        return
    if probe.status is not ProbeStatus.PRESENT:
        try:
            _recover_broken_session(
                session,
                target=target,
                target_owns_session=target_owns_session,
                db=db,
            )
            return
        except (BrokenStateError, StateError):
            if not force:
                raise BrokenStateError(
                    f"session '{session.name}' tmux server is unreachable or indeterminate",
                    entity_kind="session",
                    entity_name=session.name,
                    hint="Use --force only after the prior server has exited.",
                ) from None
            raise
    observed = probe.fingerprint
    assert observed is not None
    observed_boot_id = _mgr._validated_observed_boot_id(observed.boot_id, session=session)
    if session.pid == PID_STOPPED:
        raise StateError(
            f"session '{session.name}' has an unrecorded server at its managed socket",
            entity_kind="session",
            entity_name=session.name,
            hint="Inspect the conflicting server manually; its runtime was preserved.",
        )
    stored_boot_id = _mgr._validated_stored_boot_id(session)
    if observed.pid != session.pid or observed_boot_id != stored_boot_id:
        raise BrokenStateError(
            f"session '{session.name}' tmux server identity does not match persisted state",
            entity_kind="session",
            entity_name=session.name,
        )
    stored_ticks = _mgr._validated_stored_start_ticks(session)
    if stored_ticks is None:
        if not _matches_launch_attempt(
            target=target,
            socket_path=socket_path,
            session_name=session.name,
            session_uuid=session.session_uuid,
            run_id=session.run_id,
            fingerprint=observed,
            sudo=not target_owns_session,
        ):
            raise StateError(
                f"session '{session.name}' has an incomplete stored runtime identity",
                entity_kind="session",
                entity_name=session.name,
                hint="Stop the native workload manually, or stop and start its VM, then retry.",
            )
        db.update_session_runtime(
            session.name,
            socket_path=socket_path,
            pid=observed.pid,
            boot_id=observed_boot_id,
            tmux_server_start_ticks=observed.start_ticks,
        )
    elif observed.start_ticks != stored_ticks:
        raise BrokenStateError(
            f"session '{session.name}' tmux server process was replaced",
            entity_kind="session",
            entity_name=session.name,
        )

    def run_runtime(command: str, *, check: bool = True, env: dict[str, str] | None = None) -> object:
        return target.run(command, sudo=not target_owns_session, check=check, env=env)

    kill_server(run_command=run_runtime, socket_path=socket_path)
    refreshed = db.get_session(session.name)
    assert refreshed is not None
    if not _mgr._prove_stored_runtime_absent(
        refreshed,
        target=target,
        sudo=not target_owns_session,
    ):
        raise ExternalError(
            f"tmux server for session '{session.name}' survived kill-server",
            entity_kind="session",
            entity_name=session.name,
        )
    _remove_stale_socket_and_mark_stopped(
        refreshed,
        target=target,
        target_owns_session=target_owns_session,
        db=db,
    )


def stop_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Accept stopped intent and stop through non-activating preparation."""
    from agentworks.runtime_state import run_session_batch

    run_session_batch(
        db,
        config,
        [_mgr._require_session(db, name)],
        operation="stop",
        force=force,
        intent=HarnessLaunchIntent.RESUME_OR_NEW,
        running_restart_authorized=frozenset(),
        interaction=interaction,
    )


def stop_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    harness_integration_name: str | list[str] | None = None,
    console_name: str | list[str] | None = None,
    admin_only: bool = False,
    force: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Stop running sessions selected by relationship, integration, or mode.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``agent_name``
    and ``admin_only`` are mutually exclusive; the caller enforces
    the mutex.
    """
    sessions = _mgr.filter_sessions(
        db,
        config=config,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        harness_integration_name=harness_integration_name,
        console_name=console_name,
        admin_only=admin_only,
    )
    if not sessions:
        output.info("No running sessions to stop.")
        return

    from agentworks.runtime_state import run_session_batch

    run_session_batch(
        db,
        config,
        sessions,
        operation="stop",
        force=force,
        intent=HarnessLaunchIntent.RESUME_OR_NEW,
        running_restart_authorized=frozenset(),
        interaction=interaction,
    )


def _launch_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    harness_integration_name: str | list[str] | None = None,
    console_name: str | list[str] | None = None,
    admin_only: bool = False,
    replace_running: bool,
    force: bool = False,
    force_new: bool = False,
    resume_only: bool = False,
    yes: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Start sessions selected by relationship, integration, or mode.

    Batch start selects stopped sessions plus states required by explicit
    modifiers; restart considers every match and replaces running runtimes.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``agent_name``
    and ``admin_only`` are mutually exclusive; the caller enforces
    the mutex.
    """
    intent = _requested_launch_intent(force_new=force_new, resume_only=resume_only)
    sessions = _mgr.filter_sessions(
        db,
        config=config,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        harness_integration_name=harness_integration_name,
        console_name=console_name,
        admin_only=admin_only,
    )
    if not sessions:
        output.info("No matching sessions to start.")
        return

    authorized_running_names: set[str] = set()
    if replace_running:
        consent_status_map = _mgr.observe_session_statuses(
            sessions,
            db=db,
            config=config,
            include_legacy=True,
        )
        running_names = sorted(
            session.name for session in sessions if consent_status_map.get(session.name) is SessionStatus.RUNNING
        )
        _confirm_running_restart(running_names, yes=yes, interaction=interaction)
        authorized_running_names.update(running_names)

    from agentworks.runtime_state import run_session_batch

    run_session_batch(
        db,
        config,
        sessions,
        operation="restart" if replace_running else "start",
        force=force,
        intent=intent,
        running_restart_authorized=frozenset(authorized_running_names),
        interaction=interaction,
    )


def start_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    force_new: bool = False,
    resume_only: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    intent = _requested_launch_intent(force_new=force_new, resume_only=resume_only)
    from agentworks.runtime_state import run_session_batch

    run_session_batch(
        db,
        config,
        [_mgr._require_session(db, name)],
        operation="start",
        force=force,
        intent=intent,
        running_restart_authorized=frozenset(),
        interaction=interaction,
    )


def restart_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    force_new: bool = False,
    resume_only: bool = False,
    yes: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Restart a session, confirming first when its runtime is live."""
    intent = _requested_launch_intent(force_new=force_new, resume_only=resume_only)
    session = _mgr._require_session(db, name)
    consent_status = _mgr.observe_session_statuses(
        [session],
        db=db,
        config=config,
        include_legacy=True,
    ).get(name, SessionStatus.UNKNOWN)
    running_restart_authorized = consent_status is SessionStatus.RUNNING
    _confirm_running_restart(
        [name] if running_restart_authorized else [],
        yes=yes,
        interaction=interaction,
    )
    from agentworks.runtime_state import run_session_batch

    run_session_batch(
        db,
        config,
        [session],
        operation="restart",
        force=force,
        intent=intent,
        running_restart_authorized=frozenset({name}) if running_restart_authorized else frozenset(),
        interaction=interaction,
    )


def start_all_sessions(
    db: Database,
    config: Config,
    **kwargs: object,
) -> None:
    _launch_all_sessions(db, config, replace_running=False, **kwargs)  # type: ignore[arg-type]


def restart_all_sessions(
    db: Database,
    config: Config,
    **kwargs: object,
) -> None:
    _launch_all_sessions(db, config, replace_running=True, **kwargs)  # type: ignore[arg-type]
