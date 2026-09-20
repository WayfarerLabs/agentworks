"""Fixed-lock and guest-local deadline checks for the file-read helper."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agentworks.execution import _file_read, _file_read_guest
from agentworks.execution._file_lock import FileLockError, FileLockFailureKind
from agentworks.execution._file_read import FileReadCandidateResult, FileReadObservationState, read_file
from agentworks.execution._file_read_protocol import FileReadFailure, FileReadRequest
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import Deadline
from tests.execution.files._file_read_support import LocalCarrier, fixed_lock_source, install_fixed_lock_bundle

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file-read helper candidate requires Linux")


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


@pytest.fixture(autouse=True)
def fixed_lock_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return install_fixed_lock_bundle(tmp_path / "lock-root", monkeypatch)


def _read(
    root: Path,
    leaf: str,
    plan: IdentityPlan,
    *,
    deadline: Deadline | None = None,
    carrier: LocalCarrier | None = None,
) -> FileReadCandidateResult:
    return read_file(
        carrier or LocalCarrier(),
        trusted_root_path=str(root),
        relative_path=leaf,
        max_bytes=1024,
        plan=plan,
        deadline=deadline or Deadline.after(15),
        runtime_path=sys.executable,
    )


def test_identity_mismatch_precedes_a_missing_system_lock(
    tmp_path: Path,
    plan: IdentityPlan,
    fixed_lock_bundle: Path,
) -> None:
    (fixed_lock_bundle / "var/lib/agentworks/execution/files.lock").unlink()
    expected = plan.expected
    mismatched = IdentityPlan(
        IdentityExpectation(expected.euid + 100_000, expected.egid, expected.groups),
        IdentityMode.DIRECT,
    )

    result = _read(tmp_path / "missing-root", "missing", mismatched)

    assert result.observation.state is FileReadObservationState.REFUSED
    assert result.observation.failure is FileReadFailure.IDENTITY_MISMATCH


@pytest.mark.parametrize("lock_state", ["missing", "unsafe"])
def test_missing_or_unsafe_system_lock_refuses_before_absent_target_observation(
    tmp_path: Path,
    plan: IdentityPlan,
    fixed_lock_bundle: Path,
    lock_state: str,
) -> None:
    lock = fixed_lock_bundle / "var/lib/agentworks/execution/files.lock"
    if lock_state == "missing":
        lock.unlink()
        expected = FileReadFailure.LOCK_MISSING
    else:
        lock.chmod(0o600)
        expected = FileReadFailure.LOCK_UNSAFE

    result = _read(tmp_path / "missing-root", "missing", plan)

    assert result.observation.state is FileReadObservationState.REFUSED
    assert result.observation.failure is expected
    assert result.observation.snapshot is None


def test_guest_deadline_while_acquiring_the_fixed_lock(
    tmp_path: Path,
    plan: IdentityPlan,
    fixed_lock_bundle: Path,
) -> None:
    import fcntl

    lock_fd = os.open(fixed_lock_bundle / "var/lib/agentworks/execution/files.lock", os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _read(
            tmp_path,
            "missing",
            plan,
            deadline=Deadline.after(0.03),
            carrier=LocalCarrier(dispatch_deadline=Deadline.after(5)),
        )
    finally:
        os.close(lock_fd)

    assert result.observation.state is FileReadObservationState.REFUSED
    assert result.observation.failure is FileReadFailure.LOCK_DEADLINE


def test_snapshot_deadline_maps_to_closed_read_refusal(
    tmp_path: Path,
    plan: IdentityPlan,
    fixed_lock_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = """
def deadline_snapshot(*args,**kwargs):
 raise guest.SnapshotReadError(guest.SnapshotFailureKind.DEADLINE)
