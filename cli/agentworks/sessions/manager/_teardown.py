"""Dedicated session teardown and batch coordination."""

from __future__ import annotations

import shlex
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as _wait_for_futures
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import PurePosixPath
from threading import Event
from time import monotonic, sleep
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import PID_STOPPED, SessionMode
from agentworks.errors import BrokenStateError, ConnectivityError, ExternalError, StateError
from agentworks.sessions.tmux import ADMIN_SOCKET_ROOT, AGENT_SOCKET_ROOT, ProbeStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.db import Database, SessionRow
    from agentworks.transports import Transport


DEDICATED_TEARDOWN_MAX_WORKERS = 8
DEDICATED_TEARDOWN_TIMEOUT_SECONDS = 10
DEDICATED_TEARDOWN_ATTEMPTS = 1
DEDICATED_TEARDOWN_HEARTBEAT_SECONDS = 5
DEDICATED_TEARDOWN_EXIT_GRACE_SECONDS = 1
DEDICATED_TEARDOWN_EXIT_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class DedicatedTeardownPlan:
    """Complete immutable authority for one dedicated remote teardown."""

    session_name: str
    session_uuid: str
    run_id: str | None
    vm_name: str
    socket_path: str
    stored_pid: int
    stored_boot_id: str
    stored_start_ticks: int
    target: Transport
    sudo: bool
    force: bool


class DedicatedTeardownEvidence(Enum):
    """Evidence returned only after the exact runtime is verified absent."""

    VERIFIED_STOPPED = "verified-stopped"


class _SubmissionGate:
    """Keep remote mutation behind complete future ownership."""

    def __init__(self) -> None:
        self._aborted = Event()
        self._released = Event()

    def release(self) -> None:
        self._released.set()

    def abort(self) -> None:
        self._aborted.set()
        self._released.set()

    def permits_mutation(self) -> bool:
        self._released.wait()
        return not self._aborted.is_set()


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


def _validated_pid(session: SessionRow) -> int:
    """Return a positive stored PID for a concurrency-eligible row."""
    pid = session.pid
    if pid is None or isinstance(pid, bool) or pid <= 0:
        raise StateError(
            f"session '{session.name}' does not have a positive stored tmux server PID",
            entity_kind="session",
            entity_name=session.name,
            hint="Repair the persisted runtime identity before retrying.",
        )
    return pid


def prepare_concurrent_dedicated_teardown(
    db: Database,
    session: SessionRow,
    *,
    vm_name: str,
    target: Transport,
    target_owns_session: bool,
    force: bool,
) -> DedicatedTeardownPlan:
    """Prepare complete dedicated work on the invoking thread.

    Persisted rows are a process boundary. The coordinator selects only
    complete-fingerprint rows for this function; any contract violation fails
    closed instead of inventing work.
    """
    socket_path = _validated_socket_path(db, session)
    pid = _validated_pid(session)
    if session.boot_id is None or session.tmux_server_start_ticks is None:
        raise StateError(
            f"session '{session.name}' does not have a complete stored tmux server fingerprint",
            entity_kind="session",
            entity_name=session.name,
            hint="Inspect and repair the persisted session runtime before retrying.",
        )
    boot_id = _mgr._validated_stored_boot_id(session)
    start_ticks = _mgr._validated_stored_start_ticks(session)
    assert start_ticks is not None
    return DedicatedTeardownPlan(
        session_name=session.name,
        session_uuid=session.session_uuid,
        run_id=session.run_id,
        vm_name=vm_name,
        socket_path=socket_path,
        stored_pid=pid,
        stored_boot_id=boot_id,
        stored_start_ticks=start_ticks,
        target=target,
        sudo=not target_owns_session,
        force=force,
    )


