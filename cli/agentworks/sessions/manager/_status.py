"""Session liveness checks (single and batched)."""

from __future__ import annotations

import base64
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

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
from agentworks.sessions.tmux import (
    ProbeStatus,
    probe_tmux_server,
    probe_tmux_session,
    runtime_probe_command,
    runtime_probe_payload,
    runtime_probe_stderr,
    server_owner_run_command,
    socket_owner,
)
from agentworks.status_observation import (
    GUEST_OBSERVATION_ATTEMPTS,
    GUEST_OBSERVATION_TIMEOUT_SECONDS,
    cancelling_futures,
)

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.transports import Transport

# Bounds each tmux client in the batch probe; well inside the request's own
# GUEST_OBSERVATION_TIMEOUT_SECONDS.
_BATCH_CLIENT_TIMEOUT_SECONDS = 2

_PID_PRESENT_FACT = "present"
_PID_ABSENT_FACT = "absent"


class _BoundedStatusTarget:
    """Force singular inspection calls onto the batch probe's safe policy."""

    def __init__(self, target: Transport) -> None:
        self._target = target

    def run(self, command: str, **kwargs: Any) -> Any:
        kwargs.update(
            timeout=GUEST_OBSERVATION_TIMEOUT_SECONDS,
            retries=GUEST_OBSERVATION_ATTEMPTS,
            tty=False,
        )
        return self._target.run(command, **kwargs)


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


def _pid_presence(pid: int, *, target: Transport, sudo: bool = False) -> ProbeStatus:
    """Probe a PID without treating a transport failure as process absence."""
    result = target.run(runtime_probe_command(_pid_probe_script(pid), "PRESENT"), sudo=sudo, check=False)
    stdout = runtime_probe_payload(getattr(result, "stdout", "") or "", "PRESENT")
    stderr = runtime_probe_stderr(getattr(result, "stderr", "") or "")
    return _pid_presence_from_fact(getattr(result, "returncode", None), stdout, stderr=stderr)


def _session_tmux_owner(session: SessionRow, *, admin_username: str) -> str | None:
    """Return the account the VM admin's transport must act as on this session's server.

    Applies ``socket_owner`` to the persisted socket path: ``None`` for the
    admin's own server, the agent account for an agent's. Raises
    ``StateError`` for a row without a socket path, a path the rule refuses,
    or a path whose owner contradicts the row's mode.
    """
    socket_path = session.socket_path
    owner = socket_owner(socket_path, admin_username=admin_username) if socket_path is not None else None
    if socket_path is None or (owner is not None) != (session.mode == SessionMode.AGENT.value):
        raise StateError(
            f"session '{session.name}' has no managed tmux socket for its mode",
            entity_kind="session",
            entity_name=session.name,
            hint="Repair the persisted session row before retrying.",
        )
    return owner


def _recorded_session_tmux_owner(db: Database, session: SessionRow) -> str | None:
    """``_session_tmux_owner`` with the VM admin and agent read from the database.

    For callers holding a database handle: the owner must also be the
    session's agent row's account.
    """
    workspace = db.get_workspace(session.workspace_name)
    vm = db.get_vm(workspace.vm_name) if workspace is not None else None
    if vm is None:
        raise StateError(
            f"session '{session.name}' has no VM to resolve its tmux server owner",
            entity_kind="session",
            entity_name=session.name,
        )
    owner = _session_tmux_owner(session, admin_username=vm.admin_username)
    if owner is not None:
        agent = db.get_agent(session.agent_name or "")
        if agent is None or agent.linux_user != owner:
            raise StateError(
                f"session '{session.name}' has a tmux socket outside its agent's directory",
                entity_kind="session",
                entity_name=session.name,
                hint="Repair the persisted session row before retrying.",
            )
    return owner


def _has_recorded_tmux_owner(db: Database, session: SessionRow) -> bool:
    """Whether a socket-bearing row passes ``_recorded_session_tmux_owner``; read-only observers skip it if not."""
    try:
        _recorded_session_tmux_owner(db, session)
    except StateError:
        return False
    return True


def _get_boot_id(target: Transport) -> str | None:
    """Read the current VM boot ID. Returns None on failure."""
    from agentworks.sessions.tmux import read_boot_id

    return read_boot_id(target)


