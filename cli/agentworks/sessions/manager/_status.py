"""Session liveness checks (single and batched)."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
from agentworks.errors import (
    AgentworksError,
    ConfigError,
    ConnectivityError,
    NotFoundError,
    StateError,
    UserAbort,
)
from agentworks.execution.errors import TrustMissing
from agentworks.execution.requests import Bytes, Script, Shell
from agentworks.sessions.manager import _teardown
from agentworks.sessions.manager._teardown import ManagedSocket
from agentworks.sessions.tmux import (
    CommandOutcome,
    ProbeStatus,
    access_run_command,
    probe_tmux_server,
    probe_tmux_session,
    runtime_probe_command,
    runtime_probe_payload,
    runtime_probe_stderr,
    socket_owner_run_command,
)
from agentworks.status_observation import GUEST_OBSERVATION_TIMEOUT_SECONDS, cancelling_futures

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.execution.access import VMAccess
    from agentworks.sessions.tmux import RunCommand

_PID_PRESENT_FACT = "present"
_PID_ABSENT_FACT = "absent"


def owner_run_command(admin: VMAccess, session: SessionRow, socket: ManagedSocket) -> RunCommand:
    """The admin's bounded reads of ``session``'s runtime, each run as its tmux server's owner.

    Every read, tmux or not, goes through ``socket_owner_run_command`` on
    the validated ``socket``: an agent session's elevate only to drop to the
    agent, an admin session's run as the admin, and a socket the guard
    refuses reads as unknown. Each is one dispatch within the observation
    deadline.
    """
    return socket_owner_run_command(
        admin,
        socket_path=socket.path,
        owner=socket.owner,
        switch_user=session.mode == SessionMode.AGENT.value,
        deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS,
    )


def _pid_probe_script(pid: int) -> str:
    """Return a shell probe whose fact exists only after the shell started."""
    return f"if test -d /proc/{pid}; then printf {_PID_PRESENT_FACT}; else printf {_PID_ABSENT_FACT}; fi"


def _pid_presence_from_fact(returncode: int, fact: str | None, *, stderr: str | None = "") -> ProbeStatus:
    if returncode != 0 or fact is None or stderr is None:
        return ProbeStatus.UNKNOWN
    if fact == _PID_PRESENT_FACT and not stderr:
        return ProbeStatus.PRESENT
    if fact == _PID_ABSENT_FACT and not stderr:
        return ProbeStatus.ABSENT
    return ProbeStatus.UNKNOWN


def _pid_presence(pid: int, *, run_command: RunCommand) -> ProbeStatus:
    """Probe a PID without treating a transport failure as process absence."""
    result = run_command(runtime_probe_command(_pid_probe_script(pid), "PRESENT"), check=False)
    stdout = runtime_probe_payload(result.stdout, "PRESENT")
    stderr = runtime_probe_stderr(result.stderr)
    return _pid_presence_from_fact(result.returncode, stdout, stderr=stderr)


def _get_boot_id(run_command: RunCommand) -> str | None:
    """Read the current VM boot ID. Returns None on failure."""
    from agentworks.sessions.tmux import read_boot_id

    return read_boot_id(run_command)


def check_session_status(
    session: SessionRow,
    *,
    admin: VMAccess,
    db: Database,
) -> SessionStatus:
    """Determine session status. Dispatches by session type.

    A dedicated session's socket path is validated against its owner's
    directory first (``_validated_socket``, which raises ``StateError``), and
    every read runs as that owner (``owner_run_command``), on ``admin``, as
    one dispatch within the observation deadline; one that does not complete
    raises its typed error (``ExecutionIncomplete`` for a missed deadline), which a
    listing reports as unknown and a lifecycle command surfaces before it
    changes anything.

    No DB side effects. Raises ``StateError`` when the session row predates
    the per-session-socket model introduced by the env-and-secrets SDD
    (``socket_path is None`` for an admin session). The hint points the
    operator at ``agw session restart <name>``, which migrates the row to
    the new shape via a surgical kill of the named session on the default
    tmux server + a fresh ``create_tmux_session`` under a per-session
    socket. Callers that aren't a launch operation (attach, stop, etc.)
    can't safely migrate, so they surface the typed error and let the
    operator restart.
    """
    if session.socket_path is not None:
        return _check_dedicated_session(session, admin=admin, socket=_teardown._validated_socket(db, session))
    return _legacy_session_status(session)


def _legacy_session_status(session: SessionRow) -> SessionStatus:
    """A legacy shared-server row's status, which needs no read: unknown, or the canonical refusal for a live one."""
    if session.pid is None or session.boot_id is None:
        return SessionStatus.UNKNOWN
    raise _legacy_session_status_error(session)