def validate_concurrent_dedicated_teardown_plans(plans: Sequence[DedicatedTeardownPlan]) -> None:
    """Refuse two workers that claim one socket or process authority."""
    socket_owners: dict[tuple[str, str], str] = {}
    process_owners: dict[tuple[str, str, int], str] = {}
    conflicting_names: set[str] = set()
    for plan in plans:
        socket_key = (plan.vm_name, plan.socket_path)
        socket_owner = socket_owners.get(socket_key)
        if socket_owner is not None:
            conflicting_names.update((socket_owner, plan.session_name))
        else:
            socket_owners[socket_key] = plan.session_name

        process_key = (plan.vm_name, plan.stored_boot_id, plan.stored_pid)
        process_owner = process_owners.get(process_key)
        if process_owner is not None:
            conflicting_names.update((process_owner, plan.session_name))
        else:
            process_owners[process_key] = plan.session_name

    if conflicting_names:
        names = ", ".join(sorted(conflicting_names))
        raise StateError(
            f"selected sessions claim overlapping tmux runtime identities ({names})",
            entity_kind="session",
            hint="Repair the conflicting persisted runtime identities before retrying.",
        )


def _prove_plan_runtime_absent(plan: DedicatedTeardownPlan) -> bool:
    """Prove the prepared process incarnation absent without SQLite access."""
    from agentworks.sessions.manager._pids import _prove_runtime_absent

    return _prove_runtime_absent(
        session_name=plan.session_name,
        stored_pid=plan.stored_pid,
        stored_boot_id=plan.stored_boot_id,
        stored_start_ticks=plan.stored_start_ticks,
        target=plan.target,
        sudo=plan.sudo,
    )


def _remove_stale_socket(
    *,
    session_name: str,
    socket_path: str,
    target: Transport,
    sudo: bool,
) -> None:
    """Remove and verify only the prepared exact managed socket."""
    from agentworks.sessions.tmux import _test_presence_from_result

    q_socket = shlex.quote(socket_path)
    removed = target.run(
        f"if test -L {q_socket} || {{ test -e {q_socket} && ! test -S {q_socket}; }}; then exit 2; fi; "
        f"if test -S {q_socket}; then rm -f -- {q_socket}; fi",
        sudo=sudo,
        check=False,
    )
    if _test_presence_from_result(removed) is not ProbeStatus.PRESENT:
        raise ExternalError(
            f"failed to remove stale tmux socket for session '{session_name}'",
            entity_kind="session",
            entity_name=session_name,
        )
    remains = _test_presence_from_result(target.run(f"test -e {q_socket}", sudo=sudo, check=False))
    if remains is not ProbeStatus.ABSENT:
        raise ExternalError(
            f"could not verify stale tmux socket removal for session '{session_name}'",
            entity_kind="session",
            entity_name=session_name,
        )


def _execute_reachable_dedicated_teardown(
    plan: DedicatedTeardownPlan,
    *,
    observed_pid: int,
    observed_boot_id: str,
    observed_start_ticks: int,
) -> DedicatedTeardownEvidence:
    """Destroy one already-captured exact process incarnation."""
    from agentworks.sessions.tmux import kill_server

    if (
        observed_pid != plan.stored_pid
        or observed_boot_id != plan.stored_boot_id
        or observed_start_ticks != plan.stored_start_ticks
    ):
        raise BrokenStateError(
            f"session '{plan.session_name}' tmux server identity does not match persisted state",
            entity_kind="session",
            entity_name=plan.session_name,
        )

    def run_runtime(command: str, *, check: bool = True, env: dict[str, str] | None = None) -> object:
        return plan.target.run(command, sudo=plan.sudo, check=check, env=env)

    kill_server(run_command=run_runtime, socket_path=plan.socket_path)
    # Acknowledging kill-server can precede the server finishing its exit.
    # Bound further observations, without retrying mutation or uncertain probes.
    deadline = monotonic() + DEDICATED_TEARDOWN_EXIT_GRACE_SECONDS
    absent = _prove_plan_runtime_absent(plan)
    while not absent:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(DEDICATED_TEARDOWN_EXIT_POLL_SECONDS, remaining))
        if monotonic() >= deadline:
            break
        absent = _prove_plan_runtime_absent(plan)
    if not absent:
        raise ExternalError(
            f"tmux server for session '{plan.session_name}' survived kill-server",
            entity_kind="session",
            entity_name=plan.session_name,
        )
    _remove_stale_socket(
        session_name=plan.session_name,
        socket_path=plan.socket_path,
        target=plan.target,
        sudo=plan.sudo,
    )
    return DedicatedTeardownEvidence.VERIFIED_STOPPED


