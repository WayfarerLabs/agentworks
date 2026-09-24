"""Core-owned custody checks for upload and JSON file calls."""

from __future__ import annotations

import gc
import json
import os
import sys
import weakref
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import ExternalError, StateError, ValidationError
from agentworks.execution import _file_upload
from agentworks.execution._file_json import (
    FileJsonChange,
    FileJsonControlFact,
    FileJsonFailure,
    FileJsonStatus,
    JsonFileStrategy,
)
from agentworks.execution._file_json import (
    _State as _JsonState,
)
from agentworks.execution._file_obligation import (
    FileCallFamily,
    FileCallObligation,
    decode_file_call_obligation,
)
from agentworks.execution._file_operation import (
    FileOperation,
    UnfinishedFileJsonUpdate,
    UnfinishedFileUpload,
)
from agentworks.execution._file_publication import Create
from agentworks.execution._file_result_transfer import reduce_file_json
from agentworks.execution._file_upload import (
    FileUploadControlFact,
    FileUploadFailure,
    FileUploadStatus,
)
from agentworks.execution._file_upload import (
    _WorkingState as _UploadWorkingState,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Failure,
    PreparedInvocation,
)
from agentworks.execution.files import FileFailureReason, FileOperationPhase
from agentworks.operations import LifecycleObligation, OperationBorrow, OperationOwner
from tests.execution.files._file_publication_support import LocalCarrier
from tests.execution.files._file_publication_support import install_fixture_bundle as install_publication_bundle
from tests.execution.files._file_read_support import install_fixture_bundle as install_read_bundle
from tests.execution.files._file_stage_support import install_fixture_bundle as install_stage_bundle
from tests.execution.files._file_upload_support import BytesSource, LostCallStdoutCarrier, new_metadata
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private file helpers require Linux")


class CaptureStop(Exception):
    pass


class NthInterruptCarrier:
    def __init__(self, call: int, control: BaseException) -> None:
        self._call = call
        self._control = control
        self._inner = LocalCarrier()
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._inner.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        self.calls += 1
        if self.calls == self._call:
            raise self._control
        return self._inner.execute(invocation, io=io, deadline=deadline)


class MissingCompletionCarrier:
    def __init__(self) -> None:
        self._inner = LocalCarrier()

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._inner.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        return replace(self._inner.execute(invocation, io=io, deadline=deadline), completion=None)


class OutputFailureCarrier:
    def __init__(self, *, stdout_complete: bool) -> None:
        self._inner = LocalCarrier()
        self._stdout_complete = stdout_complete
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._inner.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        self.calls += 1
        report = self._inner.execute(invocation, io=io, deadline=deadline)
        return replace(
            report,
            stdout=replace(report.stdout, complete=self._stdout_complete),
            failure=Failure.OUTPUT,
        )


class ReentrantCarrier:
    def __init__(self, callback: Callable[[Deadline], None]) -> None:
        self._callback = callback
        self._inner = LocalCarrier()
        self.calls = 0
        self.rejected = False

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._inner.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        if self.calls == 0:
            with pytest.raises(StateError):
                self._callback(deadline)
            self.rejected = True
        self.calls += 1
        return self._inner.execute(invocation, io=io, deadline=deadline)


class ObligationInspectingCarrier:
    def __init__(self, database: Database, owner: OperationOwner) -> None:
        self._database = database
        self._owner = owner
        self._inner = LocalCarrier()
        self.payloads: list[FileCallObligation] = []

    @property
    def features(self) -> ChannelFeatures:
        return self._inner.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._inner.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        rows = self._database.operations.list_lifecycle_obligations(self._owner.ownership)
        assert len(rows) == 1
        assert rows[0].obligation_kind == "file-call"
        self.payloads.append(decode_file_call_obligation(rows[0].payload))
        return self._inner.execute(invocation, io=io, deadline=deadline)


class ClosingLostCarrier:
    def __init__(self, owner: OperationOwner, lost_call: int) -> None:
        self._owner = owner
        self._lost_call = lost_call
        self._inner = LostCallStdoutCarrier(lost_call)
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return self._inner.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._inner.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        self.calls += 1
        if self.calls == self._lost_call:
            with pytest.raises(StateError):
                self._owner.close()
        return self._inner.execute(invocation, io=io, deadline=deadline)