def _legacy_session_status_error(session: SessionRow) -> StateError:
    """Build the canonical refusal for a live legacy shared-server row."""
    return StateError(
        f"session '{session.name}' has no socket_path",
        entity_kind="session",
        entity_name=session.name,
        hint=(
            "This session predates the per-session-socket model introduced by "
            f"the env-and-secrets SDD. Run `agw session restart {session.name}` "
            "to migrate it to the new shape."
        ),
    )


def _check_dedicated_session(session: SessionRow, *, admin: VMAccess, socket: ManagedSocket) -> SessionStatus:
    """Sessions with their own tmux server and socket, ``socket`` validated. Applies uniformly to
    admin and agent sessions after the env-and-secrets SDD migrated admin
    sessions to per-session sockets.

    Every read runs as the server's owner (``owner_run_command``).
    """
    socket_path = socket.path
    run_command = owner_run_command(admin, session, socket)
    session_presence = probe_tmux_session(session.name, run_command=run_command, socket_path=socket_path)
    if session_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if session_presence is ProbeStatus.PRESENT:
        if session.pid not in {None, PID_STOPPED} or _pending_launch_is_owned(session, admin=admin, socket=socket):
            return SessionStatus.RUNNING
        return SessionStatus.UNKNOWN

    server_presence = probe_tmux_server(run_command=run_command, socket_path=socket_path)
    if server_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if server_presence is ProbeStatus.PRESENT:
        return SessionStatus.UNKNOWN if session.pid in {None, PID_STOPPED} else SessionStatus.RESIDUAL

    # Exact session and dedicated server are authoritatively absent. The
    # stopped sentinel is supporting evidence only after those live facts.
    if session.pid == PID_STOPPED:
        return SessionStatus.STOPPED

    # has-session failed: STOPPED or BROKEN?
    if session.boot_id is None:
        return SessionStatus.UNKNOWN
    current_boot = _mgr._get_boot_id(run_command)
    if current_boot is None:
        return SessionStatus.UNKNOWN  # can't verify boot cycle, unsafe to offer --force
    stored_boot_id = _mgr._validated_stored_boot_id(session)
    if stored_boot_id != current_boot:
        return SessionStatus.STOPPED  # stale boot, PID is meaningless
    if session.pid is None or session.pid <= 0:
        return SessionStatus.UNKNOWN
    pid_presence = _pid_presence(session.pid, run_command=run_command)
    if pid_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if pid_presence is ProbeStatus.ABSENT:
        return SessionStatus.STOPPED  # process is dead
    return SessionStatus.BROKEN  # same boot, process alive, socket unreachable


def _pending_launch_is_owned(session: SessionRow, *, admin: VMAccess, socket: ManagedSocket) -> bool:
    """Observe a dispatched attempt without adopting its native identity, its reads as the server's owner."""
    from agentworks.sessions.tmux import (
        _matches_launch_attempt,
        canonical_boot_id,
        capture_tmux_server_fingerprint,
        require_socket_path,
    )

    if session.pid is not None or session.run_id is None:
        return False
    boot_id = canonical_boot_id(session.boot_id)
    if boot_id is None:
        return False
    run_command = owner_run_command(admin, session, socket)
    try:
        stored_ticks = _mgr._validated_stored_start_ticks(session)
        require_socket_path(socket.path, run_command=run_command)
        probe = capture_tmux_server_fingerprint(run_command=run_command, socket_path=socket.path)
        observed = probe.fingerprint
        return (
            probe.status is ProbeStatus.PRESENT
            and observed is not None
            and observed.boot_id == boot_id
            and (stored_ticks is None or stored_ticks == observed.start_ticks)
            and _matches_launch_attempt(
                run_command=run_command,
                socket_path=socket.path,
                session_name=session.name,
                session_uuid=session.session_uuid,
                run_id=session.run_id,
                fingerprint=observed,
            )
        )
    except UserAbort:
        raise
    except (AgentworksError, OSError):
        return False