def execute_dedicated_teardown(plan: DedicatedTeardownPlan) -> DedicatedTeardownEvidence:
    """Run one complete dedicated remote teardown without SQLite or output."""
    from agentworks.sessions.tmux import capture_tmux_server_fingerprint, require_socket_path

    require_socket_path(plan.socket_path, run_command=partial(plan.target.run, sudo=plan.sudo))

    probe = capture_tmux_server_fingerprint(
        target=plan.target,
        socket_path=plan.socket_path,
        sudo=plan.sudo,
    )
    if probe.status is not ProbeStatus.PRESENT:
        try:
            if not _prove_plan_runtime_absent(plan):
                raise BrokenStateError(
                    f"session '{plan.session_name}' may still own a live tmux server; refusing stale cleanup",
                    entity_kind="session",
                    entity_name=plan.session_name,
                    hint="Recover the tmux runtime manually before retrying.",
                )
        except (BrokenStateError, ConnectivityError, StateError):
            if not plan.force:
                raise BrokenStateError(
                    f"session '{plan.session_name}' tmux server is unreachable or indeterminate",
                    entity_kind="session",
                    entity_name=plan.session_name,
                    hint="Use --force only after the prior server has exited.",
                ) from None
            raise
        _remove_stale_socket(
            session_name=plan.session_name,
            socket_path=plan.socket_path,
            target=plan.target,
            sudo=plan.sudo,
        )
        return DedicatedTeardownEvidence.VERIFIED_STOPPED

    observed = probe.fingerprint
    assert observed is not None
    from agentworks.sessions.tmux import canonical_boot_id

    observed_boot_id = canonical_boot_id(observed.boot_id)
    if observed_boot_id is None:
        raise StateError(
            f"session '{plan.session_name}' has an invalid observed VM boot identity",
            entity_kind="session",
            entity_name=plan.session_name,
        )
    return _execute_reachable_dedicated_teardown(
        plan,
        observed_pid=observed.pid,
        observed_boot_id=observed_boot_id,
        observed_start_ticks=observed.start_ticks,
    )


def teardown_dedicated_session(
    session: SessionRow,
    *,
    target: Transport,
    target_owns_session: bool,
    db: Database,
    force: bool,
) -> None:
    """Run the synchronous dedicated compatibility path."""
    from agentworks.sessions.tmux import _matches_launch_attempt, capture_tmux_server_fingerprint, require_socket_path

    socket_path = _validated_socket_path(db, session)
    sudo = not target_owns_session
    require_socket_path(socket_path, run_command=partial(target.run, sudo=sudo))
    probe = capture_tmux_server_fingerprint(target=target, socket_path=socket_path, sudo=sudo)
    if session.pid == PID_STOPPED and probe.status is ProbeStatus.ABSENT:
        _remove_stale_socket(session_name=session.name, socket_path=socket_path, target=target, sudo=sudo)
        _mark_stopped(db, session)
        return
    if probe.status is not ProbeStatus.PRESENT:
        try:
            if not _mgr._prove_stored_runtime_absent(session, target=target, sudo=sudo):
                raise BrokenStateError(
                    f"session '{session.name}' may still own a live tmux server; refusing stale cleanup",
                    entity_kind="session",
                    entity_name=session.name,
                    hint="Recover the tmux runtime manually before retrying.",
                )
        except (BrokenStateError, StateError):
            if not force:
                raise BrokenStateError(
                    f"session '{session.name}' tmux server is unreachable or indeterminate",
                    entity_kind="session",
                    entity_name=session.name,
                    hint="Use --force only after the prior server has exited.",
                ) from None
            raise
        _remove_stale_socket(
            session_name=session.name,
            socket_path=socket_path,
            target=target,
            sudo=sudo,
        )
        _mark_stopped(db, session)
        return

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
            sudo=sudo,
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
        refreshed = db.get_session(session.name)
        assert refreshed is not None
        session = refreshed
        stored_ticks = observed.start_ticks
    elif observed.start_ticks != stored_ticks:
        raise BrokenStateError(
            f"session '{session.name}' tmux server process was replaced",
            entity_kind="session",
            entity_name=session.name,
        )

    plan = DedicatedTeardownPlan(
        session_name=session.name,
        session_uuid=session.session_uuid,
        run_id=session.run_id,
        vm_name=_mgr._require_workspace(db, session.workspace_name).vm_name,
        socket_path=socket_path,
        stored_pid=observed.pid,
        stored_boot_id=observed_boot_id,
        stored_start_ticks=stored_ticks,
        target=target,
        sudo=sudo,
        force=force,
    )
    _execute_reachable_dedicated_teardown(
        plan,
        observed_pid=observed.pid,
        observed_boot_id=observed_boot_id,
        observed_start_ticks=observed.start_ticks,
    )
    _mark_stopped(db, session)