guest.read_snapshot=deadline_snapshot
"""
    monkeypatch.setattr(_file_read, "FIXED_SOURCE", fixed_lock_source(fixed_lock_bundle, patch))

    result = _read(tmp_path, "missing", plan)

    assert result.observation.state is FileReadObservationState.REFUSED
    assert result.observation.failure is FileReadFailure.DEADLINE


@pytest.mark.parametrize(
    ("kind", "failure"),
    [
        (FileLockFailureKind.UNSUPPORTED, FileReadFailure.LOCK_UNSUPPORTED),
        (FileLockFailureKind.MISSING, FileReadFailure.LOCK_MISSING),
        (FileLockFailureKind.UNSAFE, FileReadFailure.LOCK_UNSAFE),
        (FileLockFailureKind.CONFLICT, FileReadFailure.LOCK_CONFLICT),
        (FileLockFailureKind.DEADLINE, FileReadFailure.LOCK_DEADLINE),
        (FileLockFailureKind.IO, FileReadFailure.LOCK_IO),
    ],
)
def test_every_lock_failure_has_a_closed_read_variant(
    kind: FileLockFailureKind,
    failure: FileReadFailure,
) -> None:
    assert _file_read_guest._failure_for_lock(FileLockError(kind)) is failure


@pytest.mark.parametrize("present", [False, True], ids=["absence", "post-snapshot"])
def test_final_guest_deadline_covers_absence_and_materialized_snapshot(
    tmp_path: Path,
    plan: IdentityPlan,
    fixed_lock_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    present: bool,
) -> None:
    if present:
        (tmp_path / "target").write_bytes(b"content")
    patch = """
real_snapshot=guest._snapshot
def delayed_snapshot(*args,**kwargs):
 result=real_snapshot(*args,**kwargs)
 guest.time.sleep(0.03)
 return result
guest._snapshot=delayed_snapshot
"""
    monkeypatch.setattr(_file_read, "FIXED_SOURCE", fixed_lock_source(fixed_lock_bundle, patch))

    result = _read(
        tmp_path,
        "target",
        plan,
        deadline=Deadline.after(0.01),
        carrier=LocalCarrier(dispatch_deadline=Deadline.after(5)),
    )

    assert result.observation.state is FileReadObservationState.REFUSED
    assert result.observation.failure is FileReadFailure.DEADLINE
    assert result.observation.snapshot is None


def test_snapshot_records_are_emitted_only_after_the_fixed_lock_is_released(
    tmp_path: Path,
    plan: IdentityPlan,
    fixed_lock_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "target").write_bytes(b"content")
    patch = """
unlocked=[False]
base_lock=guest.system_file_lock
@contextlib.contextmanager
def observed_lock(*,expires_at):
 with base_lock(expires_at=expires_at):
  yield
 unlocked[0]=True
guest.system_file_lock=observed_lock
real_emit=guest._Emitter.emit
def checked_emit(self,kind,body):
 if not unlocked[0]:
  raise RuntimeError('protocol output while locked')
 return real_emit(self,kind,body)
guest._Emitter.emit=checked_emit
"""
    monkeypatch.setattr(_file_read, "FIXED_SOURCE", fixed_lock_source(fixed_lock_bundle, patch))

    result = _read(tmp_path, "target", plan)

    assert result.observation.state is FileReadObservationState.PRESENT
    assert result.observation.snapshot is not None
    assert result.observation.snapshot.data == b"content"


def test_root_descriptor_closes_when_snapshot_control_raises(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_fd = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    interruption = KeyboardInterrupt()
    request = FileReadRequest("0" * 32, str(tmp_path), "file", 1, plan.expected, None)

    monkeypatch.setattr(_file_read_guest, "open_linux_root", lambda _path: root_fd)

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise interruption

    monkeypatch.setattr(_file_read_guest, "read_snapshot", interrupt)

    with pytest.raises(KeyboardInterrupt) as raised:
        _file_read_guest._snapshot(request, None)

    assert raised.value is interruption
    with pytest.raises(OSError):
        os.fstat(root_fd)