def batch_check_status(
    sessions: list[SessionRow],
    *,
    admin: VMAccess,
    owners: Mapping[str, str],
) -> dict[str, SessionStatus]:
    """Probe session presence together, verifying pending launch candidates separately.

    ``owners`` names each session's socket owner, whose socket path the caller
    validated (``_validated_socket``). The probe is one elevated request on
    ``admin``, within the observation deadline, its rows on stdin; it runs
    each row's tmux reads as that row's owner, never as root, and refuses a
    row whose socket is a link, is not a socket, or belongs to another
    account, which reads as ``UNKNOWN``. A probe that does not complete raises
    its typed error. Returns {session_name: SessionStatus}. An unrecorded
    present server remains ``UNKNOWN`` unless its exact native launch identity
    matches the pending attempt. Observation never adopts that identity into
    the persisted row.
    """
    from agentworks.sessions.tmux import canonical_boot_id

    checkable = [s for s in sessions if s.socket_path is not None]
    if not checkable:
        return {}

    raw = admin.run(
        Script(
            runtime_probe_command(_batch_probe_command(), "BATCH"),
            Shell.SH,
            elevated=True,
            deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS,
            stdin=Bytes(_batch_probe_data(checkable, owners).encode()),
        )
    )
    returncode = raw.require_completed(admin.vm_name)
    stdout = runtime_probe_payload(raw.stdout.decode("utf-8", "replace"), "BATCH")

    status_map: dict[str, SessionStatus] = {
        session.name: SessionStatus.UNKNOWN for session in checkable if session.socket_path is not None
    }
    sessions_by_key = {_encoded_probe_field(session.name): session for session in checkable}

    if (
        returncode != 0
        or runtime_probe_stderr(raw.stderr.decode("utf-8", "replace")) != ""
        or stdout is None
        or "\x00" in stdout
        or "\r" in stdout
    ):
        return status_map

    frames: dict[str, list[str]] = {}
    framed_stdout = stdout[:-1] if stdout.endswith("\n") else stdout
    lines = framed_stdout.split("\n") if framed_stdout else []
    if any(not line for line in lines):
        return status_map
    for line in lines:
        fields = line.split(":")
        if len(fields) != 10:
            return status_map
        key = fields[1]
        if fields[0] != "S" or key not in sessions_by_key or key in frames or not _valid_batch_frame(fields):
            return status_map
        frames[key] = fields
    if set(frames) != set(sessions_by_key):
        return status_map

    for key, fields in frames.items():
        session = sessions_by_key[key]
        name = session.name

        has_presence = _batch_tmux_presence(fields[2], fields[3], missing_target_is_absent=True)
        if has_presence is ProbeStatus.PRESENT:
            owned = ManagedSocket(session.socket_path, owners[name]) if session.socket_path is not None else None
            if session.pid not in {None, PID_STOPPED} or (
                owned is not None and _pending_launch_is_owned(session, admin=admin, socket=owned)
            ):
                status_map[name] = SessionStatus.RUNNING
            continue
        if has_presence is ProbeStatus.UNKNOWN:
            continue

        server_presence = _batch_tmux_presence(fields[4], fields[5], missing_target_is_absent=False)
        if server_presence is ProbeStatus.PRESENT:
            if session.pid not in {None, PID_STOPPED}:
                status_map[name] = SessionStatus.RESIDUAL
            continue
        if server_presence is ProbeStatus.UNKNOWN:
            continue

        if session.pid == PID_STOPPED:
            status_map[name] = SessionStatus.STOPPED
            continue

        if session.boot_id is None:
            continue

        try:
            boot_returncode = int(fields[6])
            observed_boot = bytes.fromhex(fields[7]).decode("ascii")
            pid_returncode = int(fields[8])
            pid_fact = bytes.fromhex(fields[9]).decode("ascii")
            stored_boot = _mgr._validated_stored_boot_id(session)
        except (StateError, UnicodeDecodeError, ValueError):
            continue
        current_boot = canonical_boot_id(observed_boot) if boot_returncode == 0 else None
        if current_boot is None:
            continue
        if stored_boot != current_boot:
            status_map[name] = SessionStatus.STOPPED
            continue
        if session.pid is None or session.pid <= 0:
            continue
        pid_presence = _pid_presence_from_fact(pid_returncode, pid_fact)
        if pid_presence is ProbeStatus.UNKNOWN:
            continue
        if pid_presence is ProbeStatus.ABSENT:
            status_map[name] = SessionStatus.STOPPED
        else:
            status_map[name] = SessionStatus.BROKEN

    return status_map


