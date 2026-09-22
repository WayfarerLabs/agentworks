"""Core-owned custody checks for single-file helper compositions."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _file_operations as operations
from agentworks.execution._file_inventory_exchange import FileInventoryObservationState
from agentworks.execution._file_metadata_bundle import _MODULE_NAMES as METADATA_MODULES
from agentworks.execution._file_metadata_bundle import _PACKAGE as METADATA_PACKAGE
from agentworks.execution._file_metadata_exchange import FileMetadataObservationState
from agentworks.execution._file_object_exchange import FileObjectCandidateResult, FileObjectObservationState
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_operation import FileOperation, UnfinishedOwnedFile
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import CarrierIO, CarrierReport, ChannelFeatures, Deadline, PreparedInvocation
from agentworks.operations import OperationAttempt, OperationBorrow, OperationOwner
from tests.execution.files._file_read_support import LocalCarrier
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the fixed file helpers require Linux")


class CaptureStop(Exception):
    pass


class InterruptingCarrier:
    def __init__(self, control: BaseException) -> None:
        self.control = control
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, io, deadline
        self.calls += 1
        raise self.control


class ReentrantCarrier:
    def __init__(self, callback: Callable[[Deadline], None]) -> None:
        self.callback = callback
        self.inner = LocalCarrier()
        self.calls = 0
        self.rejected = False

    @property
    def features(self) -> ChannelFeatures:
        return self.inner.features

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        if self.calls == 0:
            with pytest.raises(StateError):
                self.callback(deadline)
            self.rejected = True
        self.calls += 1
        return self.inner.execute(invocation, io=io, deadline=deadline)


@pytest.fixture
def plan() -> IdentityPlan:
    gid = os.getegid()
    groups = tuple(sorted(set(os.getgroups()) | {gid}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    approved = tmp_path / "approved"
    approved.mkdir()
    return approved


def _owner(database: Database) -> OperationOwner:
    return OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "single-file-vm"),
        "single-file",
    )


def _owner_name() -> str:
    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name


def _group_name() -> str:
    import grp

    return grp.getgrgid(os.getegid()).gr_name


def test_real_stat_inventory_and_conditional_remove_share_core_custody(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    target = root / "target"
    target.write_bytes(b"content")
    listed = root / "listed"
    listed.mkdir()
    listed.joinpath("sibling").write_bytes(b"other")
    listed.joinpath("target").write_bytes(b"content")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    carrier = LocalCarrier()
    try:
        present = operation.stat(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )
        absent = operation.stat(
            carrier,
            trusted_root_path=str(root),
            relative_path="absent",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )
        inventory = operation.list_directory(
            carrier,
            trusted_root_path=str(root),
            relative_path="listed",
            max_entries=16,
            max_depth=1,
            max_encoded_bytes=4096,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )

        present_observation = present.result.observation if present.result is not None else None
        absent_observation = absent.result.observation if absent.result is not None else None
        inventory_observation = inventory.result.observation if inventory.result is not None else None
        assert present_observation is not None and present_observation.state is FileObjectObservationState.PRESENT
        assert present_observation.revision is not None
        assert absent_observation is not None and absent_observation.state is FileObjectObservationState.ABSENT
        assert inventory_observation is not None
        assert inventory_observation.state is FileInventoryObservationState.PRESENT
        assert inventory_observation.entries is not None
        assert {entry.relative_path for entry in inventory_observation.entries} == {"sibling", "target"}

        target.write_bytes(b"changed")
        refused = operation.remove(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            expected_kind=FileKind.REGULAR,
            expected_revision=present_observation.revision,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )
        refused_observation = refused.result.observation if refused.result is not None else None
        assert refused_observation is not None
        assert refused_observation.state is FileObjectObservationState.REFUSED
        assert target.exists()

        current = operation.stat(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )
        current_observation = current.result.observation if current.result is not None else None
        assert current_observation is not None and current_observation.revision is not None
        removed = operation.remove(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            expected_kind=FileKind.REGULAR,
            expected_revision=current_observation.revision,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )
        removed_observation = removed.result.observation if removed.result is not None else None
        assert removed_observation is not None and removed_observation.state is FileObjectObservationState.CHANGED
        assert not target.exists()
        assert operation.active_stats == ()
        assert operation.active_inventories == ()
        assert operation.active_removals == ()
        assert operation.unfinished_owned_files == ()
        assert database.operations.inspect(owner.ownership.scope) is not None
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_real_metadata_and_directory_paths_preserve_noop_and_refusal(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    target = root / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    link = root / "link"
    link.symlink_to(target)
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    carrier = LocalCarrier()

    def set_metadata(relative_path: str, mode: int):
        return operation.set_metadata(
            carrier,
            trusted_root_path=str(root),
            relative_path=relative_path,
            trusted_owner=_owner_name(),
            trusted_group=_group_name(),
            mode=mode,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )

    def ensure_directory(relative_path: str, mode: int):
        return operation.ensure_directory(
            carrier,
            trusted_root_path=str(root),
            relative_path=relative_path,
            trusted_owner=_owner_name(),
            trusted_group=_group_name(),
            mode=mode,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )

    try:
        changed = set_metadata("target", 0o640)
        unchanged = set_metadata("target", 0o640)
        refused = set_metadata("link", 0o640)
        created = ensure_directory("created", 0o750)
        converged = ensure_directory("created", 0o750)

        observations = [
            outcome.result.observation if outcome.result is not None else None
            for outcome in (changed, unchanged, refused, created, converged)
        ]
        assert [item.state if item is not None else None for item in observations] == [
            FileMetadataObservationState.CHANGED,
            FileMetadataObservationState.UNCHANGED,
            FileMetadataObservationState.REFUSED,
            FileMetadataObservationState.CHANGED,
            FileMetadataObservationState.UNCHANGED,
        ]
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        assert stat.S_IMODE(root.joinpath("created").stat().st_mode) == 0o750
        assert operation.active_metadata == ()
        assert operation.unfinished_owned_files == ()
        assert database.operations.inspect(owner.ownership.scope) is not None
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_real_partial_directory_creation_is_captured_without_replay(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_source = (
        "import os\n"
        "g=guest\n"
        f"m=sys.modules[{(METADATA_PACKAGE + '._file_metadata')!r}]\n"
        "def partial(parent_fd,leaf_name,**kwargs):\n"
        " os.mkdir(leaf_name,0o700,dir_fd=parent_fd)\n"
        " raise m.MetadataError(m.MetadataFailureKind.IO,m.MetadataPhase.VERIFICATION,"
        "completed_steps=(m.MetadataStep.CREATION,))\n"
        "g.ensure_directory=partial\n"
    )
    bundle = fixture_file_bundle(
        METADATA_PACKAGE,
        METADATA_MODULES,
        "_file_metadata_guest",
        patch_source,
    )
    monkeypatch.setattr("agentworks.execution._file_metadata_exchange.FIXED_BUNDLE", bundle)
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    try:
        outcome = operation.ensure_directory(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="partial",
            trusted_owner=_owner_name(),
            trusted_group=_group_name(),
            mode=0o755,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )

        observation = outcome.result.observation if outcome.result is not None else None
        assert observation is not None and observation.state is FileMetadataObservationState.PARTIAL
        assert root.joinpath("partial").is_dir()
        assert stat.S_IMODE(root.joinpath("partial").stat().st_mode) == 0o700
        assert operation.active_metadata == ()
        assert operation.unfinished_owned_files == ()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_cross_family_reentry_uses_the_same_owner(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    root.joinpath("target").write_bytes(b"content")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)

    def inventory(deadline: Deadline) -> None:
        operation.list_directory(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path=".",
            max_entries=16,
            max_depth=1,
            max_encoded_bytes=4096,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection(sys.executable),
        )

    carrier = ReentrantCarrier(inventory)
    try:
        outcome = operation.stat(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )

        assert outcome.result is not None and carrier.rejected
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_local_inventory_refusal_relinquishes_borrow_without_custody(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    carrier = LocalCarrier()
    try:
        with pytest.raises(ValidationError):
            operation.list_directory(
                carrier,
                trusted_root_path=str(root),
                relative_path="listed",
                max_entries=0,
                max_depth=1,
                max_encoded_bytes=4096,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert carrier.calls == 0
        assert operation.active_inventories == ()
        assert operation.unfinished_owned_files == ()
        owner.close()
        assert database.operations.inspect(owner.ownership.scope) is None
    finally:
        database.close()


def test_normal_outcome_is_attached_before_borrow_closes(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root.joinpath("target").write_bytes(b"content")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    observed = []
    close = OperationBorrow.close

    def observe_close(borrow: OperationBorrow) -> None:
        active = operation.active_stats[0]
        assert active.borrow is borrow
        assert active.outcome is not None
        observed.append(active.outcome)
        close(borrow)

    monkeypatch.setattr(OperationBorrow, "close", observe_close)
    try:
        outcome = operation.stat(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="target",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )

        assert observed == [outcome]
        assert operation.active_stats == ()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_multiple_unfinished_single_file_outcomes_remain_distinct(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    carriers = (LocalCarrier(), LocalCarrier())

    def retained(
        prepared: operations._PreparedStat,  # noqa: SLF001
    ) -> operations.OwnedFileOutcome[FileObjectCandidateResult]:
        return operations.OwnedFileOutcome(
            prepared.binding,
            coordination_uncertain=True,
            requires_owner_retention=True,
        )

    monkeypatch.setattr(operations._PreparedStat, "run", retained)  # noqa: SLF001
    try:
        outcomes = tuple(
            operation.stat(
                carrier,
                trusted_root_path=str(root),
                relative_path=f"target-{index}",
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )
            for index, carrier in enumerate(carriers)
        )

        retained_outcomes = operation.unfinished_owned_files
        assert len(retained_outcomes) == 2
        assert tuple(item.carrier for item in retained_outcomes) == carriers
        assert all(
            item.binding is outcome.binding and item.outcome is outcome
            for item, outcome in zip(retained_outcomes, outcomes, strict=True)
        )
        assert retained_outcomes[0] is not retained_outcomes[1]
        assert operation.active_stats == ()
    finally:
        database.close()


@pytest.mark.parametrize("failure_point", ["outcome", "fact"])
def test_fact_allocation_failure_preserves_current_binding_and_original_control(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    control = KeyboardInterrupt("single-control-canary")
    control.__cause__ = RuntimeError("unrelated-prior-cause")
    carrier = InterruptingCarrier(control)

    def fail_allocation(*args: object) -> None:
        del args
        raise MemoryError("single-allocation-canary")

    if failure_point == "outcome":
        monkeypatch.setattr(operations._State, "finish", fail_allocation)  # noqa: SLF001
    else:
        monkeypatch.setattr(operations, "OwnedFileControlFact", fail_allocation)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            operation.stat(
                carrier,
                trusted_root_path=str(root),
                relative_path="private-target",
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert raised.value is control and raised.value.__cause__ is None
        active = operation.active_stats[0]
        assert active.outcome is None
        assert active.binding.trusted_root_path == str(root)
        assert active.binding.relative_path == "private-target"
        assert active.prepared.state.binding is active.binding
        assert operation.unfinished_owned_files == ()
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


def test_capture_failure_preserves_original_fact_and_active_state(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    control = KeyboardInterrupt("capture-control-canary")
    carrier = InterruptingCarrier(control)
    captured: list[UnfinishedOwnedFile] = []

    class FailingRetention(list[UnfinishedOwnedFile]):
        def append(self, item: UnfinishedOwnedFile) -> None:
            captured.append(item)
            raise CaptureStop("capture-allocation-canary")

    operation._unfinished_owned_files = FailingRetention()  # noqa: SLF001
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            operation.stat(
                carrier,
                trusted_root_path=str(root),
                relative_path="target",
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert raised.value is control
        fact = raised.value.__cause__
        assert isinstance(fact, operations.OwnedFileControlFact)
        active = operation.active_stats[0]
        assert active.outcome is fact.outcome is captured[0].outcome
        assert active.binding is fact.outcome.binding
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


def test_metadata_lookup_fact_is_attached_before_failed_settlement(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root.joinpath("target").write_bytes(b"content")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)

    def fail_settlement(attempt: OperationAttempt) -> None:
        del attempt
        raise CaptureStop("lookup-settlement-canary")

    monkeypatch.setattr(OperationAttempt, "settle", fail_settlement)
    try:
        with pytest.raises(CaptureStop) as raised:
            operation.set_metadata(
                LocalCarrier(),
                trusted_root_path=str(root),
                relative_path="target",
                trusted_owner=_owner_name(),
                trusted_group=_group_name(),
                mode=0o640,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        fact = raised.value.__cause__
        assert isinstance(fact, operations.OwnedFileControlFact)
        assert fact.outcome.ownership_result is not None
        assert fact.outcome.result is None
        assert fact.outcome.coordination_uncertain
        assert operation.active_metadata == ()
        assert operation.unfinished_owned_files[0].outcome is fact.outcome
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()
