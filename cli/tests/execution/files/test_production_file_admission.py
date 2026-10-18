"""RunContext file admission with simulated guest entry and intact packed helpers."""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, LifecycleObligationState
from agentworks.errors import ExternalError, StateError
from agentworks.execution import _file_local_download, _file_operation
from agentworks.execution._file_download import _prepare_download_from_binding
from agentworks.execution._file_effect_gate_exchange import exchange_file_effect_gate
from agentworks.execution._file_obligation import FileCallFamily, FileCallObligation, decode_file_call_obligation
from agentworks.execution._file_operation import FileGateSetupControlFact, _PendingDownloadSetup, _PendingUploadSetup
from agentworks.execution._local_file_io import LocalFileSource
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteObservation, RuntimePrerequisiteState
from agentworks.execution.carrier import CarrierReport, Deadline, Dispatch, ExitStatus, Failure
from agentworks.execution.files import (
    Create,
    DirectoryLimit,
    DirectoryTransferLimit,
    ExistingFile,
    FileFailureReason,
    FileOperationPhase,
    JsonStrategy,
    NewMetadata,
)
from agentworks.execution.target import ExecutionRoute
from agentworks.operations import LifecycleObligation, OperationBorrow, OperationOwner, _PreRegistrationRefusal
from agentworks.vms._native_operation import VMOperation, _vm_operation
from tests.vms.test_native_operation import _install_route, _RouteCarrier, _scope
from tests.vms.test_native_operation import database as database

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import CarrierIO, PreparedInvocation

pwd = pytest.importorskip("pwd")
grp = pytest.importorskip("grp")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux helper proof")


@dataclass
class _Harness:
    selected: VMOperation
    route: _RouteCarrier
    root: Path
    retained: bool = False


@pytest.fixture
def prepared(database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    platform = WSL2Platform("wsl2", {})
    route, _, _ = _install_route(database, platform, monkeypatch)
    root = tmp_path / "files"
    root.mkdir()
    (root / "source").write_bytes(b"ordinary")
    (root / "tree").mkdir()
    (root / "tree" / "child").write_bytes(b"child")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o1777)
    scratch.chmod(0o1777)
    route.local.scratch = scratch
    context = _vm_operation(
        database,
        "box",
        platform,
        RunContext(),
        route=ExecutionRoute.NATIVE,
        deadline=Deadline.after(60),
        trusted_root=PurePosixPath(root),
    )
    case = _Harness(context.__enter__(), route, root)
    yield case
    if case.retained:
        with pytest.raises(StateError):
            context.__exit__(None, None, None)
        assert database.operations.inspect(_scope()) is not None
    else:
        context.__exit__(None, None, None)
        assert database.operations.inspect(_scope()) is None


def _metadata(mode: int = 0o600) -> NewMetadata:
    return NewMetadata(pwd.getpwuid(os.geteuid()).pw_name, grp.getgrgid(os.getegid()).gr_name, mode)


def _invoke(case: _Harness, action: str, *, sudo: bool = False) -> None:
    files, root = case.selected.files, case.root
    target, metadata = PurePosixPath(root / "source"), _metadata()
    if action == "read":
        result = files.read_file(target, max_bytes=64, sudo=sudo)
        assert result is not None and result.data == b"ordinary"
    elif action == "upload":
        files.write_file(
            PurePosixPath(root / "published"), b"value", condition=Create(), create_metadata=metadata, sudo=sudo
        )
    elif action == "upload_file":
        files.upload_file(
            PurePosixPath(root / "published"), root / "source", condition=Create(), create_metadata=metadata, sudo=sudo
        )
    elif action == "download":
        files.download(target, root / "downloaded", max_bytes=64, sudo=sudo)
        assert (root / "downloaded").read_bytes() == b"ordinary"
    elif action == "json":
        files.update_json(
            PurePosixPath(root / "settings.json"),
            {"enabled": True},
            strategy=JsonStrategy.REPLACE,
            create=True,
            create_metadata=metadata,
            sudo=sudo,
        )
    elif action in {"create_directory", "ensure_directory"}:
        method = files.create_directory if action == "create_directory" else files.ensure_directory
        method(PurePosixPath(root / "new-directory"), metadata=_metadata(0o700), sudo=sudo)
    elif action == "metadata":
        files.set_metadata(target, owner=metadata.owner, group=metadata.group, mode=0o600, sudo=sudo)
    elif action == "remove":
        current = files.stat(target, sudo=sudo)
        assert current is not None
        files.remove(target, expected_kind=current.kind, expected=current.revision, sudo=sudo)
    elif action == "upload_directory":
        files.upload_directory(
            PurePosixPath(root / "remote-tree"),
            root / "tree",
            limit=DirectoryTransferLimit(max_bytes=64),
            existing_files=ExistingFile.ERROR,
            file_metadata=metadata,
            directory_metadata=_metadata(0o700),
            sudo=sudo,
        )
    else:
        assert action == "download_directory"
        files.download_directory(
            PurePosixPath(root / "tree"),
            root / "local-tree",
            limit=DirectoryTransferLimit(max_bytes=64),
            existing_files=ExistingFile.ERROR,
            sudo=sudo,
        )