def check_session_status(
    session: SessionRow,
    *,
    target: Transport | None = None,
    admin_username: str | None = None,
) -> SessionStatus:
    """Determine session status. Dispatches by session type.

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
        if target is None or admin_username is None:
            raise TypeError("dedicated session status requires a transport and the VM admin")
        return _check_dedicated_session(session, target=target, admin_username=admin_username)
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


def _check_dedicated_session(session: SessionRow, *, target: Transport, admin_username: str) -> SessionStatus:
    """Sessions with their own tmux server and socket. Applies uniformly to
    admin and agent sessions after the env-and-secrets SDD migrated admin
    sessions to per-session sockets.
    """
    socket_path = session.socket_path
    assert socket_path is not None
    # Match batch observation and fingerprint repair: admin inspection of an
    # agent server runs its tmux clients as the agent, which works even
    # before startup completed its shared socket permissions.
    try:
        owner = _session_tmux_owner(session, admin_username=admin_username)
    except StateError:
        return SessionStatus.UNKNOWN
    run_command = server_owner_run_command(target, owner)
    session_presence = probe_tmux_session(session.name, run_command=run_command, socket_path=socket_path)
    if session_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if session_presence is ProbeStatus.PRESENT:
        if session.pid not in {None, PID_STOPPED} or _pending_launch_is_owned(
            session, target=target, admin_username=admin_username
        ):
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
    current_boot = _mgr._get_boot_id(target)
    if current_boot is None:
        return SessionStatus.UNKNOWN  # can't verify boot cycle, unsafe to offer --force
    stored_boot_id = _mgr._validated_stored_boot_id(session)
    if stored_boot_id != current_boot:
        return SessionStatus.STOPPED  # stale boot, PID is meaningless
    if session.pid is None or session.pid <= 0:
        return SessionStatus.UNKNOWN
    pid_presence = _pid_presence(
        session.pid,
        target=target,
        sudo=owner is not None,
    )
    if pid_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if pid_presence is ProbeStatus.ABSENT:
        return SessionStatus.STOPPED  # process is dead
    return SessionStatus.BROKEN  # same boot, process alive, socket unreachable


def _pending_launch_is_owned(session: SessionRow, *, target: Transport, admin_username: str) -> bool:
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
    bounded = cast("Transport", _BoundedStatusTarget(target))
    try:
        owner = _session_tmux_owner(session, admin_username=admin_username)
        stored_ticks = _mgr._validated_stored_start_ticks(session)
        require_socket_path(session.socket_path, run_command=partial(bounded.run, sudo=owner is not None))
        probe = capture_tmux_server_fingerprint(target=bounded, socket_path=session.socket_path, owner=owner)
        observed = probe.fingerprint
        return (
            probe.status is ProbeStatus.PRESENT
            and observed is not None
            and observed.boot_id == boot_id
            and (stored_ticks is None or stored_ticks == observed.start_ticks)
            and _matches_launch_attempt(
                target=bounded,
                socket_path=session.socket_path,
                session_name=session.name,
                session_uuid=session.session_uuid,
                run_id=session.run_id,
                fingerprint=observed,
                owner=owner,
            )
        )
    except UserAbort:
        raise
    except (AgentworksError, OSError):
        return False


def batch_check_status(
    sessions: list[SessionRow],
    *,
    target: Transport,
    admin_username: str,
) -> dict[str, SessionStatus]:
    """Probe session presence together, verifying pending launch candidates separately.

    Returns {session_name: SessionStatus}. An unrecorded present server remains
    ``UNKNOWN`` unless its exact native launch identity matches the pending
    attempt. Observation never adopts that identity into the persisted row.
    """
    from agentworks.sessions.tmux import canonical_boot_id

    checkable = [s for s in sessions if s.socket_path is not None]
    if not checkable:
        return {}
    # A row whose server owner cannot be established is never probed and
    # stays UNKNOWN, like any other indeterminate observation.
    owners: dict[str, str | None] = {}
    for session in checkable:
        try:
            owners[session.name] = _session_tmux_owner(session, admin_username=admin_username)
        except StateError:
            continue
    probed = [(session, owners[session.name]) for session in checkable if session.name in owners]

    cmd = runtime_probe_command(_batch_probe_command(), "BATCH")
    result = target.run(
        cmd,
        check=False,
        timeout=GUEST_OBSERVATION_TIMEOUT_SECONDS,
        retries=GUEST_OBSERVATION_ATTEMPTS,
        tty=False,
        input_data=_batch_probe_data(probed),
    )
    stdout = runtime_probe_payload(getattr(result, "stdout", "") or "", "BATCH")

    status_map: dict[str, SessionStatus] = {
        session.name: SessionStatus.UNKNOWN for session in checkable if session.socket_path is not None
    }
    sessions_by_key = {_encoded_probe_field(session.name): session for session, _owner in probed}

    if (
        result.returncode != 0
        or runtime_probe_stderr(result.stderr) != ""
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
            if session.pid not in {None, PID_STOPPED} or _pending_launch_is_owned(
                session, target=target, admin_username=admin_username
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


def _batch_probe_data(sessions: list[tuple[SessionRow, str | None]]) -> str:
    """Encode selected session rows and their server owners as non-sensitive stdin protocol data.

    Each record is ``name socket pid owner``. ``owner`` is ``-`` for the
    admin's own servers; otherwise it names the account each probe of that
    row runs as.
    """
    records = []
    for session, owner in sessions:
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
                    _encoded_probe_field(owner) if owner is not None else "-",
                )
            )
        )
    return "\n".join(records) + "\n"


def _batch_probe_command() -> str:
    """Build the constant-size loop program for a VM's session rows."""
    from agentworks.sessions.tmux import RUNUSER_PATH

    client = f"timeout -k 1 {_BATCH_CLIENT_TIMEOUT_SECONDS}"
    return (
        "hex() { od -An -tx1 | tr -d ' \\n'; }; "
        'decode() { printf %s "$1" | base64 -d; }; '
        "BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null); B=$?; "
        'BOOT_HEX=$(printf %s "$BOOT" | hex); '
        "while read -r N64 S64 PID O64; do "
        'N=$(decode "$N64") || exit 90; SOCK=$(decode "$S64") || exit 90; '
        'if [ "$O64" != - ]; then OWNER=$(decode "$O64") || exit 90; fi; '
        # Each client is bounded, so one stuck server costs only its own row,
        # and takes no stdin, so the remaining rows stay out of every
        # server's reach.
        f'run_tmux() {{ if [ "$O64" != - ]; then {client} sudo -n {RUNUSER_PATH} -u "$OWNER" -- tmux -S "$SOCK" "$@" '
        f'</dev/null; else {client} tmux -S "$SOCK" "$@" </dev/null; fi; }}; '
        'HERR=$(run_tmux has-session -t "=$N" 2>&1 >/dev/null); H=$?; '
        'HHEX=$(printf %s "$HERR" | hex); '
        "SERR=$(run_tmux list-sessions 2>&1 >/dev/null); S=$?; "
        'SHEX=$(printf %s "$SERR" | hex); '
        'if [ "$PID" = - ]; then PFACT=; P=1; '
        'elif [ "$O64" != - ]; then '
        'PFACT=$(sudo -n sh -c "if test -d /proc/$PID; then printf present; '
        'else printf absent; fi" 2>/dev/null); P=$?; '
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
    vm_targets: dict[str, Transport] = {}
    vm_admins: dict[str, str] = {}
    legacy_targets: dict[str, Transport] = {}
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
        if ws.vm_name not in vm_targets:
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
            try:
                vm_targets[ws.vm_name] = _mgr.transport(vm, config)
                vm_admins[ws.vm_name] = vm.admin_username
            except UserAbort:
                raise
            except (ConnectivityError, StateError):
                unavailable_vms.add(ws.vm_name)
                continue
        if s.socket_path is not None and not _has_recorded_tmux_owner(db, s):
            # Never probed: a socket outside its owner's directory stays UNKNOWN.
            continue
        by_vm.setdefault(ws.vm_name, []).append(s)
        if include_legacy and s.socket_path is None:
            try:
                legacy_targets[s.name] = _mgr._build_session_target(
                    s,
                    vm=vm,
                    config=config,
                    db=db,
                    admin_target=vm_targets[ws.vm_name],
                )
            except UserAbort:
                raise
            except AgentworksError:
                continue

    if not by_vm:
        return result_map

    def _check_vm(vm_name: str) -> dict[str, SessionStatus]:
        statuses = batch_check_status(by_vm[vm_name], target=vm_targets[vm_name], admin_username=vm_admins[vm_name])
        for session in by_vm[vm_name]:
            target = legacy_targets.get(session.name)
            if target is None:
                continue
            try:
                presence = probe_tmux_session(
                    session.name,
                    run_command=_BoundedStatusTarget(target).run,
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
