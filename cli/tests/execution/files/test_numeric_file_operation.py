"""Constructor-bound numeric context through core custody and durable updates.

Exchange doubles model dispatch evidence only; these are not native credential
or root-entry proofs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _file_download, _file_json, _file_operation, _file_operations, _file_upload
from agentworks.execution._account import (
    AccountObservationState,
    FileOwnershipObservation,
    FileOwnershipResolutionResult,
)
from agentworks.execution._account_protocol import FileOwnership
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_effect_gate import FileEffectGateBinding
from agentworks.execution._file_effect_gate_exchange import (
    GateControlCandidateResult,
    GateControlObservation,
    GateControlObservationState,
)
from agentworks.execution._file_gate_setup import FileEffectGateSetup
from agentworks.execution._file_inventory_exchange import FileInventoryCandidateResult
from agentworks.execution._file_metadata_exchange import FileMetadataCandidateResult
from agentworks.execution._file_object_exchange import (
    FileObjectCandidateResult,
    FileObjectObservation,
    FileObjectObservationState,
)
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_obligation import FileCallObligation, decode_file_call_obligation
from agentworks.execution._file_operation import FileOperation, PackageUploadMember, PackageUploadStopped
from agentworks.execution._file_publication import PublicationFailureKind, PublicationPhase, Replace
from agentworks.execution._file_publication_protocol import FilePublicationFailureCode, FilePublicationFailureControl
from agentworks.execution._file_read import FileReadCandidateResult, FileReadObservation, FileReadObservationState
from agentworks.execution._file_snapshot_exchange import FileSnapshotCandidateResult
from agentworks.execution._file_stage_exchange import FileStageCandidateResult
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution._file_upload import FileUploadFailure, FileUploadOutcome, FileUploadStatus, _PreparedUpload
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    _NumericGuestBootstrap,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
)
from agentworks.operations import LifecycleObligation, OperationOwner
from tests.execution.files._file_download_support import BytesSink
from tests.execution.files._file_upload_support import BytesSource, new_metadata
from tests.execution.files._target_support import target_for_owner

_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)
_ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
_BODY = IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DEMOTE)
_BOOTSTRAP = _NumericGuestBootstrap(_ROOT, _GUEST)
_READY = RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, "/usr/bin/python3")
_REVISION = FileRevision(FileStat(1, 2, 0o100600, 1, 1001, 1002, 0, 1, 1))


class EvidenceCarrier:
    """Provide explicit dispatch evidence without running a helper."""

    features = ChannelFeatures()

    def __init__(self, *, completion: ExitStatus | None = None, control: bool = False) -> None:
        self.completion = completion
        self.control = control
        self.calls = 0

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        del invocation, io

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        del invocation, io, deadline
        self.calls += 1
        if self.control:
            raise RuntimeError("dispatch reply lost")
        return CarrierReport(Dispatch.SENT, completion=self.completion)


def _context(tmp_path: Path, *, numeric: bool = True) -> tuple[Database, OperationOwner, FileOperation]:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "numeric-vm"), "files")
    target = replace(target_for_owner(owner), boot_id=vm_guest_boot_id(_GUEST))
    return database, owner, FileOperation(owner, target, bootstrap=_BOOTSTRAP if numeric else None)


def _row(
    database: Database, owner: OperationOwner, *, version: int = 2, body: IdentityPlan = _BODY
) -> FileCallObligation:
    (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    record = decode_file_call_obligation(row.payload)
    assert row.payload_version == record.payload_version == version
    assert record.bootstrap == (_BOOTSTRAP if version == 2 else None)
    assert record.identity_plan == body
    return record


def _dispatch(operation: BorrowedFixedHelperCarrier, deadline: Deadline) -> CarrierReport:
    return operation.execute(PreparedInvocation(("scoped-evidence",)), io=CarrierIO(), deadline=deadline)


def _ownership(
    operation: BorrowedFixedHelperCarrier, _owner: str, _group: str, deadline: Deadline, _selection: RuntimeSelection
) -> FileOwnershipResolutionResult:
    report = _dispatch(operation, deadline)
    # Ownership resolution is a separate control exchange.
    return FileOwnershipResolutionResult(
        report.dispatch,
        report.completion,
        None,
        None,
        _READY,
        FileOwnershipObservation(AccountObservationState.RESOLVED, FileOwnership(1001, 1002)),
    )


def _call(operation: FileOperation, carrier: EvidenceCarrier, family: str, **extra: Any) -> Any:
    common = dict(
        trusted_root_path="/data",
        relative_path="file",
        plan=_BODY,
        deadline=Deadline.after(30),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
    )
    common.update(extra)
    if family == "download":
        return operation.download(carrier, sink=BytesSink(), max_bytes=64, **common)
    if family == "upload":
        return operation.upload(
            carrier, source=BytesSource(b""), size=0, condition=Replace(), create_metadata=new_metadata(), **common
        )
    if family == "package":
        common.pop("relative_path")
        return operation.upload_package(
            carrier,
            members=(PackageUploadMember("file", BytesSource(b""), 0, Replace(), new_metadata()),),
            checkpoint=lambda *_: None,
            **common,
        )
    if family == "json":
        return operation.update_json(
            carrier,
            source=b"{}",
            strategy=common.pop("strategy", "replace"),
            create=True,
            create_metadata=new_metadata(),
            max_bytes=64,
            max_depth=4,
            **common,
        )
    if family == "stat":
        return operation.stat(carrier, **common)
    if family == "inventory":
        return operation.list_directory(carrier, max_entries=10, max_depth=2, max_encoded_bytes=1024, **common)
    if family == "remove":
        return operation.remove(carrier, expected_kind=FileKind.REGULAR, expected_revision=_REVISION, **common)
    method = operation.set_metadata if family == "metadata" else operation.ensure_directory
    return method(carrier, trusted_owner="owner", trusted_group="group", mode=0o600, **common)


@pytest.mark.parametrize(
    "family", ["download", "upload", "package", "json", "stat", "inventory", "remove", "metadata", "directory"]
)
@pytest.mark.parametrize("control", [False, True])
def test_real_workflows_retain_context_and_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family: str, control: bool
) -> None:
    database, owner, operation = _context(tmp_path)
    seen: list[_NumericGuestBootstrap] = []

    def exchange(op: Any, **kwargs: Any) -> Any:
        assert kwargs["bootstrap"] is _BOOTSTRAP
        assert kwargs["plan"] is _BODY
        seen.append(kwargs["bootstrap"])
        _row(database, owner)
        report = _dispatch(op, kwargs["deadline"])
        result_type = {
            "download": FileSnapshotCandidateResult,
            "upload": FileStageCandidateResult,
            "package": FileStageCandidateResult,
            "json": FileObjectCandidateResult,
            "stat": FileObjectCandidateResult,
            "inventory": FileInventoryCandidateResult,
            "remove": FileObjectCandidateResult,
            "metadata": FileMetadataCandidateResult,
            "directory": FileMetadataCandidateResult,
        }[family]
        return result_type(report.dispatch, report.completion, None, None, _READY, None)

    monkeypatch.setattr(_file_operations, "resolve_file_ownership", _ownership)
    # Ownership succeeds before the metadata body exchange.
    carrier = EvidenceCarrier(control=control)
    if family in {"metadata", "directory"}:

        def ownership(op: Any, *args: Any) -> FileOwnershipResolutionResult:
            old_control, old_completion = carrier.control, carrier.completion
            carrier.control, carrier.completion = False, ExitStatus(code=0)
            try:
                return _ownership(op, *args)
            finally:
                carrier.control, carrier.completion = old_control, old_completion

        monkeypatch.setattr(_file_operations, "resolve_file_ownership", ownership)
    module, name = {
        "download": (_file_download, "snapshot_begin"),
        "upload": (_file_upload, "stage_begin"),
        "package": (_file_upload, "stage_begin"),
        "json": (_file_json, "stat_file"),
        "stat": (_file_operations, "exchange_stat_file"),
        "inventory": (_file_operations, "exchange_list_directory"),
        "remove": (_file_operations, "exchange_remove_file"),
        "metadata": (_file_operations, "set_file_metadata"),
        "directory": (_file_operations, "ensure_file_directory"),
    }[family]
    monkeypatch.setattr(module, name, exchange)
    if control:
        with pytest.raises(RuntimeError) as caught:
            _call(operation, carrier, family)
        assert caught.value.__cause__ is not None
        fact = caught.value.__cause__
        assert isinstance(
            fact,
            _file_download.FileDownloadControlFact
            | _file_upload.FileUploadControlFact
            | _file_json.FileJsonControlFact
            | _file_operations.OwnedFileControlFact,
        )
        outcome = fact.outcome
    elif family == "package":
        with pytest.raises(PackageUploadStopped) as caught_package:
            _call(operation, carrier, family)
        outcome = caught_package.value.outcome
    else:
        outcome = _call(operation, carrier, family)
    assert outcome.binding.bootstrap is _BOOTSTRAP
    assert outcome.requires_owner_retention
    assert seen == [_BOOTSTRAP]
    _row(database, owner)
    with pytest.raises(StateError):
        owner.close()


@pytest.mark.parametrize("kind", [OperationResourceKind.VM, OperationResourceKind.PLATFORM_HOST])
def test_constructor_refuses_inapplicable_context_without_obligations(
    tmp_path: Path, kind: OperationResourceKind
) -> None:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(database.operations, OperationScope(kind, "wrong"), "files")
    with pytest.raises(ValidationError):
        FileOperation(owner, target_for_owner(owner), bootstrap=_BOOTSTRAP)
    assert not database.operations.list_pending_lifecycle_obligations(owner.ownership)
    owner.close()


@pytest.mark.parametrize("family", ["download", "upload", "package"])
def test_gate_setup_and_promotion_keep_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family: str) -> None:
    database, owner, operation = _context(tmp_path)
    target = replace(target_for_owner(owner), boot_id=vm_guest_boot_id(_GUEST))
    setup = FileEffectGateSetup.for_target(target, _BODY.expected.euid, _GUEST)
    gate = FileEffectGateBinding(setup.path, b"a" * 16, b"b" * 16, _GUEST, _BODY.expected.euid, target.name, 1, 2)
    seen: list[str] = []

    def setup_exchange(op: Any, **kwargs: Any) -> GateControlCandidateResult:
        assert kwargs["bootstrap"] is _BOOTSTRAP
        assert kwargs["plan"] is _BODY
        assert _row(database, owner).gate_setup == setup
        seen.append("setup")
        _dispatch(op, kwargs["deadline"])
        return GateControlCandidateResult(
            Dispatch.SENT,
            ExitStatus(code=0),
            None,
            None,
            _READY,
            GateControlObservation(GateControlObservationState.RESOLVED, gate),
        )

    def body_exchange(op: Any, **kwargs: Any) -> Any:
        assert kwargs["bootstrap"] is _BOOTSTRAP
        assert kwargs["effect_gate"] == gate
        record = _row(database, owner)
        assert record.gate_setup is None and record.effect_gate == gate
        seen.append("body")
        _dispatch(op, kwargs["deadline"])
        result_type = FileSnapshotCandidateResult if family == "download" else FileStageCandidateResult
        return result_type(Dispatch.SENT, None, None, None, _READY, None)

    monkeypatch.setattr(_file_operation, "exchange_file_effect_gate", setup_exchange)
    monkeypatch.setattr(_file_download, "snapshot_begin", body_exchange)
    monkeypatch.setattr(_file_upload, "stage_begin", body_exchange)
    if family == "package":
        with pytest.raises(PackageUploadStopped):
            _call(operation, EvidenceCarrier(), family, gate_setup=setup)
        binding = operation.unfinished_package_uploads[0].binding
    else:
        binding = _call(operation, EvidenceCarrier(), family, gate_setup=setup).binding
    assert binding.bootstrap is _BOOTSTRAP and binding.effect_gate == gate
    assert seen == ["setup", "body"]
    _row(database, owner)


@pytest.mark.parametrize("failure", ["runtime", "gate", "size"])
def test_invalid_aggregate_refuses_before_registration_and_source(tmp_path: Path, failure: str) -> None:
    database, owner, operation = _context(tmp_path)
    source = BytesSource(b"payload")
    carrier = EvidenceCarrier()
    extra: dict[str, Any] = {}
    if failure == "runtime":
        extra["runtime_selection"] = RuntimeSelection(RuntimeTargetOS.DARWIN)
    elif failure == "gate":
        other_guest = replace(_GUEST, instance_marker="b" * 32)
        target = replace(target_for_owner(owner), boot_id=vm_guest_boot_id(_GUEST))
        setup = FileEffectGateSetup.for_target(target, _BODY.expected.euid, other_guest)
        extra["gate_setup"] = setup
    else:
        extra["trusted_root_path"] = "/" + "x" * 3900
        extra["relative_path"] = "y" * 3900
    common = dict(
        trusted_root_path="/data",
        relative_path="file",
        plan=_BODY,
        deadline=Deadline.after(30),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
    )
    common.update(extra)
    with pytest.raises(ValidationError):
        operation.upload(carrier, source=source, size=7, condition=Replace(), create_metadata=new_metadata(), **common)
    assert source.calls == carrier.calls == 0
    assert not database.operations.list_pending_lifecycle_obligations(owner.ownership)
    owner.close()


@pytest.mark.parametrize("numeric", [False, True])
@pytest.mark.parametrize("lost_reply", [False, True])
def test_package_consecutive_children_exact_publication_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, numeric: bool, lost_reply: bool
) -> None:
    database, owner, operation = _context(tmp_path, numeric=numeric)
    version = 2 if numeric else 1
    checkpoints: list[int] = []
    publications: list[tuple[int, int, bytes]] = []
    original_publish = LifecycleObligation.publish_payload

    def publish(self: LifecycleObligation, *, expected_revision: int, payload_version: int, payload: bytes):
        publications.append((expected_revision, payload_version, payload))
        result = original_publish(
            self, expected_revision=expected_revision, payload_version=payload_version, payload=payload
        )
        if lost_reply and len(publications) == 1:
            raise RuntimeError("publication committed but reply lost")
        return result

    def run(self: _PreparedUpload) -> FileUploadOutcome:
        record = _row(database, owner, version=version)
        assert record.batch_index == len(checkpoints)
        assert record.token == self.state.token
        assert self.binding is self.state.binding
        assert self.binding.bootstrap is (_BOOTSTRAP if numeric else None)
        _dispatch(self.state.operation, Deadline.after(30))
        assert self.state.operation.settle(Dispatch.SENT, ExitStatus(code=0))
        return FileUploadOutcome(
            FileUploadStatus.COMPLETE, self.binding, self.state.token, 0, 0, publication_confirmed=True
        )

    def checkpoint(index: int, _outcome: FileUploadOutcome) -> None:
        assert _row(database, owner, version=version).batch_index == index
        checkpoints.append(index)
        if index == 2:
            raise RuntimeError("checkpoint committed but reply lost")

    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
    monkeypatch.setattr(_PreparedUpload, "run", run)
    with pytest.raises(RuntimeError):
        operation.upload_package(
            EvidenceCarrier(completion=ExitStatus(code=0)),
            trusted_root_path="/data",
            members=tuple(
                PackageUploadMember(f"file-{i}", BytesSource(b""), 0, Replace(), new_metadata()) for i in range(3)
            ),
            checkpoint=checkpoint,
            plan=_BODY,
            deadline=Deadline.after(30),
            runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        )
    assert checkpoints == [0, 1, 2]
    assert len(publications) == (3 if lost_reply else 2)
    assert all(publication[1] == version for publication in publications)
    if lost_reply:
        assert publications[0] == publications[1]
    assert _row(database, owner, version=version).batch_index == 2
    retained = operation.unfinished_package_uploads[0]
    assert retained.index == 2 and retained.checkpoint_pending
    assert retained.binding.bootstrap is (_BOOTSTRAP if numeric else None)


def test_json_child_conflict_retry_and_retained_update_keep_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, operation = _context(tmp_path)
    attempts: list[int] = []
    tokens: list[bytes] = []

    def read(op: Any, **kwargs: Any) -> FileReadCandidateResult:
        assert kwargs["bootstrap"] is _BOOTSTRAP
        _row(database, owner)
        _dispatch(op, kwargs["deadline"])
        return FileReadCandidateResult(
            Dispatch.SENT, ExitStatus(code=0), None, None, _READY, FileReadObservation(FileReadObservationState.ABSENT)
        )

    def run(self: _PreparedUpload) -> FileUploadOutcome:
        assert self.binding is self.state.binding
        assert self.binding.bootstrap is _BOOTSTRAP
        record = _row(database, owner)
        assert record.token == self.state.token
        attempts.append(record.attempt)
        tokens.append(self.state.token)
        if len(attempts) == 1:
            return FileUploadOutcome(
                FileUploadStatus.FAILED,
                self.binding,
                self.state.token,
                0,
                0,
                failure=FileUploadFailure.PUBLICATION,
                publication_failure=FilePublicationFailureControl(
                    FilePublicationFailureCode.PUBLICATION,
                    publication_kind=PublicationFailureKind.CONFLICT,
                    publication_phase=PublicationPhase.CONDITION,
                ),
            )
        return FileUploadOutcome(
            FileUploadStatus.UNCERTAIN,
            self.binding,
            self.state.token,
            0,
            0,
            pending_remote_effects=True,
            requires_owner_retention=True,
        )

    monkeypatch.setattr(_file_json, "read_file", read)
    monkeypatch.setattr(_PreparedUpload, "run", run)
    outcome = _call(operation, EvidenceCarrier(), "json", strategy="merge-overwrite")
    assert attempts == [1, 2] and tokens[0] != tokens[1]
    assert outcome.binding.bootstrap is _BOOTSTRAP and outcome.requires_owner_retention
    record = _row(database, owner)
    assert record.attempt == 2 and record.token == tokens[1]
    assert operation.unfinished_json_updates[0].binding.bootstrap is _BOOTSTRAP


def test_configured_root_body_is_separate_from_root_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, owner, operation = _context(tmp_path)
    body = IdentityPlan(IdentityExpectation(0, 20, (20, 30)), IdentityMode.DIRECT)

    def exchange(op: BorrowedFixedHelperCarrier, **kwargs: Any) -> FileObjectCandidateResult:
        assert kwargs["plan"] is body and kwargs["bootstrap"] is _BOOTSTRAP
        _row(database, owner, body=body)
        _dispatch(op, kwargs["deadline"])
        return FileObjectCandidateResult(Dispatch.SENT, None, None, None, _READY, None)

    monkeypatch.setattr(_file_operations, "exchange_stat_file", exchange)
    outcome = _call(operation, EvidenceCarrier(), "stat", plan=body)
    assert outcome.binding.identity_plan is body
    assert outcome.binding.bootstrap is _BOOTSTRAP
    _row(database, owner, body=body)


def test_package_unconfirmed_publication_retains_next_child_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, operation = _context(tmp_path)
    publications: list[tuple[int, int, bytes]] = []
    sources = [BytesSource(b""), BytesSource(b"")]
    original_publish = LifecycleObligation.publish_payload

    def publish(self: LifecycleObligation, *, expected_revision: int, payload_version: int, payload: bytes):
        publications.append((expected_revision, payload_version, payload))
        original_publish(self, expected_revision=expected_revision, payload_version=payload_version, payload=payload)
        raise RuntimeError("both publication replies lost")

    def run(self: _PreparedUpload) -> FileUploadOutcome:
        assert _row(database, owner).batch_index == 0
        _dispatch(self.state.operation, Deadline.after(30))
        assert self.state.operation.settle(Dispatch.SENT, ExitStatus(code=0))
        return FileUploadOutcome(
            FileUploadStatus.COMPLETE, self.binding, self.state.token, 0, 0, publication_confirmed=True
        )

    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
    monkeypatch.setattr(_PreparedUpload, "run", run)
    with pytest.raises(RuntimeError):
        operation.upload_package(
            EvidenceCarrier(completion=ExitStatus(code=0)),
            trusted_root_path="/data",
            members=tuple(
                PackageUploadMember(f"file-{index}", source, 0, Replace(), new_metadata())
                for index, source in enumerate(sources)
            ),
            checkpoint=lambda *_: None,
            plan=_BODY,
            deadline=Deadline.after(30),
            runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        )
    assert len(publications) == 2 and publications[0] == publications[1]
    (active,) = operation.active_package_uploads
    assert active.binding.bootstrap is _BOOTSTRAP
    assert isinstance(active.prepared, _PreparedUpload)
    assert active.prepared.binding is active.binding
    assert _row(database, owner).batch_index == 1
    assert all(source.calls == 0 for source in sources)
    with pytest.raises(StateError):
        owner.close()


def test_json_lost_child_publication_retains_exact_child_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, operation = _context(tmp_path)
    original_publish = LifecycleObligation.publish_payload

    def stat(op: BorrowedFixedHelperCarrier, **kwargs: Any) -> FileObjectCandidateResult:
        assert kwargs["bootstrap"] is _BOOTSTRAP
        _dispatch(op, kwargs["deadline"])
        return FileObjectCandidateResult(
            Dispatch.SENT,
            ExitStatus(code=0),
            None,
            None,
            _READY,
            FileObjectObservation(FileObjectObservationState.ABSENT),
        )

    def publish(self: LifecycleObligation, *, expected_revision: int, payload_version: int, payload: bytes):
        original_publish(self, expected_revision=expected_revision, payload_version=payload_version, payload=payload)
        raise RuntimeError("child publication committed but reply lost")

    monkeypatch.setattr(_file_json, "stat_file", stat)
    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
    carrier = EvidenceCarrier(completion=ExitStatus(code=0))
    with pytest.raises(RuntimeError) as caught:
        _call(operation, carrier, "json")
    assert caught.value.__cause__ is None
    (active,) = operation.active_json_updates
    assert active.binding.bootstrap is _BOOTSTRAP
    child = active.prepared.state.active_upload
    assert child is not None and child.binding.bootstrap is _BOOTSTRAP
    assert child.binding is child.state.binding
    record = _row(database, owner)
    assert record.attempt == 1 and record.token == child.state.token
    assert carrier.calls == 1
    with pytest.raises(StateError):
        owner.close()
