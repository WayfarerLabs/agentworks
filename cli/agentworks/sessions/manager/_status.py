"""Session liveness checks (single and batched)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
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
from agentworks.sessions.tmux import (
    ProbeStatus,
    access_run_command,
    probe_tmux_server,
    probe_tmux_session,
    runtime_probe_command,
    runtime_probe_payload,
    runtime_probe_stderr,
)
from agentworks.status_observation import GUEST_OBSERVATION_TIMEOUT_SECONDS, cancelling_futures

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.execution.access import VMAccess
    from agentworks.sessions.tmux import RunCommand

_PID_PRESENT_FACT = "present"
_PID_ABSENT_FACT = "absent"


def observation_run_command(admin: VMAccess, session: SessionRow) -> RunCommand:
    """The admin's bounded reads of ``session``'s runtime: each one dispatch within the observation deadline.

    An agent session's reads run as root, the admin's existing read boundary
    over an agent-owned server, even before startup shared its socket.
    """
    return access_run_command(
        admin, deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS, elevated=session.mode == SessionMode.AGENT.value
    )


def _pid_probe_script(pid: int) -> str:
    """Return a shell probe whose fact exists only after the shell started."""
    return f"if test -d /proc/{pid}; then printf {_PID_PRESENT_FACT}; else printf {_PID_ABSENT_FACT}; fi"


def _pid_presence_from_fact(returncode: object, fact: object, *, stderr: object = "") -> ProbeStatus:
    if returncode != 0 or not isinstance(fact, str) or not isinstance(stderr, str):
        return ProbeStatus.UNKNOWN
    if fact == _PID_PRESENT_FACT and not stderr:
        return ProbeStatus.PRESENT
    if fact == _PID_ABSENT_FACT and not stderr:
        return ProbeStatus.ABSENT
    return ProbeStatus.UNKNOWN


def _pid_presence(pid: int, *, run_command: RunCommand) -> ProbeStatus:
    """Probe a PID without treating a transport failure as process absence."""
    result = run_command(runtime_probe_command(_pid_probe_script(pid), "PRESENT"), check=False)
    stdout = runtime_probe_payload(getattr(result, "stdout", "") or "", "PRESENT")
    stderr = runtime_probe_stderr(getattr(result, "stderr", "") or "")
    return _pid_presence_from_fact(getattr(result, "returncode", None), stdout, stderr=stderr)


def _get_boot_id(run_command: RunCommand) -> str | None:
    """Read the current VM boot ID. Returns None on failure."""
    from agentworks.sessions.tmux import read_boot_id

    return read_boot_id(run_command)


def check_session_status(
    session: SessionRow,
    *,
    admin: VMAccess | None = None,
) -> SessionStatus:
    """Determine session status. Dispatches by session type.

    Every read runs on ``admin`` as one dispatch within the observation
    deadline (``observation_run_command``); one that does not complete raises
    its typed error, which callers read as an unknown state.

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
        if admin is None:
            raise TypeError("dedicated session status requires the admin's access")
        return _check_dedicated_session(session, admin=admin)
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


def _check_dedicated_session(session: SessionRow, *, admin: VMAccess) -> SessionStatus:
    """Sessions with their own tmux server and socket. Applies uniformly to
    admin and agent sessions after the env-and-secrets SDD migrated admin
    sessions to per-session sockets.
    """
    socket_path = session.socket_path
    assert socket_path is not None
    run_command = observation_run_command(admin, session)
    session_presence = probe_tmux_session(session.name, run_command=run_command, socket_path=socket_path)
    if session_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if session_presence is ProbeStatus.PRESENT:
        if session.pid not in {None, PID_STOPPED} or _pending_launch_is_owned(session, admin=admin):
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


def _pending_launch_is_owned(session: SessionRow, *, admin: VMAccess) -> bool:
    """Observe a dispatched attempt without adopting its native identity."""
    from agentworks.sessions.tmux import (
        _matches_launch_attempt,
        canonical_boot_id,
        capture_tmux_server_fingerprint,
        require_socket_path,
    )

    if session.pid is not None or session.run_id is None or session.socket_path is None:
        return False
    boot_id = canonical_boot_id(session.boot_id)
    if boot_id is None:
        return False
    run_command = observation_run_command(admin, session)
    try:
        stored_ticks = _mgr._validated_stored_start_ticks(session)
        require_socket_path(session.socket_path, run_command=run_command)
        probe = capture_tmux_server_fingerprint(run_command=run_command, socket_path=session.socket_path)
        observed = probe.fingerprint
        return (
            probe.status is ProbeStatus.PRESENT
            and observed is not None
            and observed.boot_id == boot_id
            and (stored_ticks is None or stored_ticks == observed.start_ticks)
            and _matches_launch_attempt(
                run_command=run_command,
                socket_path=session.socket_path,
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


def batch_status_request(sessions: list[SessionRow]) -> Script:
    """One VM's batch status probe: the loop program as root, the selected rows on its stdin."""
    return Script(
        runtime_probe_command(_batch_probe_command(), "BATCH"),
        Shell.SH,
        elevated=True,
        deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS,
        stdin=Bytes(_batch_probe_data(sessions).encode()),
    )


def batch_check_status(
    sessions: list[SessionRow],
    *,
    admin: VMAccess,
) -> dict[str, SessionStatus]:
    """Probe session presence together, verifying pending launch candidates separately.

    The probe is one request as root on ``admin``, within the observation
    deadline; one that does not complete raises its typed error. Returns
    {session_name: SessionStatus}. An unrecorded present server remains
    ``UNKNOWN`` unless its exact native launch identity matches the pending
    attempt. Observation never adopts that identity into the persisted row.
    """
    from agentworks.sessions.tmux import canonical_boot_id

    checkable = [s for s in sessions if s.socket_path is not None]
    if not checkable:
        return {}

    raw = admin.run(batch_status_request(checkable))
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
            if session.pid not in {None, PID_STOPPED} or _pending_launch_is_owned(session, admin=admin):
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


def _batch_probe_data(sessions: list[SessionRow]) -> str:
    """Encode selected session rows as non-sensitive stdin protocol data."""
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
                    pid,
                )
            )
        )
    return "\n".join(records) + "\n"


