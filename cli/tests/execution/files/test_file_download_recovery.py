"""Local-substrate checks for the private DOWNLOAD recovery boundary."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligation, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.execution._file_download_recovery import (
    FileDownloadRecovery,
    _DownloadDrainEvidence,
)
from agentworks.execution._file_obligation import (
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    FileCallUncertainty,
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._file_snapshot_exchange import (
    FileSnapshotObservationState,
    snapshot_begin,
    snapshot_reconcile,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution.carrier import (
    CarrierReport,
    Deadline,
    PreparedInvocation,
)
from agentworks.operations import LifecycleObligation as OwnerLifecycleObligation
from agentworks.operations import OperationOwner
from tests.execution.files._file_snapshot_support import LocalCarrier, fixture_source, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private snapshot helper requires Linux")


class _LocalHelperDrainRecord:
    """One test-journal observation for a helper tied to a DOWNLOAD token."""

    def __init__(self, helper_id: str, exited: bool) -> None:
        self.helper_id = helper_id
        self.exited = exited


def _local_drain_evidence(
    ownership,
    obligation: LifecycleObligation,
    call: FileCallObligation,
    helper_records: tuple[_LocalHelperDrainRecord, ...],
) -> _DownloadDrainEvidence:
    """Model an adapter journal that proved every exact helper has exited."""
    if call.family is not FileCallFamily.DOWNLOAD or call.token is None:
        raise ValueError("local drain evidence requires one DOWNLOAD token")
    if not helper_records or any(not record.helper_id or not record.exited for record in helper_records):
        raise ValueError("local drain evidence requires terminated helpers")
    return _DownloadDrainEvidence(
        ownership,
        obligation.obligation_id,
        obligation.payload_revision,
        obligation.payload,
    )


def _append_journal(path: str, record: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _JournalCarrier(LocalCarrier):
    """Local carrier with pre-execution journal evidence.

    The fixture is deliberately limited to DIRECT's local ``env -> sh -> exec
    python`` helper path; it does not claim remote shell or identity coverage.
    """

    def __init__(self, journal_path: str, token: bytes, operation: str) -> None:
        super().__init__()
        self._journal_path = journal_path
        self._token = token
        self._operation = operation

    def execute(self, invocation: PreparedInvocation, *, io, deadline):
        marker = invocation.argv.index("agentworks-runtime-prerequisite")
        _append_journal(
            self._journal_path,
            {
                "kind": "expected",
                "nonce": invocation.argv[marker + 1],
                "operation": self._operation,
                "token": self._token.hex(),
            },
        )
        return super().execute(invocation, io=io, deadline=deadline)


class _RecordedExitCarrier(_JournalCarrier):
    def __init__(self, journal_path: str, token: bytes) -> None:
        super().__init__(journal_path, token, "FileSnapshotBeginRequest")

    def execute(self, invocation: PreparedInvocation, *, io, deadline):
        super().execute(invocation, io=io, deadline=deadline)
        os._exit(91)


class _CrashAfterHelperCarrier(_JournalCarrier):
    def __init__(self, journal_path: str, token: bytes, operation: str, exit_code: int) -> None:
        super().__init__(journal_path, token, operation)
        self._exit_code = exit_code

    def execute(self, invocation: PreparedInvocation, *, io, deadline) -> CarrierReport:
        super().execute(invocation, io=io, deadline=deadline)
        os._exit(self._exit_code)


class _CrashAfterActualCarrier(_RecordedExitCarrier):
    def execute(self, invocation: PreparedInvocation, *, io, deadline) -> CarrierReport:
        def crash_after_actual() -> None:
            deadline_at = time.monotonic() + 20
            while time.monotonic() < deadline_at:
                try:
                    journal = Path(self._journal_path).read_text(encoding="ascii")
                except FileNotFoundError:
                    journal = ""
                if '"kind": "actual"' in journal:
                    os._exit(93)
                time.sleep(0.01)
            os._exit(94)

        threading.Thread(target=crash_after_actual, daemon=True).start()
        super().execute(invocation, io=io, deadline=deadline)
        raise AssertionError("controller should exit while the helper is blocked")


def _start_ticks() -> str:
    return _proc_start_ticks(Path("/proc/self/stat").read_text(encoding="ascii"))


def _proc_start_ticks(stat: str) -> str:
    """Read field 22 after the final parenthesis in Linux proc stat."""
    _, separator, remainder = stat.rpartition(")")
    fields = remainder.split()
    if not separator or len(fields) <= 19:
        raise ValueError("invalid Linux proc stat")
    return fields[19]


def _recorded_helper_is_gone(pid: int, start_ticks: str) -> bool:
    """Treat every readable, live, zombie, or unreadable identity as retained."""
    try:
        current_ticks = _proc_start_ticks((Path("/proc") / str(pid) / "stat").read_text(encoding="ascii"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return current_ticks != start_ticks


def _drain_records_from_journal(path: Path) -> tuple[_LocalHelperDrainRecord, ...]:
    records = [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]
    expected = [record for record in records if record.get("kind") == "expected"]
    actual = [record for record in records if record.get("kind") == "actual"]
    expected_keys = Counter((record.get("nonce"), record.get("operation"), record.get("token")) for record in expected)
    actual_keys = Counter((record.get("nonce"), record.get("operation"), record.get("token")) for record in actual)
    if not expected or expected_keys != actual_keys or any(count != 1 for count in expected_keys.values()):
        raise ValueError("local helper journal has incomplete expected/start coverage")
    result = []
    for record in actual:
        pid = record.get("pid")
        ticks = record.get("ticks")
        if not isinstance(pid, int) or not isinstance(ticks, str):
            raise ValueError("local helper journal has an invalid process identity")
        result.append(_LocalHelperDrainRecord(f"{pid}:{ticks}", _recorded_helper_is_gone(pid, ticks)))
    return tuple(result)


def _tracking_bundle(scratch_path: str, journal_path: str, *, release_path: str | None = None):
    wait_for_release = ""
    if release_path is not None:
        wait_for_release = f"""
 while not os.path.exists({release_path!r}):
  time.sleep(0.01)