def _mark_stopped(db: Database, session: SessionRow) -> None:
    db.update_session_runtime(
        session.name,
        socket_path=session.socket_path,
        pid=PID_STOPPED,
        boot_id=None,
        tmux_server_start_ticks=None,
    )


def reconcile_dedicated_teardown(db: Database, plan: DedicatedTeardownPlan) -> None:
    """Persist verified stopped evidence on the invoking thread."""
    db.compare_and_set_session_stopped(
        plan.session_name,
        expected_session_uuid=plan.session_uuid,
        expected_run_id=plan.run_id,
        expected_socket_path=plan.socket_path,
        expected_pid=plan.stored_pid,
        expected_boot_id=plan.stored_boot_id,
        expected_tmux_server_start_ticks=plan.stored_start_ticks,
    )


def _execute_gated(
    gate: _SubmissionGate,
    plan: DedicatedTeardownPlan,
) -> DedicatedTeardownEvidence | None:
    if not gate.permits_mutation():
        return None
    return execute_dedicated_teardown(plan)


def _shutdown_after_submission_abort(executor: ThreadPoolExecutor, gate: _SubmissionGate) -> None:
    """Abort every wrapper and keep later interrupts from masking the first."""
    while True:
        try:
            gate.abort()
            executor.shutdown(wait=True, cancel_futures=True)
            return
        except KeyboardInterrupt:
            continue


def _announce_reconciliation(error: BaseException) -> None:
    if isinstance(error, KeyboardInterrupt):
        output.notice("Session stop interrupted; reconciling active teardowns before exit.")
    else:
        output.notice("Session stop failed; reconciling active teardowns before exit.")


def _enter_reconciliation(
    futures: Sequence[Future[DedicatedTeardownEvidence | None]],
    error: BaseException,
    *,
    cancel_queued: bool,
) -> None:
    """Enter or report drain mode without letting later interrupts escape."""
    while True:
        try:
            if cancel_queued:
                _cancel_queued(futures)
                cancel_queued = False
            _announce_reconciliation(error)
            return
        except KeyboardInterrupt as later_interrupt:
            error = later_interrupt
            cancel_queued = True
        except BaseException:
            # The primary failure is already recorded. A broken output sink
            # cannot be allowed to bypass executor draining.
            return


def _release_gate_after_escape(gate: _SubmissionGate) -> None:
    """Authorize the complete mapped batch despite repeated interrupts."""
    while True:
        try:
            gate.release()
            return
        except KeyboardInterrupt:
            continue


def _cancel_queued(futures: Sequence[Future[DedicatedTeardownEvidence | None]]) -> None:
    for future in futures:
        if not future.done():
            future.cancel()


def _heartbeat(
    futures: Sequence[Future[DedicatedTeardownEvidence | None]],
) -> None:
    completed = sum(future.done() for future in futures)
    active = sum(future.running() for future in futures)
    queued = len(futures) - completed - active
    output.info(f"Session teardown progress: {completed} completed, {active} active, {queued} queued.")