_ACTIONS = [
    "read",
    "upload",
    "upload_file",
    "download",
    "json",
    "create_directory",
    "ensure_directory",
    "metadata",
    "remove",
    "upload_directory",
    "download_directory",
]


@pytest.mark.parametrize("action", _ACTIONS)
def test_every_effect_child_has_pending_then_bound_same_row(prepared: _Harness, action: str) -> None:
    _invoke(prepared, action)
    calls: dict[str, list[FileCallObligation]] = defaultdict(list)
    for obligation_id, payload in zip(prepared.route.file_ids, prepared.route.file_records, strict=True):
        calls[obligation_id].append(decode_file_call_obligation(payload))
    effects = 0
    for records in calls.values():
        if records[0].family in {FileCallFamily.STAT, FileCallFamily.INVENTORY}:
            assert all(record.effect_gate is None and record.gate_setup is None for record in records)
            continue
        effects += 1
        assert records[0].gate_setup is not None and records[0].effect_gate is None and len(records) >= 2
        assert all(record.gate_setup is None and record.effect_gate is not None for record in records[1:])
        assert all(record.identity_plan == records[0].identity_plan for record in records)
        if records[0].family in {FileCallFamily.DOWNLOAD, FileCallFamily.UPLOAD}:
            assert len({record.token for record in records}) == 1
    assert effects >= (2 if action == "upload_directory" else 1)


