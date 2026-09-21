"""End-to-end checks for private stage creation and bounded chunks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from agentworks.execution import _file_stage_guest
from agentworks.execution._file_stage_bundle import FIXED_BUNDLE
from agentworks.execution._file_stage_exchange import (
    FileStageObservationState,
    stage_begin,
    stage_chunk,
    stage_cleanup,
    stage_reconcile,
)
from agentworks.execution._file_stage_protocol import (
    MAX_STAGE_CHUNK_BYTES,
    FileStageFailureCode,
    FileStageReconcileRequest,
)
from agentworks.execution._helper_bundle import FixedFileHelperBundle
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution._scratch import ScratchFailureKind, ScratchPhase
from agentworks.execution._scratch_receipt import _Identity, scratch_name
from agentworks.execution.carrier import CarrierIO, Deadline, Dispatch, SinkOutput
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from tests.execution.files._file_stage_support import LocalCarrier, fixture_source, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private stage helper candidate requires Linux")

_TOKEN = bytes(range(16))

_ADVANCE_AFTER_OPEN_ROOT = """
clock=[guest.time.monotonic()]
def controlled_monotonic():
 return clock[0]
guest.time.monotonic=controlled_monotonic
real_open_root=guest.open_linux_root
def advancing_open_root(path):
 result=real_open_root(path)
 clock[0]+=10.0
 return result
guest.open_linux_root=advancing_open_root
"""

_ADVANCE_AFTER_OPERATE = """
clock=[guest.time.monotonic()]
def controlled_monotonic():
 return clock[0]
guest.time.monotonic=controlled_monotonic
real_operate=guest._operate
def advancing_operate(*args,**kwargs):
 result=real_operate(*args,**kwargs)
 clock[0]+=10.0
 return result
