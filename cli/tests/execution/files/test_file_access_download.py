"""Whole-call FileAccess download behavior and custody."""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from threading import Event, Thread, current_thread

import pytest

from agentworks.db import Database
from agentworks.errors import ConflictError, ExternalError, StateError, UncertainOutcomeError
from agentworks.execution import _file_local_download as local_download_module
from agentworks.execution import _file_operation as file_operation_module
from agentworks.execution import access as access_module
from agentworks.execution._file_local_download import (
    FileLocalDownloadControlFact,
    FileLocalDownloadOutcome,
    download_to_local_file,
)
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_result_transfer import reduce_file_local_download
from agentworks.execution._local_download_publication import LocalDownloadPublication
from agentworks.execution._local_download_stage import (
    LocalDownloadCleanupUncertainError,
    LocalDownloadUnsupportedError,
)
from agentworks.execution.access import FileAccess
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import Change, Create, FileFailureReason, FileOperationPhase, Replace
from agentworks.operations import OperationBorrow, OperationOwner, release_borrow_after_custody
from tests.execution.files._file_access_support import bound_access as bound_access
from tests.execution.files._file_access_support import plan as plan

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the fixed file helpers require Linux")


def test_download_publishes_held_snapshot_and_reports_source_metadata(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    access, root, _owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")
    destination = root.parent / "local-download"
    observed_bounds: list[int] = []
    original = download_to_local_file

    def recording_download(*args: object, **kwargs: object):
        observed_bounds.append(kwargs["max_bytes"])  # type: ignore[arg-type]
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(access_module, "download_to_local_file", recording_download)
    metadata = access.download(PurePosixPath(source), destination)
    assert observed_bounds == [(1 << 63) - 1]
    assert metadata.size == 7
    assert destination.read_bytes() == b"payload"
    with pytest.raises(ConflictError) as existing:
        access.download(PurePosixPath(source), destination)
    assert existing.value.details is not None
    assert existing.value.details.reason is FileFailureReason.CONFLICT
    assert destination.read_bytes() == b"payload"
    source.write_bytes(b"updated")
    replaced = access.download(PurePosixPath(source), destination, local_condition=Replace())
    assert replaced.size == 7
    assert destination.read_bytes() == b"updated"
    with pytest.raises(StateError) as absent:
        access.download(PurePosixPath(root / "absent"), root.parent / "absent-local")
    assert absent.value.details is not None
    assert absent.value.details.reason is FileFailureReason.NOT_FOUND
    assert not (root.parent / "absent-local").exists()


def test_download_retains_failed_local_cleanup_and_retries_before_next_stage(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    access, root, _owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")
    destination = root.parent / "local-download"

    class Stage:
        published = False
        publication_uncertain = False
        cleanup_uncertain = False

        def __init__(self) -> None:
            self.aborts = 0

        def abort(self) -> None:
            self.aborts += 1
            if self.aborts == 1:
                raise OSError("private local pathname")

        def try_write(self, data: memoryview) -> int:
            return len(data)

        def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
            del verified_complete, size, sha256, deadline

    stage = Stage()
    original = download_to_local_file
    calls = 0

    def first_fails(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            access._operation.retain_local_download_stage(stage)
            return FileLocalDownloadOutcome(None, cleanup_failed=True, unfinished_stage=stage)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(access_module, "download_to_local_file", first_fails)
    with pytest.raises(ExternalError) as failed:
        access.download(PurePosixPath(source), destination)
    assert failed.value.details is not None
    assert failed.value.details.reason is FileFailureReason.CLEANUP
    assert access._operation.retained_local_download_stage is stage
    assert calls == 1 and stage.aborts == 0
    with pytest.raises(ExternalError):
        access.download(PurePosixPath(source), destination)
    assert access._operation.retained_local_download_stage is stage
    assert calls == 1 and stage.aborts == 1
    metadata = access.download(PurePosixPath(source), destination)
    assert metadata.size == 7 and destination.read_bytes() == b"payload"
    assert stage.aborts == 2 and calls == 2
    assert access._operation.retained_local_download_stage is None


def test_download_preserves_interrupt_and_attached_local_custody(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    access, root, _owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")

    class Stage:
        published = False
        publication_uncertain = False
        cleanup_uncertain = True

        def __init__(self) -> None:
            self.known_cleanup_done = False
            self.aborts = 0

        def abort(self) -> None:
            self.aborts += 1
            self.known_cleanup_done = True
            raise LocalDownloadCleanupUncertainError("ambiguous close remains")

        def try_write(self, data: memoryview) -> int:
            return len(data)

        def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
            del verified_complete, size, sha256, deadline

    stage = Stage()
    facts = FileLocalDownloadOutcome(None, cleanup_uncertain=True, cleanup_failed=True, unfinished_stage=stage)
    interrupt = KeyboardInterrupt()

    def interrupted(*args: object, **kwargs: object):
        access._operation.retain_local_download_stage(stage)
        raise interrupt from FileLocalDownloadControlFact(facts)

    monkeypatch.setattr(access_module, "download_to_local_file", interrupted)
    with pytest.raises(KeyboardInterrupt) as stopped:
        access.download(PurePosixPath(source), root.parent / "local-download")
    assert stopped.value is interrupt
    assert isinstance(stopped.value.__cause__, FileLocalDownloadControlFact)
    assert stopped.value.__cause__.outcome is facts
    assert access._operation.retained_local_download_stage is stage
    with pytest.raises(ExternalError):
        access.download(PurePosixPath(source), root.parent / "second-download")
    assert stage.known_cleanup_done and stage.aborts == 1
    assert access._operation.retained_local_download_stage is stage


def test_download_reduces_local_failures_without_path_details(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    access, root, _owner, _database = bound_access
    source = PurePosixPath(root / "source")
    private_path = str(root.parent / "private-destination")

    def refused(*args: object, **kwargs: object):
        raise LocalDownloadUnsupportedError(private_path) from FileLocalDownloadControlFact(
            FileLocalDownloadOutcome(None)
        )

    monkeypatch.setattr(access_module, "download_to_local_file", refused)
    with pytest.raises(StateError) as raised:
        access.download(source, root.parent / "destination")
    assert raised.value.details is not None
    assert raised.value.details.phase is FileOperationPhase.PUBLICATION
    assert raised.value.details.reason is FileFailureReason.UNSUPPORTED
    assert private_path not in str(raised.value)

    with pytest.raises(UncertainOutcomeError) as uncertain:
        reduce_file_local_download(
            FileLocalDownloadOutcome(None, publication_uncertain=True),
            entity_kind="file",
            entity_name="configuration",
        )
    assert uncertain.value.details is not None
    assert uncertain.value.details.phase is FileOperationPhase.PUBLICATION


def test_expired_local_download_does_not_admit_borrow_or_stage(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    access, root, _owner, _database = bound_access
    monkeypatch.setattr(access, "_deadline", lambda: Deadline.after(0))

    def unexpected_stage(*args: object, **kwargs: object):
        del args, kwargs
        raise AssertionError("expired download must not stage")

    monkeypatch.setattr(local_download_module, "_publisher_for_host", unexpected_stage)
    with pytest.raises(ExternalError) as expired:
        access.download(PurePosixPath(root / "source"), root.parent / "local-download")
    assert expired.value.details is not None
    assert expired.value.details.reason is FileFailureReason.DEADLINE
    assert access._operation._local_download_call is None


@pytest.mark.parametrize("phase", ["constructor", "commit", "abort", "reduction"])
def test_local_download_holds_owner_borrow_through_public_reduction(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    access, root, _owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")
    destination = root.parent / "local-download"
    entered = Event()
    proceed = Event()
    stage_calls = 0
    returned: list[object] = []

    def block(selected: str) -> None:
        if phase == selected:
            entered.set()
            assert proceed.wait(10)

    class BlockingStage:
        def __init__(self, path: Path, condition: Create | Replace) -> None:
            self._inner = LocalDownloadPublication(path, condition=condition)

        @property
        def published(self) -> bool:
            return self._inner.published

        @property
        def publication_uncertain(self) -> bool:
            return self._inner.publication_uncertain

        @property
        def cleanup_uncertain(self) -> bool:
            return self._inner.cleanup_uncertain

        def try_write(self, data: memoryview) -> int:
            return self._inner.try_write(data)

        def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
            block("commit")
            self._inner.commit(verified_complete=verified_complete, size=size, sha256=sha256, deadline=deadline)

        def abort(self) -> None:
            block("abort")
            self._inner.abort()

    def publisher(path: Path, condition: Create | Replace) -> BlockingStage:
        nonlocal stage_calls
        stage_calls += 1
        block("constructor")
        return BlockingStage(path, condition)

    original_reducer = reduce_file_local_download

    def reducer(*args: object, **kwargs: object):
        block("reduction")
        return original_reducer(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(local_download_module, "_publisher_for_host", publisher)
    monkeypatch.setattr(access_module, "reduce_file_local_download", reducer)

    def run_first() -> None:
        try:
            returned.append(access.download(PurePosixPath(source), destination))
        except BaseException as exc:
            returned.append(exc)

    worker = Thread(target=run_first, daemon=True)
    worker.start()
    try:
        assert entered.wait(10)
        with pytest.raises(StateError):
            access.download(PurePosixPath(source), root.parent / "second-download")
        with pytest.raises(StateError):
            access.stat(PurePosixPath(source))
        assert stage_calls == 1
    finally:
        proceed.set()
        worker.join(10)
    assert not worker.is_alive()
    assert len(returned) == 1 and not isinstance(returned[0], BaseException)
    assert destination.read_bytes() == b"payload"


def test_local_cleanup_retry_holds_owner_borrow_before_new_stage(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database],
) -> None:
    access, root, _owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")
    entered = Event()
    proceed = Event()
    returned: list[object] = []

    class RetainedStage:
        published = False
        publication_uncertain = False
        cleanup_uncertain = False
        aborts = 0

        def try_write(self, data: memoryview) -> int:
            return len(data)

        def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
            del verified_complete, size, sha256, deadline

        def abort(self) -> None:
            self.aborts += 1
            entered.set()
            assert proceed.wait(10)

    stage = RetainedStage()
    access._operation.retain_local_download_stage(stage)

    def run_first() -> None:
        try:
            returned.append(access.download(PurePosixPath(source), root.parent / "local-download"))
        except BaseException as exc:
            returned.append(exc)

    worker = Thread(target=run_first, daemon=True)
    worker.start()
    try:
        assert entered.wait(10)
        with pytest.raises(StateError):
            access.download(PurePosixPath(source), root.parent / "second-download")
        assert stage.aborts == 1
    finally:
        proceed.set()
        worker.join(10)
    assert not worker.is_alive()
    assert len(returned) == 1 and not isinstance(returned[0], BaseException)
    assert access._operation.retained_local_download_stage is None


def test_unknown_remote_control_keeps_active_download_and_serial_borrow(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    access, root, _owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")
    interrupt = KeyboardInterrupt()

    def unknown_remote(*args: object, **kwargs: object):
        del args, kwargs
        raise interrupt

    monkeypatch.setattr(FileOperation, "_run_prepared_download", unknown_remote)
    with pytest.raises(KeyboardInterrupt) as stopped:
        access.download(PurePosixPath(source), root.parent / "local-download")
    assert stopped.value is interrupt
    assert isinstance(stopped.value.__cause__, FileLocalDownloadControlFact)
    assert access._operation.active_downloads
    assert access._operation._local_download_call is not None
    with pytest.raises(StateError):
        access.stat(PurePosixPath(source))


@pytest.mark.parametrize("original_error", [False, True])
def test_failed_local_download_finalization_retains_call_and_error_priority(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database],
    monkeypatch: pytest.MonkeyPatch,
    original_error: bool,
) -> None:
    access, root, _owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")
    interrupt = KeyboardInterrupt()

    def failed_close(self: OperationBorrow) -> None:
        del self
        raise OSError("private finalization failure")

    monkeypatch.setattr(OperationBorrow, "close", failed_close)
    if original_error:

        def interrupted(*args: object, **kwargs: object):
            del args, kwargs
            raise interrupt from FileLocalDownloadControlFact(FileLocalDownloadOutcome(None))

        monkeypatch.setattr(access_module, "download_to_local_file", interrupted)
        with pytest.raises(KeyboardInterrupt) as stopped:
            access.download(PurePosixPath(source), root.parent / "local-download")
        assert stopped.value is interrupt
        assert isinstance(stopped.value.__cause__, FileLocalDownloadControlFact)
    else:
        with pytest.raises(ExternalError) as failed:
            access.download(PurePosixPath(source), root.parent / "local-download")
        assert failed.value.details is not None
        assert failed.value.details.reason is FileFailureReason.COORDINATION
        assert failed.value.details.effect is Change.CHANGED
    call = access._operation._local_download_call
    assert call is not None
    with pytest.raises(StateError):
        access.stat(PurePosixPath(source))


def test_finalization_interrupt_is_preserved_with_safe_outcome(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    access, root, _owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")
    interrupt = KeyboardInterrupt()

    def interrupted_close(self: OperationBorrow) -> None:
        del self
        raise interrupt

    monkeypatch.setattr(OperationBorrow, "close", interrupted_close)
    with pytest.raises(KeyboardInterrupt) as stopped:
        access.download(PurePosixPath(source), root.parent / "local-download")
    assert stopped.value is interrupt
    assert isinstance(stopped.value.__cause__, FileLocalDownloadControlFact)
    assert stopped.value.__cause__.outcome.published
    call = access._operation._local_download_call
    assert call is not None


def test_closed_then_interrupted_borrow_keeps_old_call_and_refuses_new_stage_or_retry(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    access, root, _owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")
    interrupt = KeyboardInterrupt()
    original_close = OperationBorrow.close
    original_publisher = local_download_module._publisher_for_host
    stage_calls = 0
    closes = 0

    def close_then_interrupt(borrow: OperationBorrow) -> None:
        nonlocal closes
        closes += 1
        original_close(borrow)
        if closes == 1:
            raise interrupt

    def publisher(path: Path, condition: Create | Replace):
        nonlocal stage_calls
        stage_calls += 1
        return original_publisher(path, condition)

    class RetainedStage:
        published = False
        publication_uncertain = False
        cleanup_uncertain = False
        aborts = 0

        def abort(self) -> None:
            self.aborts += 1

        def try_write(self, data: memoryview) -> int:
            return len(data)

        def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
            del verified_complete, size, sha256, deadline

    monkeypatch.setattr(OperationBorrow, "close", close_then_interrupt)
    monkeypatch.setattr(local_download_module, "_publisher_for_host", publisher)
    with pytest.raises(KeyboardInterrupt) as stopped:
        access.download(PurePosixPath(source), root.parent / "local-download")
    assert stopped.value is interrupt
    assert isinstance(stopped.value.__cause__, FileLocalDownloadControlFact)
    assert stopped.value.__cause__.outcome.published
    old_call = access._operation._local_download_call
    assert old_call is not None

    stage = RetainedStage()
    access._operation.retain_local_download_stage(stage)
    with pytest.raises(StateError):
        access.download(PurePosixPath(source), root.parent / "second-download")
    assert access._operation._local_download_call is old_call
    assert access._operation.retained_local_download_stage is stage
    assert stage_calls == 1 and stage.aborts == 0


@pytest.mark.parametrize("scenario", ["normal", "finalizer_interrupt", "original_interrupt"])
def test_delayed_borrow_refuses_next_call_before_stage_or_retained_cleanup_retry(
    scenario: str, bound_access: tuple[FileAccess, Path, OperationOwner, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    access, root, owner, _database = bound_access
    source = root / "source"
    source.write_bytes(b"payload")
    before_borrow = Event()
    admit_second = Event()
    released_first = Event()
    finish_first = Event()
    first_errors: list[BaseException] = []
    second_errors: list[BaseException] = []
    stage_calls = 0
    original_release = release_borrow_after_custody
    original_borrow = owner.borrow
    finalizer_interrupt = KeyboardInterrupt()
    original_interrupt = KeyboardInterrupt()

    class FailedAbortStage:
        published = False
        publication_uncertain = False
        cleanup_uncertain = False
        aborts = 0

        def try_write(self, data: memoryview) -> int:
            return len(data)

        def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
            del verified_complete, size, sha256, deadline
            if scenario == "original_interrupt":
                raise original_interrupt
            self.published = True

        def abort(self) -> None:
            self.aborts += 1
            raise OSError("owned stage cleanup failed")

    stage = FailedAbortStage()

    def publisher(path: Path, condition: Create | Replace) -> FailedAbortStage:
        nonlocal stage_calls
        del path, condition
        stage_calls += 1
        return stage

    def paused_release(borrow: OperationBorrow, *, retain_effect: bool = False) -> None:
        original_release(borrow, retain_effect=retain_effect)
        if current_thread().name == "first-local-download":
            released_first.set()
            assert finish_first.wait(10)
            if scenario != "normal":
                raise finalizer_interrupt

    def delayed_borrow() -> OperationBorrow:
        if current_thread().name == "second-local-download":
            before_borrow.set()
            assert admit_second.wait(10)
        return original_borrow()

    def run_first() -> None:
        try:
            access.download(PurePosixPath(source), root.parent / "local-download")
        except BaseException as exc:
            first_errors.append(exc)

    def run_second() -> None:
        try:
            access.download(PurePosixPath(source), root.parent / "second-download")
        except BaseException as exc:
            second_errors.append(exc)

    monkeypatch.setattr(local_download_module, "_publisher_for_host", publisher)
    monkeypatch.setattr(file_operation_module, "release_borrow_after_custody", paused_release)
    monkeypatch.setattr(owner, "borrow", delayed_borrow)
    second = Thread(target=run_second, name="second-local-download", daemon=True)
    first = Thread(target=run_first, name="first-local-download", daemon=True)
    second.start()
    try:
        assert before_borrow.wait(10)
        first.start()
        assert released_first.wait(10)
        old_call = access._operation._local_download_call
        assert old_call is not None
        assert access._operation.retained_local_download_stage is stage
        admit_second.set()
        second.join(10)
        assert not second.is_alive()
        assert len(second_errors) == 1 and isinstance(second_errors[0], StateError)
        assert access._operation._local_download_call is old_call
        assert stage_calls == 1 and stage.aborts == 1
    finally:
        admit_second.set()
        finish_first.set()
        first.join(10)
        second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert len(first_errors) == 1
    if scenario == "original_interrupt":
        assert first_errors[0] is original_interrupt
        assert isinstance(original_interrupt.__cause__, FileLocalDownloadControlFact)
        assert original_interrupt.__cause__.outcome.unfinished_stage is stage
    else:
        assert isinstance(first_errors[0], ExternalError)
        assert first_errors[0].details is not None
        assert first_errors[0].details.reason is FileFailureReason.CLEANUP
    assert access._operation._local_download_call is (old_call if scenario != "normal" else None)
    assert access._operation.retained_local_download_stage is stage