def _encoded_probe_field(value: str) -> str:
    """Encode one row field without shell or frame delimiters."""
    return base64.b64encode(value.encode()).decode("ascii")


def _batch_probe_data(sessions: list[SessionRow], owners: Mapping[str, str]) -> str:
    """Encode selected session rows, each with its socket owner, as non-sensitive stdin protocol data."""
    records = []
    for session in sessions:
        assert session.socket_path is not None
        pid = (
            str(session.pid)
            if isinstance(session.pid, int) and not isinstance(session.pid, bool) and session.pid > 0
            else "-"
        )
        records.append(
            " ".join(
                (
                    _encoded_probe_field(session.name),
                    _encoded_probe_field(session.socket_path),
                    _encoded_probe_field(owners[session.name]),
                    pid,
                )
            )
        )
    return "\n".join(records) + "\n"


def _batch_probe_command() -> str:
    """Build the constant-size loop program for a VM's session rows.

    It runs as root only to read the boot id and drop to each row's owner:
    ``runuser`` runs that row's tmux reads as the owner, after root refuses a
    socket path that is a link, is not a socket, or is owned by another
    account, the checks ``tmux.as_socket_owner_line`` makes for every other
    owner read, inlined here for the loop. A refused row reports
    ``SOCKET_GUARD_REFUSED`` (126) for both reads, which reads as unknown.
    """
    return (
        "hex() { od -An -tx1 | tr -d ' \\n'; }; "
        'decode() { printf %s "$1" | base64 -d; }; '
        "BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null); B=$?; "
        'BOOT_HEX=$(printf %s "$BOOT" | hex); '
        "while read -r N64 S64 O64 PID; do "
        'N=$(decode "$N64") || exit 90; SOCK=$(decode "$S64") || exit 90; O=$(decode "$O64") || exit 90; '
        'if [ -L "$SOCK" ] || { [ -e "$SOCK" ] && { [ ! -S "$SOCK" ] || '
        '[ "$(stat -c %U -- "$SOCK" 2>/dev/null)" != "$O" ]; }; }; then '
        "H=126; HERR=; S=126; SERR=; else "
        'HERR=$(runuser -u "$O" -- tmux -S "$SOCK" has-session -t "=$N" 2>&1 >/dev/null); H=$?; '
        'SERR=$(runuser -u "$O" -- tmux -S "$SOCK" list-sessions 2>&1 >/dev/null); S=$?; fi; '
        'HHEX=$(printf %s "$HERR" | hex); '
        'SHEX=$(printf %s "$SERR" | hex); '
        'if [ "$PID" = - ]; then PFACT=; P=1; '
        'else PFACT=$(sh -c "if test -d /proc/$PID; then printf present; '
        'else printf absent; fi" 2>/dev/null); P=$?; fi; '
        'PHEX=$(printf %s "$PFACT" | hex); '
        'printf "S:%s:%s:%s:%s:%s:%s:%s:%s:%s\\n" '
        '"$N64" "$H" "$HHEX" "$S" "$SHEX" "$B" "$BOOT_HEX" "$P" "$PHEX"; '
        "done"
    )


def _valid_batch_frame(fields: list[str]) -> bool:
    """Reject malformed framing before any requested row is trusted."""
    try:
        for index in (2, 4, 6, 8):
            int(fields[index])
        bytes.fromhex(fields[3]).decode("utf-8")
        bytes.fromhex(fields[5]).decode("utf-8")
        bytes.fromhex(fields[7]).decode("ascii")
        bytes.fromhex(fields[9]).decode("ascii")
    except (UnicodeDecodeError, ValueError):
        return False
    return True


def _batch_tmux_presence(
    returncode: str,
    diagnostic_hex: str,
    *,
    missing_target_is_absent: bool,
) -> ProbeStatus:
    """Decode one batch fact and delegate classification to tmux authority."""
    from agentworks.sessions.tmux import _tmux_presence_from_result

    try:
        code = int(returncode)
        diagnostic = bytes.fromhex(diagnostic_hex).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return ProbeStatus.UNKNOWN
    return _tmux_presence_from_result(
        CommandOutcome("", diagnostic, code), missing_target_is_absent=missing_target_is_absent
    )