def test_observations_never_setup(prepared: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("observation tried gate setup")

    monkeypatch.setattr(_file_operation, "exchange_file_effect_gate", unexpected)
    files = prepared.selected.files
    assert files.stat(PurePosixPath(prepared.root / "source")) is not None
    assert files.list_directory(PurePosixPath(prepared.root / "tree"), limit=DirectoryLimit(max_entries=4))


@pytest.mark.parametrize("action", ["upload_file", "download"])
def test_local_transfer_uses_one_original_borrow(
    prepared: _Harness, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    original = OperationOwner.borrow
    borrows: list[OperationBorrow] = []

    def borrow(owner: OperationOwner) -> OperationBorrow:
        result = original(owner)
        assert owner is prepared.selected.owner
        borrows.append(result)
        return result

    monkeypatch.setattr(OperationOwner, "borrow", borrow)
    _invoke(prepared, action)
    assert len(borrows) == 1 and borrows[0].closed


def test_caller_source_is_read_only_after_bound_promotion(prepared: _Harness) -> None:
    class Source:
        calls = 0

        def read(self, limit: int, /) -> bytes:
            record = decode_file_call_obligation(prepared.route.file_records[-1])
            assert record.effect_gate is not None and record.gate_setup is None
            self.calls += 1
            return b"v" if self.calls == 1 else b""

        def close(self) -> None:
            pytest.fail("borrowed source was closed")

    source = Source()
    prepared.selected.files.upload(
        PurePosixPath(prepared.root / "published"), source, size=1, condition=Create(), create_metadata=_metadata()
    )
    assert source.calls == 2 and (prepared.root / "published").read_bytes() == b"v"


@pytest.mark.parametrize("action", _ACTIONS[:9])
@pytest.mark.parametrize("fault", ["not_sent", "lost", "invalid", "nonzero", "interrupt", "local_unsettled"])
def test_setup_failures_preserve_exact_custody(
    prepared: _Harness, monkeypatch: pytest.MonkeyPatch, action: str, fault: str
) -> None:
    original = prepared.route.local.execute
    control = KeyboardInterrupt("setup interrupted")
    setup_calls = 0
    originals: list[tuple[LocalFileSource, bool]] = []
    close = LocalFileSource.close

    def close_original(resource: LocalFileSource, deadline: Deadline) -> None:
        call = prepared.selected.file_operation._local_file_call
        assert call is not None and call.resource is resource
        originals.append((resource, call.borrow.closed))
        close(resource, deadline)

    monkeypatch.setattr(LocalFileSource, "close", close_original)

    def execute(
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        nonlocal setup_calls
        record = decode_file_call_obligation(prepared.route.file_records[-1])
        if record.gate_setup is None:
            return original(invocation, io=io, deadline=deadline, custody=custody)
        setup_calls += 1
        if action == "upload_file":
            call = prepared.selected.file_operation._local_file_call
            assert call is not None and not call.borrow.closed
            assert isinstance(call.resource, LocalFileSource) and call.resource._read_size == 0
        if fault == "not_sent":
            return CarrierReport(Dispatch.NOT_SENT, failure=Failure.DISPATCH)
        if fault == "local_unsettled":
            assert custody is not None
            custody.begin_process()
            return CarrierReport(Dispatch.NOT_SENT, failure=Failure.DISPATCH)
        report = original(invocation, io=io, deadline=deadline, custody=custody)
        if fault == "lost":
            raise OSError("private setup details")
        if fault == "interrupt":
            raise control
        if fault == "nonzero":
            return replace(report, completion=ExitStatus(code=1))
        return replace(report, stdout=replace(report.stdout, complete=False))

    monkeypatch.setattr(prepared.route.local, "execute", execute)
    with pytest.raises(KeyboardInterrupt if fault == "interrupt" else ExternalError) as raised:
        _invoke(prepared, action)
    assert setup_calls == 1
    if fault == "interrupt":
        assert raised.value is control
        fact = control.__cause__
        if action in {"download", "upload_file"}:
            fact = fact.__cause__ if fact is not None else None
        assert isinstance(fact, FileGateSetupControlFact)
    else:
        assert isinstance(raised.value, ExternalError) and raised.value.details is not None
        assert raised.value.details.phase is FileOperationPhase.OBSERVATION
        assert raised.value.details.reason is FileFailureReason.COORDINATION
        assert raised.value.details.effect is None
    assert not (prepared.root / "downloaded").exists() and not (prepared.root / "published").exists()
    if action == "upload_file":
        assert len(originals) == 1 and originals[0][1] is False and originals[0][0].settled
    rows = [
        row
        for row in prepared.selected.owner.list_pending_lifecycle_obligations()
        if row.obligation_kind == "file-call"
    ]
    op = prepared.selected.file_operation
    if fault == "not_sent":
        assert not rows and not op.active_uploads and not op.active_downloads
        return
    prepared.retained = True
    assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT
    record = decode_file_call_obligation(rows[0].payload)
    assert record.gate_setup is not None and record.effect_gate is None
    if action in {"read", "download", "upload", "upload_file"}:
        active = (op.active_downloads if action in {"read", "download"} else op.active_uploads)[0]
        assert isinstance(active.prepared, _PendingDownloadSetup | _PendingUploadSetup)
        assert active.prepared.token == record.token
        assert active.prepared.operation._borrow is active.borrow and not active.borrow.closed


@pytest.mark.parametrize("sudo", [False, True])
def test_selected_effective_uid_drives_setup_before_any_local_stage(
    prepared: _Harness, monkeypatch: pytest.MonkeyPatch, sudo: bool
) -> None:
    selected = prepared.selected
    plan = selected.files._elevated_plan if sudo else selected.files._ordinary_plan
    assert plan is not None
    observed: list[FileCallObligation] = []

    def execute(
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        record = decode_file_call_obligation(prepared.route.file_records[-1])
        assert record.identity_plan is not None and record.identity_plan.expected == plan.expected
        assert record.gate_setup is not None and f"/{plan.expected.euid}/" in record.gate_setup.path
        observed.append(record)
        return CarrierReport(Dispatch.NOT_SENT, failure=Failure.DISPATCH)

    monkeypatch.setattr(prepared.route.local, "execute", execute)
    with pytest.raises(ExternalError):
        _invoke(prepared, "download", sudo=sudo)
    assert len(observed) == 1 and not (prepared.root / "downloaded").exists()
    assert not selected.file_operation.has_unfinished_local_download_call


@pytest.mark.parametrize(
    "state,reason",
    [
        (RuntimePrerequisiteState.MISSING, FileFailureReason.RUNTIME_MISSING),
        (RuntimePrerequisiteState.SHIM, FileFailureReason.RUNTIME_SHIM),
        (RuntimePrerequisiteState.UNSUPPORTED_VERSION, FileFailureReason.RUNTIME_UNSUPPORTED_VERSION),
    ],
)
def test_setup_preserves_known_runtime_diagnostics(
    prepared: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    state: RuntimePrerequisiteState,
    reason: FileFailureReason,
) -> None:
    original = exchange_file_effect_gate

    def exchange(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        return replace(result, runtime_prerequisite=RuntimePrerequisiteObservation(state, None))

    monkeypatch.setattr(_file_operation, "exchange_file_effect_gate", exchange)
    with pytest.raises(ExternalError) as raised:
        _invoke(prepared, "read")
    assert raised.value.details is not None and raised.value.details.reason is reason
    prepared.retained = True


def test_known_registration_refusal_leaves_local_original_finalization_to_its_owner(
    prepared: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[LocalFileSource] = []

    def refuse(borrow: OperationBorrow, *args: Any, **kwargs: Any) -> Any:
        call = prepared.selected.file_operation._local_file_call
        assert call is not None and call.borrow is borrow and not borrow.closed
        assert isinstance(call.resource, LocalFileSource) and not call.resource.settled
        observed.append(call.resource)
        raise _PreRegistrationRefusal("registration refused before dispatch")

    monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", refuse)
    with pytest.raises(ExternalError):
        _invoke(prepared, "upload_file")
    assert len(observed) == 1 and observed[0].settled
    assert not prepared.selected.file_operation.has_unfinished_local_file_call
    assert not prepared.selected.file_operation.active_uploads


@pytest.mark.parametrize("action", ["read", "download", "upload_file", "json", "metadata", "remove"])
@pytest.mark.parametrize("committed", [False, True])
def test_binding_publication_loss_retains_row_before_data_effects(
    prepared: _Harness, monkeypatch: pytest.MonkeyPatch, action: str, committed: bool
) -> None:
    original = LifecycleObligation.publish_payload

    def publish(obligation: LifecycleObligation, **kwargs: Any) -> Any:
        if committed:
            original(obligation, **kwargs)
        raise RuntimeError("private promotion loss")

    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
    with pytest.raises(ExternalError) as raised:
        _invoke(prepared, action)
    assert raised.value.details is not None and raised.value.details.reason is FileFailureReason.COORDINATION
    records = [decode_file_call_obligation(payload) for payload in prepared.route.file_records]
    assert records[-1].gate_setup is not None
    (row,) = [
        row
        for row in prepared.selected.owner.list_pending_lifecycle_obligations()
        if row.obligation_kind == "file-call"
    ]
    bound = decode_file_call_obligation(row.payload)
    assert (bound.effect_gate is not None) is committed and bound.token == records[-1].token
    assert not (prepared.root / "downloaded").exists()
    prepared.retained = True


@pytest.mark.parametrize("fault", ["prepared", "writer", "registration", "registration_lost"])
def test_local_download_construction_preserves_setup_custody(
    prepared: _Harness, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    op = prepared.selected.file_operation
    original_prepare = _prepare_download_from_binding
    original_install = OperationBorrow.install_dispatch_obligation
    writer_calls = 0

    def prepare(*args: Any, **kwargs: Any) -> Any:
        assert args[0].effect_gate is not None
        if fault == "prepared":
            raise MemoryError("prepared allocation")
        return original_prepare(*args, **kwargs)

    def writer(*args: Any, **kwargs: Any) -> Any:
        nonlocal writer_calls
        writer_calls += 1
        active = op.active_downloads[0]
        assert active.binding.effect_gate is not None
        assert op._local_download_call is not None and op._local_download_call.borrow is active.borrow
        raise OSError("local stage constructor")

    def install(borrow: OperationBorrow, *args: Any, **kwargs: Any) -> Any:
        if fault == "registration_lost":
            original_install(borrow, *args, **kwargs)
        raise MemoryError("registration allocation")

    monkeypatch.setattr(_file_operation, "_prepare_download_from_binding", prepare)
    monkeypatch.setattr(_file_local_download, "_publisher_for_host", writer)
    if fault.startswith("registration"):
        monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", install)
    with pytest.raises(ExternalError):
        _invoke(prepared, "download")
    assert writer_calls == (1 if fault == "writer" else 0)
    (active,) = op.active_downloads
    assert not active.borrow.closed and active.outcome is None
    assert op._local_download_call is not None and op._local_download_call.active is active
    assert op._local_download_call.local_outcome is not None and not op._local_download_call.dispatched
    assert not (prepared.root / "downloaded").exists()
    prepared.retained = True


@pytest.mark.parametrize("fault", ["outcome", "fact", "capture", "gate_fact"])
@pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit])
def test_setup_control_identity_survives_local_reporting_failure(
    prepared: _Harness, monkeypatch: pytest.MonkeyPatch, fault: str, stop: type[BaseException]
) -> None:
    control = stop("original setup control")

    def execute(*args: Any, **kwargs: Any) -> Any:
        raise control

    def allocate(*args: Any, **kwargs: Any) -> Any:
        raise MemoryError("reporting allocation")

    monkeypatch.setattr(prepared.route.local, "execute", execute)
    if fault == "outcome":
        monkeypatch.setattr(_file_local_download.FileLocalDownloadOutcome, "__init__", allocate)
    elif fault == "fact":
        monkeypatch.setattr(_file_local_download.FileLocalDownloadControlFact, "__init__", allocate)
    elif fault == "gate_fact":
        monkeypatch.setattr(FileGateSetupControlFact, "__init__", allocate)
    else:
        monkeypatch.setattr(prepared.selected.file_operation, "capture_local_download", allocate)
    with pytest.raises(stop) as raised:
        _invoke(prepared, "download")
    assert raised.value is control
    assert len(prepared.selected.file_operation.active_downloads) == 1
    assert not (prepared.root / "downloaded").exists()
    prepared.retained = True
