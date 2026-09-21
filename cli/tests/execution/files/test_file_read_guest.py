"""Guest-local deadline and cleanup checks for the private file-read helper."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agentworks.execution import _file_read, _file_read_guest
from agentworks.execution._file_read import FileReadCandidateResult, FileReadObservationState, read_file
from agentworks.execution._file_read_protocol import FileReadFailure, FileReadRequest
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import Deadline
from tests.execution.files._file_read_support import LocalCarrier, fixture_source, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file-read helper candidate requires Linux")


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


@pytest.fixture(autouse=True)
def fixed_source(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fixture_bundle(monkeypatch)


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
        runtime_selection=runtime_selection(sys.executable),
    )


def test_identity_mismatch_precedes_target_access(tmp_path: Path, plan: IdentityPlan) -> None:
    expected = plan.expected
    mismatched = IdentityPlan(
        IdentityExpectation(expected.euid + 100_000, expected.egid, expected.groups),
        IdentityMode.DIRECT,
    )

    result = _read(tmp_path / "missing-root", "missing", mismatched)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileReadObservationState.REFUSED
    assert result_observation.failure is FileReadFailure.IDENTITY_MISMATCH


def test_snapshot_deadline_maps_to_closed_read_refusal(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = """
def deadline_snapshot(*args,**kwargs):
 raise guest.SnapshotReadError(guest.SnapshotFailureKind.DEADLINE)
guest.read_snapshot=deadline_snapshot
"""
    monkeypatch.setattr(_file_read, "FIXED_BUNDLE", fixture_source(patch))

    result = _read(tmp_path, "missing", plan)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileReadObservationState.REFUSED
    assert result_observation.failure is FileReadFailure.DEADLINE


@pytest.mark.parametrize("present", [False, True], ids=["absence", "post-snapshot"])
def test_final_guest_deadline_covers_absence_and_materialized_snapshot(
    tmp_path: Path,
    plan: IdentityPlan,
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
    monkeypatch.setattr(_file_read, "FIXED_BUNDLE", fixture_source(patch))

    result = _read(
        tmp_path,
        "target",
        plan,
        deadline=Deadline.after(0.01),
        carrier=LocalCarrier(dispatch_deadline=Deadline.after(5)),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileReadObservationState.REFUSED
    assert result_observation.failure is FileReadFailure.DEADLINE
    assert result_observation.snapshot is None


def test_read_succeeds_without_protected_lock_namespace(tmp_path: Path, plan: IdentityPlan) -> None:
    (tmp_path / "target").write_bytes(b"content")

    result = _read(tmp_path, "target", plan)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileReadObservationState.PRESENT
    result_snapshot = result_observation.snapshot
    assert result_snapshot is not None
    assert result_snapshot.data == b"content"


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