guest._operate=advancing_operate
"""


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


@pytest.fixture(autouse=True)
def fixed_source(monkeypatch: pytest.MonkeyPatch) -> FixedFileHelperBundle:
    return install_fixture_bundle(monkeypatch)


def _begin(
    root: Path,
    path: str,
    length: int,
    plan: IdentityPlan,
    *,
    runtime: str = sys.executable,
    carrier: LocalCarrier | None = None,
    deadline: Deadline | None = None,
):
    carrier = carrier or LocalCarrier()
    result = stage_begin(
        carrier,
        trusted_root_path=str(root),
        relative_path=path,
        token=_TOKEN,
        expected_length=length,
        plan=plan,
        deadline=deadline or Deadline.after(15),
        runtime_selection=runtime_selection(runtime),
    )
    return carrier, result


def _chunk(root: Path, path: str, reference, offset: int, data: bytes, plan: IdentityPlan):
    carrier = LocalCarrier()
    result = stage_chunk(
        carrier,
        trusted_root_path=str(root),
        relative_path=path,
        token=_TOKEN,
        reference=reference,
        offset=offset,
        data=data,
        chunk_digest=hashlib.sha256(data).digest(),
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(sys.executable),
    )
    return carrier, result


def _reconcile(
    root: Path,
    path: str,
    plan: IdentityPlan,
    *,
    runtime: str = sys.executable,
    carrier: LocalCarrier | None = None,
    deadline: Deadline | None = None,
):
    carrier = carrier or LocalCarrier()
    result = stage_reconcile(
        carrier,
        trusted_root_path=str(root),
        relative_path=path,
        token=_TOKEN,
        plan=plan,
        deadline=deadline or Deadline.after(15),
        runtime_selection=runtime_selection(runtime),
    )
    return carrier, result


def _cleanup(
    root: Path,
    path: str,
    debt,
    plan: IdentityPlan,
    *,
    runtime: str = sys.executable,
    carrier: LocalCarrier | None = None,
    deadline: Deadline | None = None,
):
    carrier = carrier or LocalCarrier()
    result = stage_cleanup(
        carrier,
        trusted_root_path=str(root),
        relative_path=path,
        token=_TOKEN,
        cleanup_debt=debt,
        plan=plan,
        deadline=deadline or Deadline.after(15),
        runtime_selection=runtime_selection(runtime),
    )
    return carrier, result


def test_missing_runtime_yields_no_stage_observation(tmp_path: Path, plan: IdentityPlan) -> None:
    carrier, result = _begin(tmp_path, "target", 1, plan, runtime="/missing/agentworks-python")

    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
    assert result.observation is None


class _DiscardSink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


class _LostStdoutCarrier(LocalCarrier):
    def execute(self, invocation, *, io, deadline):
        assert isinstance(io.output, SinkOutput)
        hidden = CarrierIO(
            input=io.input,
            output=SinkOutput(_DiscardSink(), io.output.stderr, require_live=False),
            sensitive=io.sensitive,
        )
        return super().execute(invocation, io=hidden, deadline=deadline)


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "system-3.11"])
def test_nested_stage_uses_one_sensitive_attempt_and_exact_private_modes(
    tmp_path: Path,
    plan: IdentityPlan,
    runtime: Path,
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    root = tmp_path / "approved"
    parent = root / "nested"
    parent.mkdir(parents=True)
    payload = b"literal-private-payload"

    begin_carrier, begun = _begin(root, "nested/destination", len(payload), plan, runtime=str(runtime))

    assert begun.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    begun_observation = begun.observation
    assert begun_observation is not None
    assert begun_observation.state is FileStageObservationState.CREATED
    reference = begun_observation.reference
    assert reference is not None
    scratch = parent / scratch_name(_TOKEN)
    data_path = scratch / "data"
    receipt_path = scratch / "receipt"
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
    assert stat.S_IMODE(data_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400
    assert not (root / "destination").exists() and not (parent / "destination").exists()
    assert begin_carrier.io is not None and begin_carrier.io.sensitive
    assert begin_carrier.io.input.sensitive  # type: ignore[union-attr]
    begin_carrier_invocation = begin_carrier.invocation
    assert begin_carrier_invocation is not None
    assert str(root) not in begin_carrier.invocation.argv and "destination" not in begin_carrier.invocation.argv

    first = payload[:7]
    second = payload[7:]
    _, written = _chunk(root, "nested/destination", reference, 0, first, plan)
    _, completed = _chunk(root, "nested/destination", reference, len(first), second, plan)

    written_observation = written.observation
    assert written_observation is not None
    assert written_observation.state is FileStageObservationState.ACCEPTED
    completed_observation = completed.observation
    assert completed_observation is not None
    assert completed_observation.state is FileStageObservationState.ACCEPTED
    assert data_path.read_bytes() == payload
    assert payload.decode() not in repr(completed)


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "system-3.11"])
def test_lost_begin_reply_recovers_cleanup_only_and_cleans_exact_artifact(
    tmp_path: Path,
    plan: IdentityPlan,
    runtime: Path,
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    root = tmp_path / "approved"
    root.mkdir()
    lost = _LostStdoutCarrier()

    _, begun = _begin(root, "destination", 1, plan, runtime=str(runtime), carrier=lost)

    assert begun.runtime_prerequisite.state is RuntimePrerequisiteState.UNKNOWN
    assert begun.observation is None
    scratch = root / scratch_name(_TOKEN)
    assert scratch.is_dir()

    reconcile_carrier, recovered = _reconcile(root, "destination", plan, runtime=str(runtime))

    recovered_observation = recovered.observation
    assert recovered_observation is not None
    assert recovered_observation.state is FileStageObservationState.RECOVERED
    assert recovered_observation.reference is None
    debt = recovered_observation.cleanup_debt
    assert debt is not None
    assert reconcile_carrier.io is not None and reconcile_carrier.io.sensitive

    _, cleaned = _cleanup(root, "destination", debt, plan, runtime=str(runtime))

    cleaned_observation = cleaned.observation
    assert cleaned_observation is not None
    assert cleaned_observation.state is FileStageObservationState.CLEANED
    assert not scratch.exists()


@pytest.mark.parametrize("artifact", ["missing", "partial", "changed-receipt"])
def test_reconcile_never_turns_missing_partial_or_invalid_receipt_into_absence(
    tmp_path: Path,
    plan: IdentityPlan,
    artifact: str,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    scratch = root / scratch_name(_TOKEN)
    if artifact == "partial":
        scratch.mkdir(mode=0o700)
        data = scratch / "data"
        data.write_bytes(b"")
        data.chmod(0o600)
    elif artifact == "changed-receipt":
        _, begun = _begin(root, "destination", 1, plan)
        begun_observation = begun.observation
        assert begun_observation is not None
        assert begun_observation.state is FileStageObservationState.CREATED
        receipt = scratch / "receipt"
        receipt.chmod(0o600)
        receipt.write_bytes(b"changed")
        receipt.chmod(0o400)

    _, result = _reconcile(root, "destination", plan)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.OWNERSHIP_UNCERTAIN
    assert result_observation.cleanup_debt is None
    assert result_observation.reference is None


def test_cleanup_wrong_inode_refuses_and_retains_exact_debt(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    _, begun = _begin(root, "destination", 1, plan)
    begun_observation = begun.observation
    assert begun_observation is not None
    assert begun_observation.state is FileStageObservationState.CREATED
    _, recovered = _reconcile(root, "destination", plan)
    recovered_observation = recovered.observation
    assert recovered_observation is not None
    debt = recovered_observation.cleanup_debt
    assert debt is not None and debt._receipt is not None
    wrong = replace(debt, _receipt=_Identity(debt._receipt.device, debt._receipt.inode + 1))

    _, result = _cleanup(root, "destination", wrong, plan)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    failure = result_observation.failure
    assert failure is not None and failure.kind is ScratchFailureKind.CONFLICT
    assert failure.phase is ScratchPhase.CLEANUP
    assert failure.cleanup_debt == wrong
    assert not (root / scratch_name(_TOKEN) / "data").exists()
    assert (root / scratch_name(_TOKEN) / "receipt").is_file()

    _, retried = _cleanup(root, "destination", debt, plan)

    retried_observation = retried.observation
    assert retried_observation is not None
    assert retried_observation.state is FileStageObservationState.CLEANED
    assert not (root / scratch_name(_TOKEN)).exists()


def test_delayed_chunk_after_cleanup_refuses_without_recreating_scratch(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    _, begun = _begin(root, "destination", 1, plan)
    begun_observation = begun.observation
    assert begun_observation is not None
    reference = begun_observation.reference
    assert reference is not None
    _, recovered = _reconcile(root, "destination", plan)
    recovered_observation = recovered.observation
    assert recovered_observation is not None
    debt = recovered_observation.cleanup_debt
    assert debt is not None
    _, cleaned = _cleanup(root, "destination", debt, plan)
    cleaned_observation = cleaned.observation
    assert cleaned_observation is not None
    assert cleaned_observation.state is FileStageObservationState.CLEANED

    _, delayed = _chunk(root, "destination", reference, 0, b"x", plan)

    delayed_observation = delayed.observation
    assert delayed_observation is not None
    assert delayed_observation.state is FileStageObservationState.REFUSED
    delayed_failure = delayed_observation.failure
    assert delayed_failure is not None
    assert delayed_observation.failure.code is FileStageFailureCode.SCRATCH
    assert not (root / scratch_name(_TOKEN)).exists()


def test_exact_duplicate_chunk_is_accepted_without_appending(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    payload = b"duplicate"
    _, begun = _begin(root, "destination", len(payload), plan)
    begun_observation = begun.observation
    assert begun_observation is not None
    reference = begun_observation.reference
    assert reference is not None

    _, first = _chunk(root, "destination", reference, 0, payload, plan)
    _, duplicate = _chunk(root, "destination", reference, 0, payload, plan)

    first_observation = first.observation
    assert first_observation is not None
    assert first_observation.state is FileStageObservationState.ACCEPTED
    duplicate_observation = duplicate.observation
    assert duplicate_observation is not None
    assert duplicate_observation.state is FileStageObservationState.ACCEPTED
    assert (root / scratch_name(_TOKEN) / "data").read_bytes() == payload


def test_wrong_hash_is_refused_with_exact_cleanup_debt(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    _, begun = _begin(root, "destination", 1, plan)
    begun_observation = begun.observation
    assert begun_observation is not None
    reference = begun_observation.reference
    assert reference is not None
    carrier = LocalCarrier()

    result = stage_chunk(
        carrier,
        trusted_root_path=str(root),
        relative_path="destination",
        token=_TOKEN,
        reference=reference,
        offset=0,
        data=b"x",
        chunk_digest=hashlib.sha256(b"y").digest(),
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(sys.executable),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    failure = result_observation.failure
    assert failure is not None and failure.code is FileStageFailureCode.SCRATCH
    assert failure.kind is ScratchFailureKind.INTEGRITY
    failure_cleanup_debt = failure.cleanup_debt
    assert failure_cleanup_debt is not None


def test_changed_receipt_is_refused_and_never_recreated(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    _, begun = _begin(root, "destination", 1, plan)
    begun_observation = begun.observation
    assert begun_observation is not None
    reference = begun_observation.reference
    assert reference is not None
    receipt = root / scratch_name(_TOKEN) / "receipt"
    receipt.chmod(0o600)
    receipt.write_bytes(b"changed")
    receipt.chmod(0o400)

    _, result = _chunk(root, "destination", reference, 0, b"x", plan)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    failure = result_observation.failure
    assert failure is not None and failure.code is FileStageFailureCode.SCRATCH
    assert failure.kind is ScratchFailureKind.CONFLICT
    failure_cleanup_debt = failure.cleanup_debt
    assert failure_cleanup_debt is not None
    assert receipt.read_bytes() == b"changed"


@pytest.mark.parametrize(
    ("root_exists", "parent_exists", "failure"),
    [
        (False, False, FileStageFailureCode.ROOT_REFUSED),
        (True, False, FileStageFailureCode.PARENT_REFUSED),
    ],
)
def test_private_creation_absence_is_a_refusal(
    tmp_path: Path,
    plan: IdentityPlan,
    root_exists: bool,
    parent_exists: bool,
    failure: FileStageFailureCode,
) -> None:
    root = tmp_path / "approved"
    if root_exists:
        root.mkdir()
    if parent_exists:
        (root / "nested").mkdir()

    _, result = _begin(root, "nested/destination", 1, plan)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_observation.failure.code is failure


def test_identity_mismatch_precedes_root_access(tmp_path: Path, plan: IdentityPlan) -> None:
    missing_root = tmp_path / "private-root-canary"
    expected = plan.expected
    mismatched = IdentityPlan(
        IdentityExpectation(expected.euid + 100_000, expected.egid, expected.groups),
        IdentityMode.DIRECT,
    )

    _, result = _begin(missing_root, "destination", 1, mismatched)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_observation.failure.code is FileStageFailureCode.IDENTITY_MISMATCH
    assert str(missing_root) not in repr(result)


def test_reconcile_identity_mismatch_precedes_absent_root_access(tmp_path: Path, plan: IdentityPlan) -> None:
    missing_root = tmp_path / "private-root-canary"
    expected = plan.expected
    mismatched = IdentityPlan(
        IdentityExpectation(expected.euid + 100_000, expected.egid, expected.groups),
        IdentityMode.DIRECT,
    )

    _, result = _reconcile(missing_root, "destination", mismatched)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_observation.failure.code is FileStageFailureCode.IDENTITY_MISMATCH
    assert str(missing_root) not in repr(result)


def test_guest_deadline_after_missing_root_lookup_is_not_root_refusal(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = fixture_source(_ADVANCE_AFTER_OPEN_ROOT)
    monkeypatch.setattr("agentworks.execution._file_stage_exchange.FIXED_BUNDLE", source)
    carrier = LocalCarrier(dispatch_deadline=Deadline.after(15))

    _, result = _begin(
        tmp_path / "missing-root",
        "destination",
        1,
        plan,
        carrier=carrier,
        deadline=Deadline.after(5),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_observation.failure.code is FileStageFailureCode.DEADLINE
    assert result_observation.failure.cleanup_debt is None


def test_reconcile_deadline_after_missing_root_lookup_is_not_absence(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = fixture_source(_ADVANCE_AFTER_OPEN_ROOT)
    monkeypatch.setattr("agentworks.execution._file_stage_exchange.FIXED_BUNDLE", source)

    _, result = _reconcile(
        tmp_path / "missing-root",
        "destination",
        plan,
        carrier=LocalCarrier(dispatch_deadline=Deadline.after(15)),
        deadline=Deadline.after(5),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_observation.failure.code is FileStageFailureCode.SCRATCH
    assert result_observation.failure.kind is ScratchFailureKind.DEADLINE
    assert result_observation.failure.phase is ScratchPhase.RECONCILE
    assert result_observation.failure.cleanup_debt is None
    assert result_observation.cleanup_debt is None


def test_guest_deadline_after_closed_success_retains_created_cleanup_debt(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = fixture_source(_ADVANCE_AFTER_OPERATE)
    monkeypatch.setattr("agentworks.execution._file_stage_exchange.FIXED_BUNDLE", source)
    root = tmp_path / "approved"
    root.mkdir()
    carrier = LocalCarrier(dispatch_deadline=Deadline.after(15))

    _, result = _begin(
        root,
        "destination",
        1,
        plan,
        carrier=carrier,
        deadline=Deadline.after(5),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    failure = result_observation.failure
    assert failure is not None and failure.code is FileStageFailureCode.SCRATCH
    assert failure.kind is ScratchFailureKind.DEADLINE
    assert failure.phase is ScratchPhase.BEGIN
    failure_cleanup_debt = failure.cleanup_debt
    assert failure_cleanup_debt is not None
    assert result_observation.reference is None
    assert (root / scratch_name(_TOKEN)).is_dir()


def test_guest_deadline_after_closed_reconcile_retains_recovered_cleanup_debt(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    _, begun = _begin(root, "destination", 1, plan)
    begun_observation = begun.observation
    assert begun_observation is not None
    assert begun_observation.state is FileStageObservationState.CREATED
    source = fixture_source(_ADVANCE_AFTER_OPERATE)
    monkeypatch.setattr("agentworks.execution._file_stage_exchange.FIXED_BUNDLE", source)

    _, result = _reconcile(
        root,
        "destination",
        plan,
        carrier=LocalCarrier(dispatch_deadline=Deadline.after(15)),
        deadline=Deadline.after(5),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    failure = result_observation.failure
    assert failure is not None and failure.kind is ScratchFailureKind.DEADLINE
    assert failure.phase is ScratchPhase.RECONCILE
    failure_cleanup_debt = failure.cleanup_debt
    assert failure_cleanup_debt is not None


def test_cleanup_deadline_after_parent_open_refuses_before_mutation(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    _, begun = _begin(root, "destination", 1, plan)
    begun_observation = begun.observation
    assert begun_observation is not None
    assert begun_observation.state is FileStageObservationState.CREATED
    _, recovered = _reconcile(root, "destination", plan)
    recovered_observation = recovered.observation
    assert recovered_observation is not None
    debt = recovered_observation.cleanup_debt
    assert debt is not None
    scratch = root / scratch_name(_TOKEN)
    source = fixture_source(_ADVANCE_AFTER_OPEN_ROOT)
    monkeypatch.setattr("agentworks.execution._file_stage_exchange.FIXED_BUNDLE", source)

    _, result = _cleanup(
        root,
        "destination",
        debt,
        plan,
        carrier=LocalCarrier(dispatch_deadline=Deadline.after(15)),
        deadline=Deadline.after(5),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    failure = result_observation.failure
    assert failure is not None and failure.kind is ScratchFailureKind.DEADLINE
    assert failure.phase is ScratchPhase.CLEANUP
    assert failure.cleanup_debt == debt
    assert (scratch / "data").is_file()
    assert (scratch / "receipt").is_file()


def test_guest_deadline_after_closed_cleanup_retains_original_exact_debt(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    _, begun = _begin(root, "destination", 1, plan)
    begun_observation = begun.observation
    assert begun_observation is not None
    assert begun_observation.state is FileStageObservationState.CREATED
    _, recovered = _reconcile(root, "destination", plan)
    recovered_observation = recovered.observation
    assert recovered_observation is not None
    debt = recovered_observation.cleanup_debt
    assert debt is not None
    source = fixture_source(_ADVANCE_AFTER_OPERATE)
    monkeypatch.setattr("agentworks.execution._file_stage_exchange.FIXED_BUNDLE", source)

    _, result = _cleanup(
        root,
        "destination",
        debt,
        plan,
        carrier=LocalCarrier(dispatch_deadline=Deadline.after(15)),
        deadline=Deadline.after(5),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    failure = result_observation.failure
    assert failure is not None and failure.kind is ScratchFailureKind.DEADLINE
    assert failure.phase is ScratchPhase.CLEANUP
    assert failure.cleanup_debt == debt
    assert not (root / scratch_name(_TOKEN)).exists()


def test_reconcile_closes_owned_descriptors_on_control_interrupt(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "approved"
    parent = root / "nested"
    parent.mkdir(parents=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    request = FileStageReconcileRequest(
        "0" * 32,
        str(root),
        "nested/destination",
        _TOKEN,
        plan.expected,
        1.0,
    )

    monkeypatch.setattr(_file_stage_guest, "_open_parent", lambda _request: (root_fd, parent_fd))

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(_file_stage_guest, "reconcile_scratch_ownership", interrupt)

    with pytest.raises(KeyboardInterrupt):
        _file_stage_guest._operate(request, None)
    for descriptor in (root_fd, parent_fd):
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_helper_records_and_receipt_tolerate_short_writes(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_source = """
real_write=guest.os.write
def short_write(fd,data):
 return real_write(fd,data[:max(1,len(data)//3)])
guest.os.write=short_write
"""
    source = fixture_source(patch_source)
    monkeypatch.setattr("agentworks.execution._file_stage_exchange.FIXED_BUNDLE", source)
    root = tmp_path / "approved"
    root.mkdir()

    _, result = _begin(root, "destination", 1, plan)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.CREATED


def test_complete_proxmox_bodies_fit_for_transfer_and_recovery(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    fixed_source: str,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.invalid", "node1", 101, "token", "synthetic"))
    body_sizes: list[int] = []
    status: dict[str, object] = {}

    def request(method: str, suffix: str, *, body: bytes | None = None, timeout: float | None) -> dict[str, object]:
        if method == "POST":
            assert body is not None and body.isascii()
            body_sizes.append(len(body))
            envelope = json.loads(body)
            completed = subprocess.run(
                envelope["command"],
                input=envelope["input-data"].encode("ascii"),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            status.clear()
            status.update(
                exited=True,
                exitcode=completed.returncode,
                **{"out-data": completed.stdout.decode("ascii"), "err-data": completed.stderr.decode("ascii")},
            )
            return {"pid": len(body_sizes)}
        assert suffix.startswith("exec-status?pid=") and body is None
        return status

    monkeypatch.setattr(carrier._wire, "request", request)
    with patch("agentworks.execution._file_stage_exchange.FIXED_BUNDLE", fixed_source):
        begun = stage_begin(
            carrier,
            trusted_root_path=str(root),
            relative_path="destination",
            token=_TOKEN,
            expected_length=MAX_STAGE_CHUNK_BYTES,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_selection=runtime_selection(sys.executable),
        )
        begun_observation = begun.observation
        assert begun_observation is not None
        reference = begun_observation.reference
        assert reference is not None
        payload = b"x" * MAX_STAGE_CHUNK_BYTES
        written = stage_chunk(
            carrier,
            trusted_root_path=str(root),
            relative_path="destination",
            token=_TOKEN,
            reference=reference,
            offset=0,
            data=payload,
            chunk_digest=hashlib.sha256(payload).digest(),
            plan=plan,
            deadline=Deadline.after(15),
            runtime_selection=runtime_selection(sys.executable),
        )
        recovered = stage_reconcile(
            carrier,
            trusted_root_path=str(root),
            relative_path="destination",
            token=_TOKEN,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_selection=runtime_selection(sys.executable),
        )
        recovered_observation = recovered.observation
        assert recovered_observation is not None
        debt = recovered_observation.cleanup_debt
        assert debt is not None
        cleaned = stage_cleanup(
            carrier,
            trusted_root_path=str(root),
            relative_path="destination",
            token=_TOKEN,
            cleanup_debt=debt,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_selection=runtime_selection(sys.executable),
        )

    begun_observation = begun.observation
    assert begun_observation is not None
    assert begun.dispatch is Dispatch.SENT and begun_observation.state is FileStageObservationState.CREATED
    written_observation = written.observation
    assert written_observation is not None
    assert written.dispatch is Dispatch.SENT and written_observation.state is FileStageObservationState.ACCEPTED
    recovered_observation = recovered.observation
    assert recovered_observation is not None
    assert recovered.dispatch is Dispatch.SENT and recovered_observation.state is FileStageObservationState.RECOVERED
    cleaned_observation = cleaned.observation
    assert cleaned_observation is not None
    assert cleaned.dispatch is Dispatch.SENT and cleaned_observation.state is FileStageObservationState.CLEANED
    assert len(body_sizes) == 4
    assert len(FIXED_BUNDLE.prefix) < min(body_sizes) <= max(body_sizes) < 65_536
