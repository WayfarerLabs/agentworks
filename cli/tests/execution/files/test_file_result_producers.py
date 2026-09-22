"""Producer-path checks for public file-result chronology."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import ConflictError, ErrorDetails, ExternalError, StateError, UncertainOutcomeError
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_result import (
    reduce_file_inventory,
    reduce_file_metadata,
    reduce_file_remove,
    reduce_file_stat,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, Failure, PreparedInvocation
from agentworks.execution.files import Change, FileFailureReason, FileOperationPhase
from agentworks.operations import OperationOwner
from tests.execution.files._file_read_support import LocalCarrier
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the fixed file helpers require Linux")

_DATA = b"public result"
_CONTEXT = {"entity_kind": "workspace file", "entity_name": "settings"}


class _ReturnedReportCarrier:
    def __init__(
        self,
        *,
        missing_completion_call: int | None = None,
        expiry_call: int | None = None,
        returned_failure: Failure | None = None,
    ) -> None:
        self._carrier = LocalCarrier()
        self._missing_completion_call = missing_completion_call
        self._expiry_call = expiry_call
        self._returned_failure = returned_failure
        self.calls = 0

    @property
    def features(self):
        return self._carrier.features

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        report = self._carrier.execute(invocation, io=io, deadline=deadline)
        if self.calls == self._expiry_call:
            object.__setattr__(deadline, "expires_at", 0.0)
        if self.calls == self._missing_completion_call:
            return replace(report, completion=None, failure=self._returned_failure)
        return report


@pytest.fixture
def real_operation(
    tmp_path: Path,
) -> Iterator[tuple[Path, OperationOwner, FileOperation, IdentityPlan, RuntimeSelection]]:
    root = tmp_path / "approved"
    root.mkdir()
    gid = os.getegid()
    plan = IdentityPlan(
        IdentityExpectation(os.geteuid(), gid, tuple(sorted(set(os.getgroups()) | {gid}))),
        IdentityMode.DIRECT,
    )
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-result-producer-vm"),
        "file-result",
    )
    try:
        yield root, owner, FileOperation(owner, target_for_owner(owner)), plan, runtime_selection(sys.executable)
    finally:
        database.close()


def _local_identity_names() -> tuple[str, str]:
    import grp
    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name, grp.getgrgid(os.getegid()).gr_name


def _details(error: ExternalError | StateError) -> ErrorDetails:
    assert error.details is not None
    return error.details


def test_real_root_refusal_uses_callers_observation_or_removal_phase(
    real_operation: tuple[Path, OperationOwner, FileOperation, IdentityPlan, RuntimeSelection],
) -> None:
    root, owner, operation, plan, runtime = real_operation
    target = root / "target"
    target.write_bytes(_DATA)
    linked_root = root.parent / "approved-link"
    linked_root.symlink_to(root, target_is_directory=True)

    stat_outcome = operation.stat(
        LocalCarrier(),
        trusted_root_path=str(linked_root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )
    with pytest.raises(StateError) as raised_stat:
        reduce_file_stat(stat_outcome, **_CONTEXT)
    assert _details(raised_stat.value).reason is FileFailureReason.REFUSED
    assert _details(raised_stat.value).phase is FileOperationPhase.OBSERVATION

    present = operation.stat(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )
    assert present.result is not None and present.result.observation is not None
    revision = present.result.observation.revision
    assert revision is not None
    remove_outcome = operation.remove(
        LocalCarrier(),
        trusted_root_path=str(linked_root),
        relative_path="target",
        expected_kind=FileKind.REGULAR,
        expected_revision=revision,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )
    with pytest.raises(StateError) as raised_remove:
        reduce_file_remove(remove_outcome, **_CONTEXT)
    assert _details(raised_remove.value).reason is FileFailureReason.REFUSED
    assert _details(raised_remove.value).phase is FileOperationPhase.REMOVAL
    assert target.read_bytes() == _DATA
    assert not stat_outcome.requires_owner_retention
    assert not present.requires_owner_retention
    assert not remove_outcome.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_real_removal_retains_confirmed_change_across_missing_termination(
    real_operation: tuple[Path, OperationOwner, FileOperation, IdentityPlan, RuntimeSelection],
) -> None:
    root, owner, operation, plan, runtime = real_operation
    target = root / "target"
    target.write_bytes(_DATA)
    observed = operation.stat(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )
    assert observed.result is not None and observed.result.observation is not None
    revision = observed.result.observation.revision
    assert revision is not None
    carrier = _ReturnedReportCarrier(missing_completion_call=1)

    outcome = operation.remove(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        expected_kind=FileKind.REGULAR,
        expected_revision=revision,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )

    assert outcome.pending_remote_effects and outcome.requires_owner_retention
    assert not target.exists() and carrier.calls == 1
    with pytest.raises(ExternalError) as raised:
        reduce_file_remove(outcome, **_CONTEXT)
    assert not isinstance(raised.value, UncertainOutcomeError)
    assert _details(raised.value).reason is FileFailureReason.TERMINATION
    assert _details(raised.value).effect is Change.CHANGED
    with pytest.raises(StateError):
        owner.close()


@pytest.mark.parametrize(
    ("initial_mode", "requested_mode", "effect"),
    [(0o600, 0o640, Change.CHANGED), (0o640, 0o640, Change.UNCHANGED)],
    ids=["changed", "unchanged"],
)
def test_real_metadata_retains_confirmed_effect_across_missing_termination(
    real_operation: tuple[Path, OperationOwner, FileOperation, IdentityPlan, RuntimeSelection],
    initial_mode: int,
    requested_mode: int,
    effect: Change,
) -> None:
    root, owner, operation, plan, runtime = real_operation
    target = root / "target"
    target.write_bytes(_DATA)
    target.chmod(initial_mode)
    owner_name, group_name = _local_identity_names()
    carrier = _ReturnedReportCarrier(missing_completion_call=2)

    outcome = operation.set_metadata(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        trusted_owner=owner_name,
        trusted_group=group_name,
        mode=requested_mode,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )

    assert outcome.pending_remote_effects and outcome.requires_owner_retention
    assert stat.S_IMODE(target.stat().st_mode) == requested_mode and carrier.calls == 2
    with pytest.raises(ExternalError) as raised:
        reduce_file_metadata(outcome, **_CONTEXT)
    assert not isinstance(raised.value, UncertainOutcomeError)
    assert _details(raised.value).reason is FileFailureReason.TERMINATION
    assert _details(raised.value).effect is effect
    with pytest.raises(StateError):
        owner.close()


def test_real_helper_refusals_precede_later_host_deadline(
    real_operation: tuple[Path, OperationOwner, FileOperation, IdentityPlan, RuntimeSelection],
) -> None:
    root, owner, operation, plan, runtime = real_operation
    target = root / "target"
    target.write_bytes(_DATA)
    link = root / "link"
    link.symlink_to(target)
    owner_name, group_name = _local_identity_names()

    stat_outcome = operation.stat(
        _ReturnedReportCarrier(expiry_call=1),
        trusted_root_path=str(root),
        relative_path="link",
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )
    with pytest.raises(StateError) as raised_stat:
        reduce_file_stat(stat_outcome, **_CONTEXT)
    assert _details(raised_stat.value).reason is FileFailureReason.UNSUPPORTED

    inventory_outcome = operation.list_directory(
        _ReturnedReportCarrier(expiry_call=1),
        trusted_root_path=str(root),
        relative_path="target",
        max_entries=8,
        max_depth=1,
        max_encoded_bytes=4096,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )
    with pytest.raises(StateError) as raised_inventory:
        reduce_file_inventory(inventory_outcome, **_CONTEXT)
    assert _details(raised_inventory.value).reason is FileFailureReason.REFUSED

    present = operation.stat(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )
    assert present.result is not None and present.result.observation is not None
    revision = present.result.observation.revision
    assert revision is not None
    target.write_bytes(b"changed")
    remove_outcome = operation.remove(
        _ReturnedReportCarrier(expiry_call=1),
        trusted_root_path=str(root),
        relative_path="target",
        expected_kind=FileKind.REGULAR,
        expected_revision=revision,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )
    with pytest.raises(ConflictError):
        reduce_file_remove(remove_outcome, **_CONTEXT)

    metadata_outcome = operation.set_metadata(
        _ReturnedReportCarrier(expiry_call=2),
        trusted_root_path=str(root),
        relative_path="link",
        trusted_owner=owner_name,
        trusted_group=group_name,
        mode=0o640,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime,
    )
    with pytest.raises(StateError) as raised_metadata:
        reduce_file_metadata(metadata_outcome, **_CONTEXT)
    assert _details(raised_metadata.value).reason is FileFailureReason.UNSUPPORTED
    assert all(
        outcome.deadline_exceeded for outcome in (stat_outcome, inventory_outcome, remove_outcome, metadata_outcome)
    )
    assert not any(
        outcome.requires_owner_retention
        for outcome in (stat_outcome, inventory_outcome, remove_outcome, metadata_outcome)
    )
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize("operation_name", ["stat", "inventory"])
def test_real_read_candidate_failure_precedes_later_custody_state(
    real_operation: tuple[Path, OperationOwner, FileOperation, IdentityPlan, RuntimeSelection],
    operation_name: str,
) -> None:
    root, owner, operation, plan, runtime = real_operation
    target = root / "target"
    target.write_bytes(_DATA)
    listed = root / "listed"
    listed.mkdir()
    listed.joinpath("child").write_bytes(_DATA)
    carrier = _ReturnedReportCarrier(
        missing_completion_call=1,
        returned_failure=Failure.OUTPUT,
    )
    if operation_name == "stat":
        stat_outcome = operation.stat(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime,
        )
        with pytest.raises(ExternalError) as raised:
            reduce_file_stat(stat_outcome, **_CONTEXT)
        assert stat_outcome.result is not None and stat_outcome.result.observation is not None
        assert stat_outcome.pending_remote_effects and stat_outcome.requires_owner_retention
    else:
        inventory_outcome = operation.list_directory(
            carrier,
            trusted_root_path=str(root),
            relative_path="listed",
            max_entries=8,
            max_depth=1,
            max_encoded_bytes=4096,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime,
        )
        with pytest.raises(ExternalError) as raised:
            reduce_file_inventory(inventory_outcome, **_CONTEXT)
        assert inventory_outcome.result is not None and inventory_outcome.result.observation is not None
        assert inventory_outcome.pending_remote_effects and inventory_outcome.requires_owner_retention

    assert carrier.calls == 1
    assert _details(raised.value).reason is FileFailureReason.CARRIER_OUTPUT
    with pytest.raises(StateError):
        owner.close()