@pytest.fixture(autouse=True)
def fixture_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stage_bundle(monkeypatch)
    install_publication_bundle(monkeypatch)
    install_read_bundle(monkeypatch)


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
        OperationScope(OperationResourceKind.VM, "core-file-write-vm"),
        "core-file-write",
    )


def _upload(
    operation: FileOperation,
    root: Path,
    plan: IdentityPlan,
    source: BytesSource,
    *,
    carrier=None,
    relative_path: str = "target",
):
    return operation.upload(
        carrier or LocalCarrier(),
        trusted_root_path=str(root),
        relative_path=relative_path,
        source=source,
        size=len(source.data),
        condition=Create(),
        create_metadata=new_metadata(),
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )


def _update(
    operation: FileOperation,
    root: Path,
    plan: IdentityPlan,
    source: bytes,
    strategy: JsonFileStrategy,
    *,
    carrier=None,
):
    return operation.update_json(
        carrier or LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        source=source,
        strategy=strategy,
        create=True,
        create_metadata=new_metadata(),
        max_bytes=64 * 1024,
        max_depth=64,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )


def test_real_upload_has_no_completed_custody_or_source_retention(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    source = BytesSource(b"private-upload-payload")
    source_reference = weakref.ref(source)
    try:
        outcome = _upload(operation, root, plan, source)

        assert outcome.status is FileUploadStatus.COMPLETE
        assert root.joinpath("target").read_bytes() == b"private-upload-payload"
        assert operation.active_uploads == ()
        assert operation.unfinished_uploads == ()
        assert database.operations.inspect(owner.ownership.scope) is not None
        del source
        gc.collect()
        assert source_reference() is None
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_upload_installs_one_tokenized_file_call_before_dispatch_and_resolves_it(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    carrier = ObligationInspectingCarrier(database, owner)
    try:
        outcome = _upload(operation, root, plan, BytesSource(b"custody-payload"), carrier=carrier)

        assert outcome.status is FileUploadStatus.COMPLETE
        assert carrier.payloads
        assert all(payload.family is FileCallFamily.UPLOAD for payload in carrier.payloads)
        assert all(payload.token == outcome.token for payload in carrier.payloads)
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1
        assert rows[0].state is LifecycleObligationState.RESOLVED
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_mismatched_target_refuses_before_the_owner_is_borrowed(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    target = ManagedTargetIdentity(
        ManagedTargetKind.VM,
        "other-vm",
        "v1:" + "a" * 64,
        "123e4567-e89b-12d3-a456-426614174000",
    )
    try:
        with pytest.raises(ValidationError):
            FileOperation(owner, target)

        borrow = owner.borrow()
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_json_replaces_its_tokenless_file_call_payload_before_child_dispatch(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    carrier = ObligationInspectingCarrier(database, owner)
    try:
        outcome = _update(operation, root, plan, b'{"custody":true}', "replace", carrier=carrier)

        assert outcome.status is FileJsonStatus.COMPLETE
        upload = outcome.upload_outcome
        assert upload is not None
        assert carrier.payloads[0].family is FileCallFamily.JSON_UPDATE
        assert carrier.payloads[0].token is None
        attempts = [payload.attempt for payload in carrier.payloads[1:] if payload.attempt is not None]
        assert all(payload.token == upload.token for payload in carrier.payloads[1:])
        assert len(attempts) == len(carrier.payloads[1:])
        assert attempts == sorted(attempts)
        assert all(1 <= attempt <= 8 for attempt in attempts)
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1
        assert rows[0].state is LifecycleObligationState.RESOLVED
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_registration_started_interruption_keeps_the_attached_upload_and_owner_borrow(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))

    install = OperationBorrow.install_dispatch_obligation

    def interrupt_install(
        borrow: OperationBorrow,
        obligation_id: str,
        obligation_kind: str,
        *,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        install(
            borrow,
            obligation_id,
            obligation_kind,
            payload_version=payload_version,
            payload=payload,
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", interrupt_install)
    try:
        with pytest.raises(KeyboardInterrupt):
            _upload(operation, root, plan, BytesSource(b"interrupted"))

        assert len(operation.active_uploads) == 1
        assert len(database.operations.list_lifecycle_obligations(owner.ownership)) == 1
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


def test_close_requested_before_install_releases_unregistered_active_upload(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    carrier = LocalCarrier()
    install = OperationBorrow.install_dispatch_obligation

    def close_before_install(
        borrow: OperationBorrow,
        obligation_id: str,
        obligation_kind: str,
        *,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        with pytest.raises(StateError):
            owner.close()
        return install(
            borrow,
            obligation_id,
            obligation_kind,
            payload_version=payload_version,
            payload=payload,
        )

    monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", close_before_install)
    try:
        with pytest.raises(StateError):
            _upload(operation, root, plan, BytesSource(b"unregistered"), carrier=carrier)

        assert carrier.calls == 0
        assert operation.active_uploads == ()
        assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
        owner.close()
        assert database.operations.inspect(owner.ownership.scope) is None
    finally:
        database.close()


def test_failed_pre_registration_cleanup_keeps_the_active_upload(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    install = OperationBorrow.install_dispatch_obligation

    def close_before_install(
        borrow: OperationBorrow,
        obligation_id: str,
        obligation_kind: str,
        *,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        with pytest.raises(StateError):
            owner.close()
        return install(
            borrow,
            obligation_id,
            obligation_kind,
            payload_version=payload_version,
            payload=payload,
        )

    def fail_close(borrow: OperationBorrow) -> None:
        del borrow
        raise KeyboardInterrupt

    monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", close_before_install)
    monkeypatch.setattr(OperationBorrow, "close", fail_close)
    try:
        with pytest.raises(KeyboardInterrupt):
            _upload(operation, root, plan, BytesSource(b"unreleased"))

        assert len(operation.active_uploads) == 1
        assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
    finally:
        database.close()


def test_interrupted_json_payload_publication_keeps_its_attached_child_and_owner_borrow(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))

    def interrupt_publish(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(LifecycleObligation, "publish_payload", interrupt_publish)
    try:
        with pytest.raises(KeyboardInterrupt):
            _update(operation, root, plan, b'{"interrupted":true}', "replace")

        active = operation.active_json_updates[0] if operation.active_json_updates else None
        assert active is not None and active.prepared.state.active_upload is not None
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_real_json_strategies_share_core_custody_and_preserve_noop(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    strategy: JsonFileStrategy,
) -> None:
    root.joinpath("target").write_text('{"base":1,"shared":"old"}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    source = b'{"incoming":2,"shared":"new"}'
    try:
        outcome = _update(operation, root, plan, source, strategy)

        assert outcome.status is FileJsonStatus.COMPLETE
        assert operation.active_json_updates == ()
        assert operation.unfinished_json_updates == ()
        assert database.operations.inspect(owner.ownership.scope) is not None
        content = json.loads(root.joinpath("target").read_bytes())
        if strategy == "skip-existing":
            assert outcome.change is FileJsonChange.UNCHANGED
            assert outcome.publication_attempts == 0
            assert content == {"base": 1, "shared": "old"}
        elif strategy == "replace":
            assert outcome.change is FileJsonChange.CHANGED
            assert content == {"incoming": 2, "shared": "new"}
        elif strategy == "merge-overwrite":
            assert outcome.change is FileJsonChange.CHANGED
            assert content == {"base": 1, "incoming": 2, "shared": "new"}
        else:
            assert outcome.change is FileJsonChange.CHANGED
            assert content == {"base": 1, "incoming": 2, "shared": "old"}
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("strategy", "stdout_complete"),
    [
        ("skip-existing", True),
        ("skip-existing", False),
        ("merge-overwrite", True),
        ("merge-overwrite", False),
    ],
    ids=["stat-complete", "stat-incomplete", "read-complete", "read-incomplete"],
)
def test_json_observation_carrier_failure_precedes_success_or_invalid_response(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    strategy: JsonFileStrategy,
    stdout_complete: bool,
) -> None:
    target = root / "target"
    target.write_text('{"base":1}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    carrier = OutputFailureCarrier(stdout_complete=stdout_complete)
    try:
        outcome = _update(operation, root, plan, b'{"incoming":2}', strategy, carrier=carrier)

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.OBSERVATION
        assert outcome.carrier_failure is Failure.OUTPUT
        assert outcome.publication_attempts == 0
        assert not outcome.requires_owner_retention
        assert operation.unfinished_json_updates == ()
        assert carrier.calls == 1
        assert target.read_text() == '{"base":1}'
        with pytest.raises(ExternalError) as raised:
            reduce_file_json(outcome, entity_kind="workspace file", entity_name="settings")
        assert raised.value.details is not None
        assert raised.value.details.phase is FileOperationPhase.OBSERVATION
        assert raised.value.details.reason is FileFailureReason.CARRIER_OUTPUT
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_cross_family_reentry_is_rejected_by_one_owner(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))

    def update(deadline: Deadline) -> None:
        operation.update_json(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="other",
            source=b'{"nested":true}',
            strategy="replace",
            create=True,
            create_metadata=new_metadata(),
            max_bytes=64,
            max_depth=8,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection(sys.executable),
        )

    carrier = ReentrantCarrier(update)
    try:
        outcome = _upload(operation, root, plan, BytesSource(b"payload"), carrier=carrier)

        assert outcome.status is FileUploadStatus.COMPLETE
        assert carrier.rejected
        assert not root.joinpath("other").exists()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_local_write_refusals_close_predispatch_borrows(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    upload_carrier = LocalCarrier()
    json_carrier = LocalCarrier()
    try:
        with pytest.raises(ValidationError):
            operation.upload(
                upload_carrier,
                trusted_root_path=str(root),
                relative_path="target",
                source=BytesSource(b"payload"),
                size=-1,
                condition=Create(),
                create_metadata=new_metadata(),
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )
        with pytest.raises(ValidationError):
            operation.update_json(
                json_carrier,
                trusted_root_path=str(root),
                relative_path="target",
                source=b'{"duplicate":1,"duplicate":2}',
                strategy="replace",
                create=True,
                create_metadata=new_metadata(),
                max_bytes=64,
                max_depth=8,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert upload_carrier.calls == json_carrier.calls == 0
        assert operation.active_uploads == ()
        assert operation.active_json_updates == ()
        owner.close()
    finally:
        database.close()


def test_upload_and_json_admission_overflow_closes_predispatch_borrows(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    upload_carrier = LocalCarrier()
    json_carrier = LocalCarrier()
    overflow_root = "/" + "\u00e9" * 1100
    try:
        with pytest.raises(ValidationError):
            operation.upload(
                upload_carrier,
                trusted_root_path=overflow_root,
                relative_path="target",
                source=BytesSource(b"payload"),
                size=len(b"payload"),
                condition=Create(),
                create_metadata=new_metadata(),
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )
        with pytest.raises(ValidationError):
            operation.update_json(
                json_carrier,
                trusted_root_path=overflow_root,
                relative_path="target",
                source=b'{"valid":true}',
                strategy="replace",
                create=True,
                create_metadata=new_metadata(),
                max_bytes=64,
                max_depth=8,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert upload_carrier.calls == json_carrier.calls == 0
        assert operation.active_uploads == ()
        assert operation.active_json_updates == ()
        assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
        owner.close()
    finally:
        database.close()


def test_nested_json_upload_uses_one_whole_call_borrow(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root.joinpath("target").write_text('{"existing":true}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    borrow = owner.borrow
    calls = 0

    def counted_borrow():
        nonlocal calls
        calls += 1
        return borrow()

    monkeypatch.setattr(owner, "borrow", counted_borrow)
    try:
        outcome = _update(operation, root, plan, b'{"added":true}', "merge-overwrite")

        assert outcome.status is FileJsonStatus.COMPLETE
        assert outcome.publication_attempts == 1
        assert calls == 1
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_multiple_upload_cleanup_obligations_remain_exact_and_bounded(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    carriers = (LostCallStdoutCarrier(5), LostCallStdoutCarrier(5))
    try:
        outcomes = tuple(
            _upload(
                operation,
                root,
                plan,
                BytesSource(f"payload-{index}".encode()),
                carrier=carrier,
                relative_path=f"target-{index}",
            )
            for index, carrier in enumerate(carriers)
        )

        assert all(outcome.failure is FileUploadFailure.CLEANUP for outcome in outcomes)
        assert all(outcome.scratch_cleanup_debt is not None for outcome in outcomes)
        retained = operation.unfinished_uploads
        assert len(retained) == 2
        assert tuple(item.carrier for item in retained) == carriers
        assert all(item.binding is outcome.binding for item, outcome in zip(retained, outcomes, strict=True))
        assert all(item.outcome is outcome for item, outcome in zip(retained, outcomes, strict=True))
        assert all(not hasattr(item, "source") for item in retained)
        assert operation.active_uploads == ()
    finally:
        database.close()


def test_upload_final_cleanup_hands_off_after_owner_close_was_requested(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    carrier = ClosingLostCarrier(owner, 5)
    try:
        outcome = _upload(operation, root, plan, BytesSource(b"payload"), carrier=carrier)

        assert outcome.failure is FileUploadFailure.CLEANUP
        assert outcome.scratch_cleanup_debt is not None
        assert len(operation.unfinished_uploads) == 1
        assert operation.active_uploads == ()
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


def test_json_uncertain_publication_retains_child_upload_without_payload(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
) -> None:
    root.joinpath("target").write_text('{"existing":true}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    carrier = LostCallStdoutCarrier(4)
    source = b'{"private-json-canary":true}'
    try:
        outcome = _update(operation, root, plan, source, "merge-overwrite", carrier=carrier)

        assert outcome.status is FileJsonStatus.UNCERTAIN
        assert outcome.upload_outcome is not None
        assert outcome.upload_outcome.publication_uncertain
        assert outcome.upload_outcome.scratch_cleanup_debt is not None
        retained = operation.unfinished_json_updates
        assert len(retained) == 1
        assert retained[0].carrier is carrier
        assert retained[0].binding is outcome.binding
        assert retained[0].outcome is outcome
        assert source.decode() not in repr(retained[0])
        assert operation.active_json_updates == ()
        borrow = owner.borrow()
        borrow.close()
    finally:
        database.close()


def test_ownership_result_is_attached_before_failed_settlement_and_outcome_allocation(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))

    def fail_outcome_allocation(state: _UploadWorkingState) -> None:
        assert state.ownership_result is not None
        raise MemoryError("outcome-allocation-canary")

    monkeypatch.setattr(_UploadWorkingState, "finish", fail_outcome_allocation)
    try:
        with pytest.raises(MemoryError):
            _upload(
                operation,
                root,
                plan,
                BytesSource(b"payload"),
                carrier=MissingCompletionCarrier(),
            )

        active = operation.active_uploads[0]
        result = active.prepared.state.ownership_result
        assert result is not None and result.observation is not None
        assert active.prepared.state.operation.has_outstanding_attempt
        assert operation.unfinished_uploads == ()
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


def test_upload_retention_failure_preserves_original_control_and_attached_source(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    control = KeyboardInterrupt("upload-control-canary")
    carrier = NthInterruptCarrier(1, control)
    source = BytesSource(b"private-source-canary")
    captured: list[UnfinishedFileUpload] = []

    def fail_retention(upload: UnfinishedFileUpload) -> None:
        captured.append(upload)
        raise CaptureStop("upload-retention-canary")

    monkeypatch.setattr(operation, "_retain_unfinished_upload", fail_retention)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _upload(operation, root, plan, source, carrier=carrier)

        assert raised.value is control
        fact = raised.value.__cause__
        assert isinstance(fact, FileUploadControlFact)
        active = operation.active_uploads[0]
        assert active.outcome is fact.outcome is captured[0].outcome
        assert active.prepared.workflow._source is source  # noqa: SLF001
        assert active.prepared.state.token == fact.outcome.token
        assert operation.unfinished_uploads == ()
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


@pytest.mark.parametrize("failure_point", ["outcome", "fact"])
def test_upload_allocation_failure_cannot_reuse_source_exception_cause(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    try:
        previous = _upload(operation, root, plan, BytesSource(b"prior"), relative_path="prior")
        prior_fact = FileUploadControlFact(previous)
        control = KeyboardInterrupt("source-control-canary")
        control.__cause__ = prior_fact

        class InterruptingSource(BytesSource):
            def try_read(self, limit: int) -> bytes:
                raise control

        source = InterruptingSource(b"payload")
        allocation_causes: list[BaseException | None] = []

        def fail_allocation(*args: object) -> None:
            allocation_causes.append(control.__cause__)
            raise MemoryError("upload-allocation-canary")

        if failure_point == "outcome":
            monkeypatch.setattr(_UploadWorkingState, "finish", fail_allocation)
        else:
            monkeypatch.setattr(_file_upload, "FileUploadControlFact", fail_allocation)

        with pytest.raises(KeyboardInterrupt) as raised:
            _upload(operation, root, plan, source)

        assert allocation_causes == [prior_fact]
        assert raised.value is control and raised.value.__cause__ is None
        active = operation.active_uploads[0]
        assert active.outcome is None
        assert active.prepared.state.token != previous.token
        assert operation.unfinished_uploads == ()
        with pytest.raises(StateError):
            owner.borrow()
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()


@pytest.mark.parametrize("failure_point", ["outcome", "fact"])
def test_json_child_ownership_lookup_allocation_failure_preserves_original_control(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    previous = _upload(operation, root, plan, BytesSource(b"prior"), relative_path="prior")
    prior_fact = FileUploadControlFact(previous)
    control = KeyboardInterrupt("json-child-control-canary")
    control.__cause__ = prior_fact
    carrier = NthInterruptCarrier(2, control)
    allocation_causes: list[BaseException | None] = []

    def fail_allocation(*args: object) -> None:
        allocation_causes.append(control.__cause__)
        raise MemoryError("child-allocation-canary")

    if failure_point == "outcome":
        monkeypatch.setattr(_UploadWorkingState, "finish", fail_allocation)
    else:
        monkeypatch.setattr(_file_upload, "FileUploadControlFact", fail_allocation)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _update(operation, root, plan, b'{"private-child":true}', "replace", carrier=carrier)

        assert allocation_causes == [prior_fact]
        assert raised.value is control and raised.value.__cause__ is None
        active = operation.active_json_updates[0]
        assert active.outcome is None
        child = active.prepared.state.active_upload
        assert child is not None
        assert child.state.token != previous.token
        assert operation.unfinished_json_updates == ()
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


def test_json_retention_failure_preserves_original_control_and_parent_fact(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    control = KeyboardInterrupt("json-control-canary")
    carrier = NthInterruptCarrier(1, control)
    captured: list[UnfinishedFileJsonUpdate] = []

    def fail_retention(update: UnfinishedFileJsonUpdate) -> None:
        captured.append(update)
        raise CaptureStop("json-retention-canary")

    monkeypatch.setattr(operation, "_retain_unfinished_json_update", fail_retention)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _update(operation, root, plan, b'{"private-parent":true}', "replace", carrier=carrier)

        assert raised.value is control
        fact = raised.value.__cause__
        assert isinstance(fact, FileJsonControlFact)
        active = operation.active_json_updates[0]
        assert active.outcome is captured[0].outcome
        assert active.outcome is fact.outcome
        assert active.prepared.state.active_upload is None
        assert operation.unfinished_json_updates == ()
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


def test_json_child_capture_failure_does_not_reuse_unrelated_upload_fact(
    tmp_path: Path,
    root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner, target_for_owner(owner))
    previous = _upload(operation, root, plan, BytesSource(b"prior"), relative_path="prior")
    prior_fact = FileUploadControlFact(previous)
    control = KeyboardInterrupt("child-capture-control-canary")

    def fail_child_capture(
        self: _JsonState,
        outcome: _file_upload.FileUploadOutcome,
    ) -> None:
        del self, outcome
        raise control from prior_fact

    monkeypatch.setattr(_JsonState, "capture_upload", fail_child_capture)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _update(operation, root, plan, b'{"private-child":true}', "replace")

        assert raised.value is control and raised.value.__cause__ is None
        active = operation.active_json_updates[0]
        assert active.outcome is None
        child = active.prepared.state.active_upload
        assert child is not None and child.control_outcome is None
        assert child.state.token != previous.token
        assert operation.unfinished_json_updates == ()
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()