def execute_concurrent_dedicated_teardowns(
    plans: Sequence[DedicatedTeardownPlan],
    *,
    db: Database,
) -> list[tuple[str, str]]:
    """Execute complete dedicated plans concurrently and reconcile serially."""
    if not plans:
        return []
    validate_concurrent_dedicated_teardown_plans(plans)

    gate = _SubmissionGate()
    failures: list[tuple[str, str]] = []
    pending: set[Future[DedicatedTeardownEvidence | None]] = set()
    primary_error: BaseException | None = None
    reconciling = False
    executor = ThreadPoolExecutor(max_workers=min(DEDICATED_TEARDOWN_MAX_WORKERS, len(plans)))
    future_plans: dict[Future[DedicatedTeardownEvidence | None], DedicatedTeardownPlan] = {}
    future_map_complete = False
    try:
        for plan in plans:
            future = executor.submit(_execute_gated, gate, plan)
            future_plans[future] = plan
            pending.add(future)
        future_map_complete = True
        gate.release()
    except BaseException as exc:
        if not future_map_complete:
            _shutdown_after_submission_abort(executor, gate)
            raise
        primary_error = exc
        _release_gate_after_escape(gate)
        reconciling = True
        _enter_reconciliation(tuple(pending), exc, cancel_queued=True)

    while pending:
        try:
            done, _unfinished = _wait_for_futures(
                pending,
                timeout=DEDICATED_TEARDOWN_HEARTBEAT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                _heartbeat(tuple(future_plans))
                continue

            for future in done:
                if future.cancelled():
                    pending.remove(future)
                    continue
                plan = future_plans[future]
                try:
                    evidence = future.result()
                except Exception as exc:
                    failures.append((plan.session_name, str(exc)))
                    pending.remove(future)
                    output.warn(f"Session '{plan.session_name}' failed to stop: {exc}")
                    continue
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
                    pending.remove(future)
                    output.warn(f"Session '{plan.session_name}' teardown aborted: {exc}")
                    if not reconciling:
                        reconciling = True
                        _enter_reconciliation(tuple(pending), exc, cancel_queued=True)
                    else:
                        _enter_reconciliation(tuple(pending), exc, cancel_queued=False)
                    continue

                if evidence is not DedicatedTeardownEvidence.VERIFIED_STOPPED:
                    invalid_evidence = StateError(
                        f"session '{plan.session_name}' teardown returned no verified stopped evidence",
                        entity_kind="session",
                        entity_name=plan.session_name,
                    )
                    failures.append((plan.session_name, str(invalid_evidence)))
                    pending.remove(future)
                    output.warn(f"Session '{plan.session_name}' failed to stop: {invalid_evidence}")
                    continue

                try:
                    reconcile_dedicated_teardown(db, plan)
                except Exception as exc:
                    failures.append((plan.session_name, str(exc)))
                    pending.remove(future)
                    output.warn(f"Session '{plan.session_name}' failed to persist stopped state: {exc}")
                    continue
                except KeyboardInterrupt as exc:
                    if primary_error is None:
                        primary_error = exc
                    if not reconciling:
                        reconciling = True
                        _enter_reconciliation(tuple(pending), exc, cancel_queued=True)
                    else:
                        _enter_reconciliation(tuple(pending), exc, cancel_queued=False)
                    break
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
                    pending.remove(future)
                    output.warn(f"Session '{plan.session_name}' stopped-state persistence aborted: {exc}")
                    if not reconciling:
                        reconciling = True
                        _enter_reconciliation(tuple(pending), exc, cancel_queued=True)
                    else:
                        _enter_reconciliation(tuple(pending), exc, cancel_queued=False)
                    continue
                pending.remove(future)
                output.info(f"Session '{plan.session_name}' stopped")
        except BaseException as exc:
            if primary_error is None:
                primary_error = exc
            if not reconciling:
                reconciling = True
                _enter_reconciliation(tuple(pending), exc, cancel_queued=True)
            else:
                _enter_reconciliation(tuple(pending), exc, cancel_queued=False)

    while True:
        try:
            executor.shutdown(wait=True, cancel_futures=False)
            break
        except KeyboardInterrupt as exc:
            if primary_error is None:
                primary_error = exc
            _enter_reconciliation((), exc, cancel_queued=False)

    if primary_error is not None:
        raise primary_error
    return failures