def _batch_probe_command() -> str:
    """Build the constant-size loop program for a VM's session rows; it runs as root, so no row elevates."""
    return (
        "hex() { od -An -tx1 | tr -d ' \\n'; }; "
        'decode() { printf %s "$1" | base64 -d; }; '
        "BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null); B=$?; "
        'BOOT_HEX=$(printf %s "$BOOT" | hex); '
        "while read -r N64 S64 PID; do "
        'N=$(decode "$N64") || exit 90; SOCK=$(decode "$S64") || exit 90; '
        'HERR=$(tmux -S "$SOCK" has-session -t "=$N" 2>&1 >/dev/null); H=$?; '
        'HHEX=$(printf %s "$HERR" | hex); '
        'SERR=$(tmux -S "$SOCK" list-sessions 2>&1 >/dev/null); S=$?; '
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
    result = SimpleNamespace(returncode=code, stdout="", stderr=diagnostic)
    return _tmux_presence_from_result(result, missing_target_is_absent=missing_target_is_absent)


def observe_session_statuses(
    sessions: list[SessionRow],
    *,
    db: Database,
    config: Config,
    include_legacy: bool = False,
) -> dict[str, SessionStatus]:
    """Observe selected sessions, optionally including legacy shared-server rows."""
    from concurrent.futures import as_completed
    from functools import partial

    from agentworks.vms.applied_state import VMSSHIdentityPolicyError

    # Resolve each session's VM and group
    by_vm: dict[str, list[SessionRow]] = {}
    vm_admins: dict[str, VMAccess] = {}
    legacy_owners: dict[str, VMAccess] = {}
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
            if not vm.tailscale_host:
                continue
            try:
                from agentworks.vms.manager import require_vm_ssh_boundary

                require_vm_ssh_boundary(db, config, vm)
            except UserAbort:
                raise
            except TrustMissing as exc:
                # One VM's missing host key must not abort the listing for the
                # others: report it once and leave that VM's sessions UNKNOWN.
                output.warn(f"Session status on VM '{ws.vm_name}' is unavailable: {exc}. {exc.hint}")
                unavailable_vms.add(ws.vm_name)
                continue
            except (ConfigError, ConnectivityError, VMSSHIdentityPolicyError):
                unavailable_vms.add(ws.vm_name)
                continue
            vm_admins[ws.vm_name] = _mgr._identity_access(vm, config, None, logger=None)
        by_vm.setdefault(ws.vm_name, []).append(s)
        if include_legacy and s.socket_path is None:
            try:
                legacy_owners[s.name] = _mgr._session_owner_access(
                    s, vm=vm, config=config, db=db, admin=vm_admins[ws.vm_name], logger=None
                )
            except UserAbort:
                raise
            except AgentworksError:
                continue

    if not by_vm:
        return result_map

    def _check_vm(vm_name: str) -> dict[str, SessionStatus]:
        statuses = batch_check_status(by_vm[vm_name], admin=vm_admins[vm_name])
        for session in by_vm[vm_name]:
            owner = legacy_owners.get(session.name)
            if owner is None:
                continue
            try:
                presence = probe_tmux_session(
                    session.name,
                    run_command=access_run_command(owner, deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS, elevated=False),
                    socket_path=None,
                )
            except UserAbort:
                raise
            except AgentworksError:
                continue
            statuses[session.name] = {
                ProbeStatus.PRESENT: SessionStatus.RUNNING,
                ProbeStatus.ABSENT: SessionStatus.STOPPED,
                ProbeStatus.UNKNOWN: SessionStatus.UNKNOWN,
            }[presence]
        return statuses

    tasks = {vm_name: partial(_check_vm, vm_name) for vm_name in by_vm}
    with cancelling_futures(tasks) as futures:
        for future in as_completed(futures):
            try:
                result_map.update(future.result())
            except UserAbort:
                raise
            except AgentworksError:
                continue

    return result_map