"""
    return fixture_source(
        Path(scratch_path),
        f"""
import json
import os
import time
def _fixture_journal(record):
 descriptor=os.open({journal_path!r}, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
 try:
  os.write(descriptor, (json.dumps(record, sort_keys=True) + "\\n").encode("ascii"))
  os.fsync(descriptor)
 finally:
  os.close(descriptor)
_fixture_main=guest.main
def _fixture_tracked_main(nonce):
 guest._fixture_nonce=nonce
 return _fixture_main(nonce)
guest.main=_fixture_tracked_main
_fixture_operate=guest._operate
def _fixture_tracked_operate(request, expires_at):
 _fixture_journal({{
  "kind":"actual",
  "nonce":guest._fixture_nonce,
  "operation":type(request).__name__,
  "pid":os.getpid(),
 "ticks":open("/proc/self/stat", encoding="ascii").read().rpartition(")")[2].split()[19],
 "token":request.token.hex(),
 }})
{wait_for_release}
 return _fixture_operate(request, expires_at)
guest._operate=_fixture_tracked_operate
""",
    )


def _crash_controller_after_completed_snapshot(
    database_path: str,
    root_path: str,
    scratch_path: str,
    journal_path: str,
) -> None:
    from agentworks.execution import _file_snapshot_exchange

    _file_snapshot_exchange.FIXED_BUNDLE = _tracking_bundle(scratch_path, journal_path)  # type: ignore[attr-defined]
    database = Database(Path(database_path))
    target = _target()
    plan = _plan()
    owner, call, _ = _possible_download(database, Path(root_path), target, plan)
    snapshot_begin(
        _RecordedExitCarrier(journal_path, call.token or b""),
        trusted_root_path=str(root_path),
        relative_path="source",
        max_bytes=1024,
        token=call.token or b"",
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=call.runtime_selection,
    )
    del owner
    os._exit(92)


def _crash_controller_with_blocked_snapshot(
    database_path: str,
    root_path: str,
    scratch_path: str,
    journal_path: str,
    release_path: str,
) -> None:
    from agentworks.execution import _file_snapshot_exchange

    _file_snapshot_exchange.FIXED_BUNDLE = _tracking_bundle(  # type: ignore[attr-defined]
        scratch_path,
        journal_path,
        release_path=release_path,
    )
    database = Database(Path(database_path))
    target = _target()
    plan = _plan()
    owner, call, _ = _possible_download(database, Path(root_path), target, plan)
    snapshot_begin(
        _CrashAfterActualCarrier(journal_path, call.token or b""),
        trusted_root_path=str(root_path),
        relative_path="source",
        max_bytes=1024,
        token=call.token or b"",
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=call.runtime_selection,
    )
    del owner
    os._exit(95)


def _crash_recovery_controller(
    database_path: str,
    scratch_path: str,
    journal_path: str,
    generation_id: str,
    after_cleanup: bool,
) -> None:
    from agentworks.execution import _file_snapshot_exchange

    _file_snapshot_exchange.FIXED_BUNDLE = _tracking_bundle(scratch_path, journal_path)  # type: ignore[attr-defined]
    database = Database(Path(database_path))
    predecessor = database.operations.inspect(OperationScope(OperationResourceKind.VM, "download-vm"))
    assert predecessor is not None
    persisted = database.operations.list_lifecycle_obligations(predecessor.ownership)[0]
    call = decode_file_call_obligation(persisted.payload)
    recovered = OperationOwner.recover(database.operations, predecessor.ownership, generation_id)
    evidence = _local_drain_evidence(
        recovered.ownership,
        persisted,
        call,
        _drain_records_from_journal(Path(journal_path)),
    )
    recovery = FileDownloadRecovery.open(recovered, _target(), persisted, evidence)
    token = call.token
    assert token is not None
    recovery.reconcile(
        _JournalCarrier(journal_path, token, "FileSnapshotReconcileRequest"),
        deadline=Deadline.after(30),
    )
    if not after_cleanup:
        os._exit(96)
    recovery.cleanup(
        _CrashAfterHelperCarrier(journal_path, token, "FileSnapshotCleanupRequest", 97),
        deadline=Deadline.after(30),
    )
    raise AssertionError("controller should exit after the cleanup helper")


def _target() -> ManagedTargetIdentity:
    return ManagedTargetIdentity(
        ManagedTargetKind.VM,
        "download-vm",
        "v1:" + "a" * 64,
        "123e4567-e89b-12d3-a456-426614174000",
    )


def _plan() -> IdentityPlan:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)


def _owner(database: Database) -> OperationOwner:
    return OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "download-vm"),
        "file-download",
    )


def _call(root: Path, target: ManagedTargetIdentity, plan: IdentityPlan, token: bytes) -> FileCallObligation:
    return FileCallObligation(
        family=FileCallFamily.DOWNLOAD,
        target=target,
        root=str(root),
        relative_path="source",
        identity_plan=plan,
        runtime_selection=runtime_selection(sys.executable),
        token=token,
        uncertainty=frozenset({FileCallUncertainty.SCRATCH_OWNERSHIP}),
    )


def _possible_download(
    database: Database,
    root: Path,
    target: ManagedTargetIdentity,
    plan: IdentityPlan,
) -> tuple[OperationOwner, FileCallObligation, LifecycleObligation]:
    owner = _owner(database)
    call = _call(root, target, plan, b"t" * 16)
    obligation = owner.register_lifecycle_obligation(
        "file-call",
        payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
        payload=encode_file_call_obligation(call),
        obligation_id="a" * 32,
    )
    obligation.mark_possible_effect()
    return owner, call, database.operations.list_lifecycle_obligations(owner.ownership)[0]


def test_recovery_reconciles_then_persists_exact_debt_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"retained snapshot")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    database = Database(tmp_path / "state.db")
    target = _target()
    plan = _plan()
    owner, call, persisted = _possible_download(database, root, target, plan)
    try:
        started = snapshot_begin(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            max_bytes=1024,
            token=call.token or b"",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=call.runtime_selection,
        )
        assert started.observation is not None
        assert started.observation.state is FileSnapshotObservationState.READY

        recovered = OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
        evidence = _local_drain_evidence(
            recovered.ownership,
            persisted,
            call,
            (_LocalHelperDrainRecord("initial-snapshot", exited=True),),
        )
        recovery = FileDownloadRecovery.open(recovered, target, persisted, evidence)
        reconciled = recovery.reconcile(LocalCarrier(), deadline=Deadline.after(30))
        assert reconciled.observation is not None
        assert reconciled.observation.state is FileSnapshotObservationState.RECOVERED

        row = database.operations.list_lifecycle_obligations(recovered.ownership)[0]
        retained = decode_file_call_obligation(row.payload)
        assert retained.scratch_cleanup_debt is not None
        recovered_again = OperationOwner.recover(database.operations, recovered.ownership, "c" * 32)
        repeated_evidence = _local_drain_evidence(
            recovered_again.ownership,
            row,
            retained,
            (_LocalHelperDrainRecord("initial-snapshot", exited=True),),
        )
        repeated = FileDownloadRecovery.open(recovered_again, target, row, repeated_evidence)
        repeated.reconcile(LocalCarrier(), deadline=Deadline.after(30))
        same_debt = database.operations.list_lifecycle_obligations(recovered_again.ownership)[0]
        assert same_debt.payload_revision == row.payload_revision
        cleaned = repeated.cleanup(LocalCarrier(), deadline=Deadline.after(30))
        assert cleaned.observation is not None
        assert cleaned.observation.state is FileSnapshotObservationState.CLEANED
        assert not tuple(scratch.iterdir())
        assert database.operations.list_lifecycle_obligations(recovered_again.ownership)[0].state is (
            LifecycleObligationState.POSSIBLE_EFFECT
        )
    finally:
        database.close()


def test_spawned_controller_loss_after_helper_completion_keeps_download_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"completed before controller loss")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    database_path = tmp_path / "state.db"
    journal_path = tmp_path / "helpers.jsonl"
    controller = multiprocessing.get_context("spawn").Process(
        target=_crash_controller_after_completed_snapshot,
        args=(str(database_path), str(root), str(scratch), str(journal_path)),
    )
    controller.start()
    controller.join(30)
    assert controller.exitcode == 91

    drain_records = _drain_records_from_journal(journal_path)
    assert drain_records[0].exited

    install_fixture_bundle(monkeypatch, scratch)
    database = Database(database_path)
    try:
        scope = OperationScope(OperationResourceKind.VM, "download-vm")
        predecessor = database.operations.inspect(scope)
        assert predecessor is not None
        persisted = database.operations.list_lifecycle_obligations(predecessor.ownership)[0]
        call = decode_file_call_obligation(persisted.payload)
        recovered = OperationOwner.recover(database.operations, predecessor.ownership, "b" * 32)
        evidence = _local_drain_evidence(
            recovered.ownership,
            persisted,
            call,
            drain_records,
        )
        recovery = FileDownloadRecovery.open(recovered, _target(), persisted, evidence)
        reconciled = recovery.reconcile(LocalCarrier(), deadline=Deadline.after(30))
        assert reconciled.observation is not None
        assert reconciled.observation.state is FileSnapshotObservationState.RECOVERED
        cleaned = recovery.cleanup(LocalCarrier(), deadline=Deadline.after(30))
        assert cleaned.observation is not None
        assert cleaned.observation.state is FileSnapshotObservationState.CLEANED
        assert database.operations.list_lifecycle_obligations(recovered.ownership)[0].state is (
            LifecycleObligationState.POSSIBLE_EFFECT
        )
    finally:
        database.close()


def test_spawned_live_helper_blocks_download_recovery_until_it_disappears(tmp_path: Path) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"blocked helper")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    database_path = tmp_path / "state.db"
    journal_path = tmp_path / "helpers.jsonl"
    release_path = tmp_path / "release-helper"
    controller = multiprocessing.get_context("spawn").Process(
        target=_crash_controller_with_blocked_snapshot,
        args=(str(database_path), str(root), str(scratch), str(journal_path), str(release_path)),
    )
    controller.start()
    controller.join(30)
    assert controller.exitcode == 93
    drain_records = _drain_records_from_journal(journal_path)
    assert len(drain_records) == 1 and not drain_records[0].exited

    database = Database(database_path)
    try:
        predecessor = database.operations.inspect(OperationScope(OperationResourceKind.VM, "download-vm"))
        assert predecessor is not None
        persisted = database.operations.list_lifecycle_obligations(predecessor.ownership)[0]
        call = decode_file_call_obligation(persisted.payload)
        recovered = OperationOwner.recover(database.operations, predecessor.ownership, "b" * 32)
        with pytest.raises(ValueError):
            _local_drain_evidence(recovered.ownership, persisted, call, drain_records)
    finally:
        database.close()

    release_path.touch()
    pid = int(drain_records[0].helper_id.split(":", maxsplit=1)[0])
    deadline_at = time.monotonic() + 20
    while time.monotonic() < deadline_at and Path("/proc", str(pid)).exists():
        time.sleep(0.01)
    assert not Path("/proc", str(pid)).exists()
    completed_records = _drain_records_from_journal(journal_path)
    assert all(record.exited for record in completed_records)
    database = Database(database_path)
    try:
        predecessor = database.operations.inspect(OperationScope(OperationResourceKind.VM, "download-vm"))
        assert predecessor is not None
        persisted = database.operations.list_lifecycle_obligations(predecessor.ownership)[0]
        call = decode_file_call_obligation(persisted.payload)
        recovered = OperationOwner.recover(database.operations, predecessor.ownership, "c" * 32)
        evidence = _local_drain_evidence(
            recovered.ownership,
            persisted,
            call,
            completed_records,
        )
        FileDownloadRecovery.open(recovered, _target(), persisted, evidence)
    finally:
        database.close()


@pytest.mark.parametrize(("after_cleanup", "exit_code"), [(False, 96), (True, 97)])
def test_spawned_recovery_crash_retains_persisted_debt_for_the_next_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_cleanup: bool,
    exit_code: int,
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"recovery crash window")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    database_path = tmp_path / "state.db"
    journal_path = tmp_path / "helpers.jsonl"
    initial = multiprocessing.get_context("spawn").Process(
        target=_crash_controller_after_completed_snapshot,
        args=(str(database_path), str(root), str(scratch), str(journal_path)),
    )
    initial.start()
    initial.join(30)
    assert initial.exitcode == 91
    recovery_controller = multiprocessing.get_context("spawn").Process(
        target=_crash_recovery_controller,
        args=(str(database_path), str(scratch), str(journal_path), "b" * 32, after_cleanup),
    )
    recovery_controller.start()
    recovery_controller.join(30)
    assert recovery_controller.exitcode == exit_code
    drain_records = _drain_records_from_journal(journal_path)
    assert all(record.exited for record in drain_records)

    from agentworks.execution import _file_snapshot_exchange

    monkeypatch.setattr(_file_snapshot_exchange, "FIXED_BUNDLE", _tracking_bundle(str(scratch), str(journal_path)))
    database = Database(database_path)
    try:
        predecessor = database.operations.inspect(OperationScope(OperationResourceKind.VM, "download-vm"))
        assert predecessor is not None
        persisted = database.operations.list_lifecycle_obligations(predecessor.ownership)[0]
        call = decode_file_call_obligation(persisted.payload)
        assert call.scratch_cleanup_debt is not None
        recovered = OperationOwner.recover(database.operations, predecessor.ownership, "c" * 32)
        evidence = _local_drain_evidence(
            recovered.ownership,
            persisted,
            call,
            drain_records,
        )
        recovery = FileDownloadRecovery.open(recovered, _target(), persisted, evidence)
        token = call.token
        assert token is not None
        cleaned = recovery.cleanup(
            _JournalCarrier(str(journal_path), token, "FileSnapshotCleanupRequest"),
            deadline=Deadline.after(30),
        )
        assert cleaned.observation is not None
        assert cleaned.observation.state is FileSnapshotObservationState.CLEANED
        assert database.operations.list_lifecycle_obligations(recovered.ownership)[0].state is (
            LifecycleObligationState.POSSIBLE_EFFECT
        )
    finally:
        database.close()


def test_local_journal_refuses_a_helper_identity_still_owned_by_this_test() -> None:
    assert not _recorded_helper_is_gone(os.getpid(), _start_ticks())


def test_proc_start_ticks_uses_the_final_parenthesis() -> None:
    fields = ["S", *(str(index) for index in range(4, 23))]
    assert _proc_start_ticks("17 (name with ) space) " + " ".join(fields)) == "22"
    with pytest.raises(ValueError):
        _proc_start_ticks("17 (truncated)")


def test_recovery_refuses_stale_payload_family_and_target(tmp_path: Path) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    target = _target()
    plan = _plan()
    owner, call, persisted = _possible_download(database, root, target, plan)
    try:
        recovered = OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
        evidence = _local_drain_evidence(
            recovered.ownership,
            persisted,
            call,
            (_LocalHelperDrainRecord("initial-snapshot", exited=True),),
        )
        wrong_target = ManagedTargetIdentity(
            ManagedTargetKind.VM,
            "other-vm",
            "v1:" + "b" * 64,
            "123e4567-e89b-12d3-a456-426614174001",
        )
        with pytest.raises(StateError):
            FileDownloadRecovery.open(recovered, wrong_target, persisted, evidence)

        rebound = recovered.rebind_lifecycle_obligation(
            persisted.obligation_id,
            "file-call",
            payload_version=persisted.payload_version,
            payload=persisted.payload,
        )
        rebound.publish_payload(
            expected_revision=persisted.payload_revision,
            payload_version=persisted.payload_version,
            payload=encode_file_call_obligation(
                FileCallObligation(
                    family=FileCallFamily.UPLOAD,
                    target=target,
                    root=call.root,
                    relative_path=call.relative_path,
                    identity_plan=plan,
                    runtime_selection=call.runtime_selection,
                    token=call.token,
                )
            ),
        )
        with pytest.raises(StateError):
            FileDownloadRecovery.open(recovered, target, persisted, evidence)
    finally:
        database.close()


def test_local_drain_evidence_refuses_a_live_or_unidentified_helper(tmp_path: Path) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    target = _target()
    plan = _plan()
    owner, call, persisted = _possible_download(database, root, target, plan)
    try:
        recovered = OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
        with pytest.raises(ValueError):
            _local_drain_evidence(
                recovered.ownership,
                persisted,
                call,
                (_LocalHelperDrainRecord("possibly-live", exited=False),),
            )
        with pytest.raises(ValueError):
            _local_drain_evidence(recovered.ownership, persisted, call, ())
    finally:
        database.close()


def test_recovery_of_recovery_requires_current_evidence_for_all_recorded_helpers(tmp_path: Path) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    target = _target()
    plan = _plan()
    owner, call, persisted = _possible_download(database, root, target, plan)
    try:
        recovered_b = OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
        evidence_b = _local_drain_evidence(
            recovered_b.ownership,
            persisted,
            call,
            (_LocalHelperDrainRecord("generation-a-helper", exited=True),),
        )
        recovered_c = OperationOwner.recover(database.operations, recovered_b.ownership, "c" * 32)
        with pytest.raises(StateError):
            FileDownloadRecovery.open(recovered_c, target, persisted, evidence_b)
        evidence_c = _local_drain_evidence(
            recovered_c.ownership,
            persisted,
            call,
            (
                _LocalHelperDrainRecord("generation-a-helper", exited=True),
                _LocalHelperDrainRecord("generation-b-helper", exited=True),
            ),
        )
        FileDownloadRecovery.open(recovered_c, target, persisted, evidence_c)
    finally:
        database.close()


def test_stale_recovery_dispatch_never_calls_the_carrier_and_allows_current_rebind(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    target = _target()
    plan = _plan()
    owner, call, persisted = _possible_download(database, root, target, plan)
    try:
        recovered = OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
        recovery = FileDownloadRecovery.open(
            recovered,
            target,
            persisted,
            _local_drain_evidence(
                recovered.ownership,
                persisted,
                call,
                (_LocalHelperDrainRecord("initial-snapshot", exited=True),),
            ),
        )
        changed_call = replace(call, relative_path="changed")
        rebound = recovered.rebind_lifecycle_obligation(
            persisted.obligation_id,
            "file-call",
            payload_version=persisted.payload_version,
            payload=persisted.payload,
        )
        rebound.publish_payload(
            expected_revision=persisted.payload_revision,
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=encode_file_call_obligation(changed_call),
        )

        carrier = LocalCarrier()
        with pytest.raises(StateError):
            recovery.reconcile(carrier, deadline=Deadline.after(30))
        assert carrier.calls == 0
        current = database.operations.list_lifecycle_obligations(recovered.ownership)[0]
        FileDownloadRecovery.open(
            recovered,
            target,
            current,
            _local_drain_evidence(
                recovered.ownership,
                current,
                changed_call,
                (_LocalHelperDrainRecord("current-snapshot", exited=True),),
            ),
        )
    finally:
        database.close()


class _LostReply(Exception):
    pass


def test_cleanup_debt_commit_then_lost_reply_adopts_exact_row_and_rethrows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"retained snapshot")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    database = Database(tmp_path / "state.db")
    target = _target()
    plan = _plan()
    owner, call, persisted = _possible_download(database, root, target, plan)
    try:
        snapshot_begin(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            max_bytes=1024,
            token=call.token or b"",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=call.runtime_selection,
        )
        recovered = OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
        recovery = FileDownloadRecovery.open(
            recovered,
            target,
            persisted,
            _local_drain_evidence(
                recovered.ownership,
                persisted,
                call,
                (_LocalHelperDrainRecord("initial-snapshot", exited=True),),
            ),
        )
        original = OwnerLifecycleObligation.publish_payload

        def commit_then_lose_reply(self, **kwargs):
            original(self, **kwargs)
            raise _LostReply()

        monkeypatch.setattr(OwnerLifecycleObligation, "publish_payload", commit_then_lose_reply)
        with pytest.raises(_LostReply):
            recovery.reconcile(LocalCarrier(), deadline=Deadline.after(30))
        row = database.operations.list_lifecycle_obligations(recovered.ownership)[0]
        assert row.payload_revision == persisted.payload_revision + 1
        assert recovery._persisted == row  # noqa: SLF001
        assert recovery._call.scratch_cleanup_debt is not None  # noqa: SLF001

        monkeypatch.setattr(OwnerLifecycleObligation, "publish_payload", original)
        cleaned = recovery.cleanup(LocalCarrier(), deadline=Deadline.after(30))
        assert cleaned.observation is not None
        assert cleaned.observation.state is FileSnapshotObservationState.CLEANED
    finally:
        database.close()


def test_cleanup_debt_lost_reply_survives_interrupted_exact_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"retained snapshot")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    database = Database(tmp_path / "state.db")
    target = _target()
    plan = _plan()
    owner, call, persisted = _possible_download(database, root, target, plan)
    try:
        snapshot_begin(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            max_bytes=1024,
            token=call.token or b"",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=call.runtime_selection,
        )
        recovered = OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
        recovery = FileDownloadRecovery.open(
            recovered,
            target,
            persisted,
            _local_drain_evidence(
                recovered.ownership,
                persisted,
                call,
                (_LocalHelperDrainRecord("initial-snapshot", exited=True),),
            ),
        )
        original_publish = OwnerLifecycleObligation.publish_payload
        original_rebind = OperationOwner.rebind_lifecycle_obligation
        rebinds = 0

        def commit_then_lose_reply(self, **kwargs):
            original_publish(self, **kwargs)
            raise _LostReply()

        def fail_only_adoption_readback(self, *args, **kwargs):
            nonlocal rebinds
            rebinds += 1
            if rebinds == 2:
                raise StateError("simulated readback interruption")
            return original_rebind(self, *args, **kwargs)

        monkeypatch.setattr(OwnerLifecycleObligation, "publish_payload", commit_then_lose_reply)
        monkeypatch.setattr(OperationOwner, "rebind_lifecycle_obligation", fail_only_adoption_readback)
        with pytest.raises(_LostReply):
            recovery.reconcile(LocalCarrier(), deadline=Deadline.after(30))
        assert recovery._persisted == persisted  # noqa: SLF001
        assert recovery._call == call  # noqa: SLF001

        monkeypatch.setattr(OwnerLifecycleObligation, "publish_payload", original_publish)
        monkeypatch.setattr(OperationOwner, "rebind_lifecycle_obligation", original_rebind)
        cleaned = recovery.cleanup(LocalCarrier(), deadline=Deadline.after(30))
        assert cleaned.observation is not None
        assert cleaned.observation.state is FileSnapshotObservationState.CLEANED
    finally:
        database.close()


@pytest.mark.parametrize("publish", ["no-commit", "different-payload"])
def test_cleanup_debt_lost_reply_without_exact_row_preserves_original_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publish: str,
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"retained snapshot")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    database = Database(tmp_path / "state.db")
    target = _target()
    plan = _plan()
    owner, call, persisted = _possible_download(database, root, target, plan)
    try:
        snapshot_begin(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            max_bytes=1024,
            token=call.token or b"",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=call.runtime_selection,
        )
        recovered = OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
        recovery = FileDownloadRecovery.open(
            recovered,
            target,
            persisted,
            _local_drain_evidence(
                recovered.ownership,
                persisted,
                call,
                (_LocalHelperDrainRecord("initial-snapshot", exited=True),),
            ),
        )
        debt_result = snapshot_reconcile(
            LocalCarrier(),
            token=call.token or b"",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=call.runtime_selection,
        )
        debt = debt_result.observation.cleanup_debt if debt_result.observation is not None else None
        assert debt is not None
        original = OwnerLifecycleObligation.publish_payload

        def lose_reply(self, **kwargs):
            if publish == "different-payload":
                original(
                    self,
                    expected_revision=kwargs["expected_revision"],
                    payload_version=kwargs["payload_version"],
                    payload=encode_file_call_obligation(replace(call, relative_path="competing")),
                )
            raise _LostReply()

        monkeypatch.setattr(OwnerLifecycleObligation, "publish_payload", lose_reply)
        with pytest.raises(_LostReply):
            recovery._persist_cleanup_debt(debt)  # noqa: SLF001
        assert recovery._persisted == persisted  # noqa: SLF001
        assert recovery._call == call  # noqa: SLF001
        row = database.operations.list_lifecycle_obligations(recovered.ownership)[0]
        if publish == "no-commit":
            assert row.payload == persisted.payload
            assert row.payload_revision == persisted.payload_revision
            monkeypatch.setattr(OwnerLifecycleObligation, "publish_payload", original)
            retried = recovery.reconcile(LocalCarrier(), deadline=Deadline.after(30))
            assert retried.observation is not None
            assert recovery._call.scratch_cleanup_debt is not None  # noqa: SLF001
        else:
            assert row.payload != persisted.payload
            carrier = LocalCarrier()
            with pytest.raises(StateError):
                recovery.reconcile(carrier, deadline=Deadline.after(30))
            assert carrier.calls == 0
    finally:
        database.close()


def test_reconcile_failure_cleanup_debt_is_persisted_before_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"retained snapshot")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    database = Database(tmp_path / "state.db")
    target = _target()
    plan = _plan()
    owner, call, persisted = _possible_download(database, root, target, plan)
    try:
        snapshot_begin(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            max_bytes=1024,
            token=call.token or b"",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=call.runtime_selection,
        )
        failure_patch = """
_fixture_operate=guest._operate
def _fixture_failure_after_reconcile(request, expires_at):
 result=_fixture_operate(request, expires_at)
 if isinstance(request, guest.FileSnapshotReconcileRequest):
  raise guest._SafeFailure(guest.FileSnapshotFailureControl(
   guest.FileSnapshotFailureCode.SCRATCH,
   scratch_kind=guest.ScratchFailureKind.DEADLINE,
   scratch_phase=guest.ScratchPhase.RECONCILE,
   cleanup_debt=guest._cleanup_debt(result),
  ))
 return result
guest._operate=_fixture_failure_after_reconcile
"""
        install_fixture_bundle(monkeypatch, scratch, failure_patch)
        recovered = OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
        recovery = FileDownloadRecovery.open(
            recovered,
            target,
            persisted,
            _local_drain_evidence(
                recovered.ownership,
                persisted,
                call,
                (_LocalHelperDrainRecord("initial-snapshot", exited=True),),
            ),
        )
        result = recovery.reconcile(LocalCarrier(), deadline=Deadline.after(30))
        assert result.observation is not None
        assert result.observation.failure is not None
        assert result.observation.failure.cleanup_debt is not None
        retained = database.operations.list_lifecycle_obligations(recovered.ownership)[0]
        assert (
            decode_file_call_obligation(retained.payload).scratch_cleanup_debt
            == result.observation.failure.cleanup_debt
        )
        cleaned = recovery.cleanup(LocalCarrier(), deadline=Deadline.after(30))
        assert cleaned.observation is not None
        assert cleaned.observation.state is FileSnapshotObservationState.CLEANED
    finally:
        database.close()
