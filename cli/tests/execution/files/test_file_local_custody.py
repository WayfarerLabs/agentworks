"""Owned local originals remain under the actual ordinary upload borrow."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import PurePosixPath
from typing import cast

import pytest

from agentworks.errors import ConflictError, PartialMutationError, StateError
from agentworks.execution import _directory_transfer, _file_operation
from agentworks.execution import access as access_module
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._file_local_custody import LocalFileControlFact
from agentworks.execution._local_file_io import LocalFileSource, LocalTreeInventory
from agentworks.execution.access import ExecutionAccess, FileAccess
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import (
    Create,
    DirectoryTransferControlFact,
    DirectoryTransferLimit,
    ExistingFile,
    directory_transfer_progress,
)
from agentworks.execution.target import ExecutionTarget
from agentworks.operations import OperationOwner
from agentworks.vms._native_operation import NativeVMOperation, _Workflow
from tests.execution.files._file_access_support import bound_access as bound_access
from tests.execution.files._file_access_support import plan as plan
from tests.execution.files._file_read_support import LocalCarrier
from tests.execution.files.test_directory_transfer import metadata

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real fixed helper proof requires Linux")


def workflow(access: FileAccess, owner: OperationOwner) -> _Workflow:
    execution_operation = ExecutionOperation(owner, access._operation._target)
    execution = ExecutionAccess(
        execution_operation,
        access._carrier,
        runtime_selection=access._runtime_selection,
        ordinary_plan=access._ordinary_plan,
        elevated_plan=access._elevated_plan,
        entity_kind="file",
        entity_name="configuration",
        deadline=access._deadline,
    )
    views = NativeVMOperation(
        owner,
        access,
        execution,
        access._operation,
        execution_operation,
        ExecutionTarget(execution, access, access._carrier.features),
    )
    return _Workflow(owner, Deadline.after(30), views=views)


def test_owned_source_open_and_close_share_the_ordinary_serial_borrow(bound_access, tmp_path, monkeypatch):
    access, root, owner, _ = bound_access
    source = tmp_path / "source"
    source.write_bytes(b"owned")
    deadlines = []
    selected = Deadline.after(30)
    access._deadline = lambda: selected

    class ObservedSource(LocalFileSource):
        def open(self, deadline):
            assert access._operation.has_unfinished_local_file_call
            with pytest.raises(StateError):
                owner.borrow()
            deadlines.append(deadline)
            return super().open(deadline)

        def close(self, deadline):
            with pytest.raises(StateError):
                owner.borrow()
            deadlines.append(deadline)
            super().close(deadline)

    monkeypatch.setattr(access_module, "LocalFileSource", ObservedSource)
    access.upload_file(PurePosixPath(root / "file"), source, condition=Create(), create_metadata=metadata())
    assert deadlines == [selected, selected]
    assert not access._operation.has_unfinished_local_file_call
    assert not owner.list_pending_lifecycle_obligations()


def test_close_failure_keeps_original_and_cleanup_never_replays_upload(bound_access, tmp_path, monkeypatch):
    access, root, owner, _ = bound_access
    source = tmp_path / "source"
    source.write_bytes(b"owned")
    reads = []
    originals = []

    class InterruptedClose(LocalFileSource):
        attempts = 0

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            originals.append(self)

        def read(self, maximum):
            reads.append(maximum)
            return super().read(maximum)

        def close(self, deadline):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("private original still open")
            super().close(deadline)

    monkeypatch.setattr(access_module, "LocalFileSource", InterruptedClose)
    with pytest.raises(PartialMutationError):
        access.upload_file(PurePosixPath(root / "file"), source, condition=Create(), create_metadata=metadata())
    assert (root / "file").read_bytes() == b"owned"
    assert access._operation.has_unfinished_local_file_call
    assert not originals[0].settled
    before = list(reads)
    access._operation.settle_local_file_bookkeeping(Deadline.after(3))
    assert reads == before
    assert originals[0].settled
    assert not access._operation.has_unfinished_local_file_call
    assert not owner.list_pending_lifecycle_obligations()


def test_inventory_constructor_is_admitted_before_construction(bound_access, tmp_path, monkeypatch):
    access, root, owner, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    seen = []

    class FailingInventory(LocalTreeInventory):
        def __init__(self, *args, **kwargs):
            with pytest.raises(StateError):
                owner.borrow()
            seen.append(True)
            raise FileExistsError("private source name")

    monkeypatch.setattr(_directory_transfer, "LocalTreeInventory", FailingInventory)
    with pytest.raises(ConflictError) as stopped:
        access.upload_directory(
            PurePosixPath(root),
            source,
            limit=DirectoryTransferLimit(0),
            existing_files=ExistingFile.ERROR,
            file_metadata=metadata(),
            directory_metadata=metadata(),
        )
    assert seen == [True]
    progress = directory_transfer_progress(stopped.value)
    assert progress is not None and progress.entries == ()
    assert not access._operation.has_unfinished_local_file_call


def test_progress_report_allocation_failure_preserves_original_child(bound_access, tmp_path, monkeypatch):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").touch()
    (root / "a").touch()
    original = ConflictError("child failed")

    def fail(*args, **kwargs):
        raise original

    def report(*args, **kwargs):
        raise MemoryError()

    monkeypatch.setattr(access_module.FileAccess, "stat", fail)
    monkeypatch.setattr(_directory_transfer, "DirectoryTransferControlFact", report)
    with pytest.raises(ConflictError) as stopped:
        access.upload_directory(
            PurePosixPath(root),
            source,
            limit=DirectoryTransferLimit(0),
            existing_files=ExistingFile.ERROR,
            file_metadata=metadata(),
            directory_metadata=metadata(),
        )
    assert stopped.value is original
    assert stopped.value.__cause__ is None


def test_interrupted_public_reduction_keeps_core_capture_and_blocks_finalization(bound_access, tmp_path, monkeypatch):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.write_bytes(b"owned")
    original = KeyboardInterrupt()

    def interrupt(*args, **kwargs):
        raise original

    monkeypatch.setattr(access_module, "reduce_file_upload", interrupt)
    with pytest.raises(KeyboardInterrupt) as stopped:
        access.upload_file(PurePosixPath(root / "file"), source, condition=Create(), create_metadata=metadata())
    assert stopped.value is original
    call = access._operation._local_file_call
    assert call is not None and call.changed
    assert call.upload_outcome is not None and call.upload_outcome.publication_confirmed
    assert call.resource.settled
    assert not call.reduction_complete
    with pytest.raises(StateError):
        access._operation.settle_local_file_bookkeeping(Deadline.after(3))


def test_uncertain_original_close_never_reuses_descriptor_or_replays_source(bound_access, tmp_path, monkeypatch):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.write_bytes(b"owned")
    original_close = os.close
    attempts: list[int] = []
    source_descriptor = []

    class CapturedSource(LocalFileSource):
        def open(self, deadline):
            size = super().open(deadline)
            assert self._file is not None
            source_descriptor.append(self._file.descriptor)
            return size

    def interrupted_close(descriptor):
        if source_descriptor and descriptor == source_descriptor[0] and not attempts:
            attempts.append(descriptor)
            original_close(descriptor)
            raise OSError("close reply lost after actual close")
        if source_descriptor and descriptor == source_descriptor[0]:
            pytest.fail("uncertain descriptor was reused")
        return original_close(descriptor)

    monkeypatch.setattr(access_module, "LocalFileSource", CapturedSource)
    monkeypatch.setattr(os, "close", interrupted_close)
    with pytest.raises(PartialMutationError):
        access.upload_file(PurePosixPath(root / "file"), source, condition=Create(), create_metadata=metadata())
    assert attempts == source_descriptor
    call = access._operation._local_file_call
    assert call is not None and not call.resource.settled
    for _ in range(2):
        with pytest.raises(OSError):
            access._operation.settle_local_file_bookkeeping(Deadline.after(3))
    assert attempts == source_descriptor
    assert (root / "file").read_bytes() == b"owned"


def test_local_control_and_directory_progress_preserve_original_cause(bound_access, tmp_path, monkeypatch):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"owned")
    original_cause = RuntimeError("earlier private cause")
    original = KeyboardInterrupt()
    original.__cause__ = original_cause

    def interrupt(*args, **kwargs):
        raise original

    monkeypatch.setattr(access_module, "reduce_file_upload", interrupt)
    with pytest.raises(KeyboardInterrupt) as stopped:
        access.upload_directory(
            PurePosixPath(root),
            source,
            limit=DirectoryTransferLimit(5),
            existing_files=ExistingFile.ERROR,
            file_metadata=metadata(),
            directory_metadata=metadata(),
        )
    assert stopped.value is original
    progress_fact = cast("BaseException", original).__cause__
    assert isinstance(progress_fact, DirectoryTransferControlFact)
    assert [str(entry.relative_path) for entry in progress_fact.progress.entries] == ["."]
    local_fact = progress_fact.__cause__
    assert isinstance(local_fact, LocalFileControlFact)
    assert local_fact.changed
    assert local_fact.__cause__ is original_cause


def test_aggregate_retries_known_close_even_when_remote_capture_remains_incomplete(bound_access, tmp_path, monkeypatch):
    access, root, owner, database = bound_access
    source = tmp_path / "source"
    source.write_bytes(b"owned")
    originals = []
    original = KeyboardInterrupt()

    class RetainedSource(LocalFileSource):
        attempts = 0

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            originals.append(self)

        def close(self, deadline):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("local original still open")
            super().close(deadline)

    class MissingCompletion(LocalCarrier):
        def execute(self, *args, **kwargs):
            return replace(super().execute(*args, **kwargs), completion=None)

    def interrupt_capture(*args, **kwargs):
        raise original

    carrier = MissingCompletion()
    access._carrier = carrier
    monkeypatch.setattr(access_module, "LocalFileSource", RetainedSource)
    monkeypatch.setattr(_file_operation, "encode_file_call_obligation", interrupt_capture)
    with pytest.raises(KeyboardInterrupt) as stopped:
        access.upload_file(PurePosixPath(root / "file"), source, condition=Create(), create_metadata=metadata())
    assert stopped.value is original
    assert not originals[0].settled
    active = access._operation.active_uploads[0]
    assert active.outcome is not None and not active.capture_ready
    outcome = active.outcome
    calls = carrier.calls
    aggregate = workflow(access, owner)
    assert not aggregate._components_settled()
    with pytest.raises(StateError):
        aggregate.close(cleanup_deadline=Deadline.after(3))
    assert originals[0].settled
    assert active.outcome is outcome and not active.capture_ready
    assert carrier.calls == calls
    assert database.operations.inspect(owner.ownership.scope) is not None


def test_aggregate_releases_only_after_known_local_cleanup_succeeds(bound_access, tmp_path, monkeypatch):
    access, root, owner, database = bound_access
    source = tmp_path / "source"
    source.write_bytes(b"owned")

    class RetainedSource(LocalFileSource):
        attempts = 0

        def close(self, deadline):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("local original still open")
            super().close(deadline)

    monkeypatch.setattr(access_module, "LocalFileSource", RetainedSource)
    with pytest.raises(PartialMutationError):
        access.upload_file(PurePosixPath(root / "file"), source, condition=Create(), create_metadata=metadata())
    aggregate = workflow(access, owner)
    assert not aggregate._components_settled()
    aggregate.close(cleanup_deadline=Deadline.after(3))
    assert aggregate._components_settled()
    assert database.operations.inspect(owner.ownership.scope) is None
    assert (root / "file").read_bytes() == b"owned"


def test_source_mutation_at_eof_is_conflict_before_remote_publication(bound_access, tmp_path, monkeypatch):
    access, root, owner, _ = bound_access
    source = tmp_path / "source"
    source.write_bytes(b"owned")

    class ChangedSource(LocalFileSource):
        changed = False

        def read(self, maximum):
            data = super().read(maximum)
            if data and not self.changed:
                self.changed = True
                source.write_bytes(b"changed")
            return data

    monkeypatch.setattr(access_module, "LocalFileSource", ChangedSource)
    with pytest.raises(ConflictError):
        access.upload_file(PurePosixPath(root / "file"), source, condition=Create(), create_metadata=metadata())
    assert not (root / "file").exists()
    assert not access._operation.active_uploads
    assert not access._operation.has_unfinished_local_file_call
    assert not owner.list_pending_lifecycle_obligations()
