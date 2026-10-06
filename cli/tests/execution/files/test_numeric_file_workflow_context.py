"""Private workflow context continuity using test-only simulated exchange backends.

Local packed helpers exercise workflow state after the exchange double observes the
supplied context. They do not establish root admission or native credentials.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.execution import _file_download as download
from agentworks.execution import _file_json as json_workflow
from agentworks.execution import _file_operations as single
from agentworks.execution import _file_upload as upload
from agentworks.execution._account import (
    AccountObservationState,
    FileOwnershipObservation,
    FileOwnershipResolutionResult,
)
from agentworks.execution._account_protocol import FileOwnership
from agentworks.execution._file_inventory_exchange import (
    FileInventoryCandidateResult,
    FileInventoryObservation,
    FileInventoryObservationState,
)
from agentworks.execution._file_metadata_exchange import (
    FileMetadataCandidateResult,
    FileMetadataObservation,
    FileMetadataObservationState,
)
from agentworks.execution._file_metadata_protocol import FileMetadataOperation
from agentworks.execution._file_object_exchange import FileObjectObservation, FileObjectObservationState
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_publication import Create, Replace
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._json import validate_json_object
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.execution.carrier import Carrier, Deadline, Dispatch, ExitStatus
from agentworks.operations import OperationOwner
from tests.execution.files._file_download_support import BytesSink
from tests.execution.files._file_download_support import LostCallStdoutCarrier as LostDownloadReply
from tests.execution.files._file_publication_support import install_fixture_bundle as install_publication
from tests.execution.files._file_read_support import install_fixture_bundle as install_read
from tests.execution.files._file_snapshot_support import LocalCarrier
from tests.execution.files._file_snapshot_support import install_fixture_bundle as install_snapshot
from tests.execution.files._file_stage_support import install_fixture_bundle as install_stage
from tests.execution.files._file_upload_support import BytesSource, LostCallStdoutCarrier, new_metadata
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files.test_file_download import (
    _CancelingSink,
    _FailingSink,
    _LostLiveCleanupCarrier,
    _LostLiveCompletionCarrier,
)
from tests.execution.files.test_file_json import AfterCallCarrier
from tests.execution.files.test_file_operations import _READY, _REVISION, SyntheticCarrier, _object_result, _report
from tests.execution.files.test_file_upload import (
    _LEAVE_EXACT_PUBLICATION_DEBT,
    MissingCompletionCallCarrier,
    RaisingCallCarrier,
    RestorePublicationBundleCarrier,
)
from tests.execution.files.test_numeric_guest_helper_adoption import _GUEST, _ROOT

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="private file workflows require Linux")

_CONTEXT = _NumericGuestBootstrap(_ROOT, _GUEST)
_TOKEN = bytes(range(16))
_DATA = b"private-workflow-data"


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple]:
    root = tmp_path / "approved"
    root.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o1777)
    scratch.chmod(0o1777)
    install_snapshot(monkeypatch, scratch)
    install_stage(monkeypatch)
    install_publication(monkeypatch)
    install_read(monkeypatch)
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    plan = IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "numeric-workflow-vm"), "file-workflow"
    )
    try:
        yield root, plan, owner.borrow()
    finally:
        database.close()


def _observe_local_exchanges(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, names: tuple[str, ...], calls: list[str]
) -> None:
    """Assert the workflow boundary, then simulate the backend with local helpers."""
    for name in names:
        original = getattr(module, name)

        def exchange(*args: Any, _name=name, _original=original, **kwargs: Any) -> Any:
            assert kwargs.pop("bootstrap") is _CONTEXT
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, name, exchange)


@pytest.mark.parametrize(
    ("case", "expected", "status"),
    [
        ("buffered", ["snapshot_begin", "snapshot_chunk", "snapshot_cleanup"], "complete"),
        ("live", ["snapshot_begin", "snapshot_stream", "snapshot_cleanup"], "complete"),
        ("absent", ["snapshot_begin"], "absent"),
        ("lost-begin", ["snapshot_begin", "snapshot_reconcile", "snapshot_cleanup"], "failed"),
        ("lost-chunk", ["snapshot_begin", "snapshot_chunk", "snapshot_cleanup"], "failed"),
        ("lost-cleanup", ["snapshot_begin", "snapshot_chunk", "snapshot_cleanup"], "failed"),
        ("unfinished", ["snapshot_begin"], "uncertain"),
        ("live-unfinished", ["snapshot_begin", "snapshot_stream"], "uncertain"),
        ("live-cleanup-unfinished", ["snapshot_begin", "snapshot_stream", "snapshot_cleanup"], "uncertain"),
        ("sink-failure", ["snapshot_begin", "snapshot_chunk", "snapshot_cleanup"], "failed"),
        ("sink-control", ["snapshot_begin", "snapshot_chunk", "snapshot_cleanup"], "control"),
        ("dispatch-control", ["snapshot_begin"], "control"),
    ],
)
def test_download_context_all_exchanges_and_retained_results(environment, monkeypatch, case, expected, status):
    root, plan, borrow = environment
    if case != "absent":
        (root / "source").write_bytes(_DATA)
    carrier: Carrier = LocalCarrier(live_stdio=case == "live")
    if case.startswith("lost-"):
        carrier = LostDownloadReply({"lost-begin": 1, "lost-chunk": 2, "lost-cleanup": 3}[case])
    elif case == "dispatch-control":
        carrier = RaisingCallCarrier(1)
    elif case == "unfinished":
        carrier = MissingCompletionCallCarrier(1)
    elif case == "live-unfinished":
        carrier = _LostLiveCompletionCarrier()
    elif case == "live-cleanup-unfinished":
        carrier = _LostLiveCleanupCarrier()
    sink = _FailingSink() if case == "sink-failure" else _CancelingSink() if case == "sink-control" else BytesSink()
    binding = download.FileDownloadBinding(
        str(root), "source", 100, plan, runtime_selection(sys.executable), bootstrap=_CONTEXT
    )
    prepared = download._prepare_download_from_binding(
        binding, sink, Deadline.after(30), _TOKEN, BorrowedFixedHelperCarrier(carrier, borrow)
    )
    calls: list[str] = []
    _observe_local_exchanges(
        monkeypatch,
        download,
        ("snapshot_begin", "snapshot_chunk", "snapshot_stream", "snapshot_reconcile", "snapshot_cleanup"),
        calls,
    )
    if status == "control":
        with pytest.raises((KeyboardInterrupt, RuntimeError)) as caught:
            prepared.run()
        assert isinstance(caught.value.__cause__, download.FileDownloadControlFact)
        outcome = caught.value.__cause__.outcome
    else:
        outcome = prepared.run()
        assert outcome.status == status
    assert outcome.binding is binding and outcome.binding.bootstrap is _CONTEXT
    assert calls == expected


@pytest.mark.parametrize(
    ("case", "expected", "status"),
    [
        ("create", ["stage_begin", "stage_chunk", "publish", "stage_cleanup"], "complete"),
        ("replace", ["stage_begin", "stage_chunk", "publish", "stage_cleanup"], "complete"),
        ("lost-begin", ["stage_begin", "stage_reconcile", "stage_cleanup"], "failed"),
        ("lost-publish", ["stage_begin", "stage_chunk", "publish", "publication_reconcile"], "uncertain"),
        (
            "publication-debt",
            ["stage_begin", "stage_chunk", "publish", "publication_cleanup", "stage_cleanup"],
            "failed",
        ),
        ("stage-control", ["stage_begin"], "control"),
        ("publication-control", ["stage_begin", "stage_chunk", "publish"], "control"),
    ],
)
def test_upload_context_stage_publication_reconciliation_and_cleanup(environment, monkeypatch, case, expected, status):
    root, plan, borrow = environment
    carrier: Carrier = LocalCarrier()
    condition = Replace() if case == "replace" else Create()
    if case == "replace":
        (root / "target").write_bytes(b"old")
    if case.startswith("lost-"):
        carrier = LostCallStdoutCarrier(2 if case == "lost-begin" else 4)
    elif case == "publication-debt":
        normal = install_publication(monkeypatch)
        install_publication(monkeypatch, _LEAVE_EXACT_PUBLICATION_DEBT)
        carrier = RestorePublicationBundleCarrier(4, normal, monkeypatch)
    elif case.endswith("control"):
        carrier = RaisingCallCarrier(2 if case == "stage-control" else 4)
    binding = upload.FileUploadBinding(
        str(root), "target", len(_DATA), plan, runtime_selection(sys.executable), bootstrap=_CONTEXT
    )
    prepared = upload._prepare_upload_from_binding(
        BorrowedFixedHelperCarrier(carrier, borrow),
        source=BytesSource(_DATA),
        deadline=Deadline.after(30),
        inputs=(binding, condition, new_metadata()),
        token=_TOKEN,
    )
    calls: list[str] = []
    _observe_local_exchanges(
        monkeypatch,
        upload,
        (
            "stage_begin",
            "stage_chunk",
            "stage_reconcile",
            "stage_cleanup",
            "publish",
            "publication_reconcile",
            "publication_cleanup",
        ),
        calls,
    )
    if status == "control":
        with pytest.raises(RuntimeError) as caught:
            prepared.run()
        assert isinstance(caught.value.__cause__, upload.FileUploadControlFact)
        outcome = caught.value.__cause__.outcome
        assert prepared.control_outcome is outcome
    else:
        outcome = prepared.run()
        assert outcome.status == status
    assert outcome.binding is binding and outcome.binding.bootstrap is _CONTEXT
    assert calls == expected


@pytest.mark.parametrize(
    "case",
    ["existing", "create", "replace", "retry", "failed", "uncertain", "child-control", "stat-control", "read-control"],
)
def test_json_context_stat_read_children_and_retained_child_results(environment, monkeypatch, case):
    root, plan, borrow = environment
    target = root / "target"
    if case != "create":
        target.write_bytes(b'{"existing":true}')

    def race_once(call: int, deadline: Deadline) -> None:
        if call == 1:
            target.write_bytes(b'{"concurrent":true}')

    carrier: Carrier = AfterCallCarrier(race_once) if case == "retry" else LocalCarrier()
    if case == "child-control":
        carrier = RaisingCallCarrier(2)
    elif case in {"stat-control", "read-control"}:
        carrier = RaisingCallCarrier(1)
    elif case == "uncertain":
        carrier = LostCallStdoutCarrier(4)
    elif case == "failed":
        install_publication(monkeypatch, _LEAVE_EXACT_PUBLICATION_DEBT)
    strategy = "replace" if case in {"replace", "stat-control"} else "merge-overwrite"
    binding = json_workflow.FileJsonBinding(
        str(root), "target", strategy, True, 1000, 10, plan, runtime_selection(sys.executable), bootstrap=_CONTEXT
    )
    inputs = json_workflow._Inputs(
        binding, validate_json_object(b'{"source":true}', max_bytes=1000, max_depth=10), new_metadata()
    )
    state = json_workflow._State(binding, BorrowedFixedHelperCarrier(carrier, borrow))
    prepared = json_workflow._PreparedJsonUpdate(
        binding, state, json_workflow._JsonWorkflow(inputs, Deadline.after(30), state)
    )
    calls: list[str] = []
    _observe_local_exchanges(monkeypatch, json_workflow, ("stat_file", "read_file"), calls)
    _observe_local_exchanges(
        monkeypatch,
        upload,
        (
            "stage_begin",
            "stage_chunk",
            "stage_reconcile",
            "stage_cleanup",
            "publish",
            "publication_reconcile",
            "publication_cleanup",
        ),
        calls,
    )
    children: list[upload._PreparedUpload] = []

    def retain_child(child: upload._PreparedUpload, attempt: int) -> None:
        assert child.binding.bootstrap is binding.bootstrap
        assert child.binding.identity_plan is binding.identity_plan
        assert child.binding.runtime_selection is binding.runtime_selection
        assert attempt == len(children) + 1
        children.append(child)

    prepared.set_child_upload_callback(retain_child)
    if case in {"child-control", "stat-control", "read-control"}:
        with pytest.raises(RuntimeError) as caught:
            prepared.run()
        assert isinstance(caught.value.__cause__, json_workflow.FileJsonControlFact)
        outcome = caught.value.__cause__.outcome
        if case == "child-control":
            assert children[-1].control_outcome is outcome.upload_outcome
    else:
        outcome = prepared.run()
        assert outcome.status == ("failed" if case == "failed" else "uncertain" if case == "uncertain" else "complete")
    assert outcome.binding is binding and outcome.binding.bootstrap is _CONTEXT
    if case in {"stat-control", "read-control"}:
        assert calls == ["stat_file" if case == "stat-control" else "read_file"]
        assert not children and outcome.upload_outcome is None
        assert outcome.pending_remote_effects and outcome.requires_owner_retention
        return
    assert len(children) == (2 if case == "retry" else 1)
    assert outcome.upload_outcome is not None
    assert outcome.upload_outcome.binding is children[-1].binding
    assert outcome.upload_outcome.binding.bootstrap is _CONTEXT
    first = "stat_file" if case == "replace" else "read_file"
    cycle = [first, "stage_begin", "stage_chunk", "publish", "stage_cleanup"]
    if case == "create":
        assert calls == cycle
    elif case == "retry":
        assert calls == cycle * 2
    elif case == "child-control":
        assert calls == [first, "stage_begin"]
    elif case == "uncertain":
        assert calls == [first, "stage_begin", "stage_chunk", "publish", "publication_reconcile"]
    elif case == "failed":
        assert calls == [first, "stage_begin", "stage_chunk", "publish", "publication_cleanup"]
    else:
        assert calls == cycle


@pytest.mark.parametrize("kind", ["stat", "inventory", "remove", "metadata", "directory"])
@pytest.mark.parametrize("result_kind", ["terminal", "failed", "uncertain", "control"])
def test_single_call_context_and_retained_control_facts(environment, monkeypatch, kind, result_kind):
    root, plan, borrow = environment
    runtime = runtime_selection(sys.executable)
    completion = None if result_kind == "uncertain" else ExitStatus(code=1 if result_kind == "failed" else 0)
    carrier = SyntheticCarrier(Dispatch.SENT, completion)
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    deadline = Deadline.after(30)
    calls: list[str] = []
    binding: single.OwnedFileBinding
    prepared_type: type[
        single._PreparedStat | single._PreparedInventory | single._PreparedRemove | single._PreparedMetadata
    ]
    if kind == "stat":
        binding = single.FileStatBinding(str(root), "target", plan, runtime, _CONTEXT)
        prepared_type = single._PreparedStat
        exchange_name = "exchange_stat_file"
    elif kind == "inventory":
        binding = single.FileInventoryBinding(str(root), "target", 10, 2, 1000, plan, runtime, _CONTEXT)
        prepared_type = single._PreparedInventory
        exchange_name = "exchange_list_directory"
    elif kind == "remove":
        binding = single.FileRemoveBinding(str(root), "target", FileKind.REGULAR, _REVISION, plan, runtime, _CONTEXT)
        prepared_type = single._PreparedRemove
        exchange_name = "exchange_remove_file"
    else:
        metadata_operation = (
            FileMetadataOperation.SET_METADATA if kind == "metadata" else FileMetadataOperation.ENSURE_DIRECTORY
        )
        binding = single.FileMetadataBinding(
            metadata_operation, str(root), "target", "owner", "group", 0o700, plan, runtime, _CONTEXT
        )
        prepared_type = single._PreparedMetadata
        exchange_name = "set_file_metadata" if kind == "metadata" else "ensure_file_directory"

        def resolve(borrowed, owner, group, selected_deadline, selected_runtime):
            assert borrowed is operation and selected_deadline is deadline and selected_runtime is runtime
            calls.append("ownership")
            # Ownership resolution is a separate read-only call; it has no bootstrap argument.
            carrier.completion = ExitStatus(code=0)
            report = _report(borrowed, selected_deadline)
            carrier.completion = completion
            return FileOwnershipResolutionResult(
                report.dispatch,
                report.completion,
                report.local_status,
                report.failure,
                _READY,
                FileOwnershipObservation(AccountObservationState.RESOLVED, ownership=FileOwnership(123, 456)),
            )

        monkeypatch.setattr(single, "resolve_file_ownership", resolve)

    def exchange(borrowed, **kwargs):
        assert kwargs["bootstrap"] is _CONTEXT
        assert kwargs["plan"] is plan and kwargs["runtime_selection"] is runtime
        assert borrowed is operation and kwargs["deadline"] is deadline
        calls.append(kind)
        if result_kind == "control":
            carrier.control = KeyboardInterrupt()
        if kind in {"stat", "remove"}:
            return _object_result(borrowed, deadline, FileObjectObservation(FileObjectObservationState.UNCHANGED))
        report = _report(borrowed, deadline)
        if kind == "inventory":
            return FileInventoryCandidateResult(
                report.dispatch,
                report.completion,
                report.local_status,
                report.failure,
                _READY,
                FileInventoryObservation(FileInventoryObservationState.NOT_FOUND),
            )
        assert kwargs["uid"] == 123 and kwargs["gid"] == 456
        return FileMetadataCandidateResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            _READY,
            FileMetadataObservation(FileMetadataObservationState.UNCHANGED),
        )

    monkeypatch.setattr(single, exchange_name, exchange)
    prepared = prepared_type(binding, single._State(binding, operation, deadline))
    if result_kind == "control":
        with pytest.raises(KeyboardInterrupt) as caught:
            prepared.run()
        assert isinstance(caught.value.__cause__, single.OwnedFileControlFact)
        outcome = caught.value.__cause__.outcome
        assert outcome.pending_remote_effects and outcome.requires_owner_retention
    else:
        outcome = prepared.run()
        assert outcome.result is not None
        assert outcome.result.carrier_completion == completion
        assert outcome.requires_owner_retention == (result_kind != "terminal")
    assert outcome.binding is binding and outcome.binding.bootstrap is _CONTEXT
    assert calls == (["ownership", kind] if kind in {"metadata", "directory"} else [kind])