def observe_session_statuses(
    sessions: list[SessionRow],
    *,
    db: Database,
    config: Config,
    include_legacy: bool = False,
) -> dict[str, SessionStatus]:
    """Observe selected sessions, optionally including legacy shared-server rows.

    A session that cannot be read reads as ``UNKNOWN``: a VM whose SSH
    boundary or reads fail warns once with the first error, and a row whose
    socket path fails validation warns naming it.
    """
    from concurrent.futures import as_completed
    from functools import partial

    from agentworks.vms.applied_state import VMSSHIdentityPolicyError

    # Resolve each session's VM and group
    by_vm: dict[str, list[SessionRow]] = {}
    vm_admins: dict[str, VMAccess] = {}
    # A legacy row is always an admin's (the schema requires an agent
    # session's socket), so it reads the admin's default server on the
    # VM admin's access, like every other read here.
    legacy: set[str] = set()
    socket_owners: dict[str, str] = {}
    unavailable_vms: set[str] = set()
    result_map = {session.name: SessionStatus.UNKNOWN for session in sessions}

    for s in sessions:
        try:
            ws = _mgr._require_workspace(db, s.workspace_name)
            vm = _mgr._require_vm_for_workspace(db, ws)
        except NotFoundError:
            # Guard-free readers can race a normal workspace or VM deletion.
            continue
        if ws.vm_name in unavailable_vms:
            continue
        if ws.vm_name not in vm_admins:
            # One VM that cannot be reached must not abort the listing for the
            # others: report it once and leave that VM's sessions UNKNOWN.
            if not vm.tailscale_host:
                _warn_unavailable(
                    ws.vm_name,
                    StateError(
                        f"VM '{ws.vm_name}' has no Tailscale IP",
                        entity_kind="vm",
                        entity_name=ws.vm_name,
                        hint="VM init may not be complete. Check 'vm describe' for status.",
                    ),
                )
                unavailable_vms.add(ws.vm_name)
                continue
            try:
                from agentworks.vms.manager import require_vm_ssh_boundary

                require_vm_ssh_boundary(db, config, vm)
            except UserAbort:
                raise
            except (TrustMissing, ConfigError, ConnectivityError, VMSSHIdentityPolicyError) as exc:
                _warn_unavailable(ws.vm_name, exc)
                unavailable_vms.add(ws.vm_name)
                continue
            vm_admins[ws.vm_name] = _mgr._identity_access(vm, config, None, logger=None)
        if s.socket_path is not None:
            try:
                socket_owners[s.name] = _teardown._validated_socket(db, s).owner
            except StateError as exc:
                output.warn(f"Session status is unavailable: {exc}.")
                continue
        by_vm.setdefault(ws.vm_name, []).append(s)
        if include_legacy and s.socket_path is None:
            legacy.add(s.name)

    if not by_vm:
        return result_map

    def _check_vm(vm_name: str) -> tuple[dict[str, SessionStatus], AgentworksError | None]:
        """The VM's statuses, and the first error that left any of them unknown."""
        statuses = batch_check_status(by_vm[vm_name], admin=vm_admins[vm_name], owners=socket_owners)
        failure: AgentworksError | None = None
        for session in by_vm[vm_name]:
            if session.name not in legacy:
                continue
            try:
                presence = probe_tmux_session(
                    session.name,
                    run_command=access_run_command(
                        vm_admins[vm_name], deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS, elevated=False
                    ),
                    socket_path=None,
                )
            except UserAbort:
                raise
            except AgentworksError as error:
                failure = failure or error
                continue
            statuses[session.name] = {
                ProbeStatus.PRESENT: SessionStatus.RUNNING,
                ProbeStatus.ABSENT: SessionStatus.STOPPED,
                ProbeStatus.UNKNOWN: SessionStatus.UNKNOWN,
            }[presence]
        return statuses, failure

    tasks = {vm_name: partial(_check_vm, vm_name) for vm_name in by_vm}
    with cancelling_futures(tasks) as futures:
        for future in as_completed(futures):
            try:
                statuses, failure = future.result()
            except UserAbort:
                raise
            except AgentworksError as error:
                statuses, failure = {}, error
            result_map.update(statuses)
            if failure is not None:
                _warn_unavailable(futures[future], failure)

    return result_map


def _warn_unavailable(vm_name: str, error: AgentworksError) -> None:
    """Say once why a VM's session statuses, or some of them, read as unknown in a listing."""
    hint = f" {error.hint}" if error.hint else ""
    output.warn(f"Session status on VM '{vm_name}' is unavailable: {error}.{hint}")
