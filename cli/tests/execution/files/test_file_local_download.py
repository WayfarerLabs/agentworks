"""Private owned-download to Linux workstation publication composition."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.db import Database
from agentworks.execution import _file_local_download as local
from agentworks.execution._file_download import (
    FileDownloadBinding,
    FileDownloadControlFact,
    FileDownloadFailure,
    FileDownloadOutcome,
    FileDownloadStatus,
)
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_snapshot_protocol import MAX_SNAPSHOT_CHUNK_BYTES
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._local_download_publication import LocalDownloadPublication
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._scratch_receipt import ScratchCleanupDebt
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import Create, Replace
from tests.execution.files._file_download_support import owner
from tests.execution.files._file_snapshot_support import LocalCarrier, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner

if TYPE_CHECKING:
    from agentworks.execution.carrier import ByteSink, Carrier

pytestmark = [pytest.mark.windows, pytest.mark.skipif(sys.platform != "linux", reason="Linux local publication")]

_PLAN = IdentityPlan(IdentityExpectation(1001, 1002, (1002,)), IdentityMode.DIRECT)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
_BINDING = FileDownloadBinding("/approved", "source", 1024, _PLAN, _RUNTIME)
_DEBT = ScratchCleanupDebt("private", None, None, None, None, (0o400,), 1001, 1002)
_CREATE = Create()


class ControlStop(BaseException):
    pass


def _download(
    data: bytes,
    *,
    status: FileDownloadStatus = FileDownloadStatus.COMPLETE,
    failure: FileDownloadFailure | None = None,
    cleanup_debt: ScratchCleanupDebt | None = None,
    deadline_exceeded: bool = False,
    requires_owner_retention: bool = False,
) -> FileDownloadOutcome:
    revision = FileRevision(FileStat(1, 2, 0o100600, 1, 1001, 1002, len(data), 0, 0), hashlib.sha256(data).digest())
    return FileDownloadOutcome(
        status,
        _BINDING,
        b"t" * 16,
        len(data),
        stream_verified=True,
        source_revision=revision,
        failure=failure,
        cleanup_debt=cleanup_debt,
        deadline_exceeded=deadline_exceeded,
        requires_owner_retention=requires_owner_retention,
    )


class _FakeOperation:
    def __init__(
        self, outcome: FileDownloadOutcome, data: bytes = b"payload", control: BaseException | None = None
    ) -> None:
        self.outcome = outcome
        self.data = data
        self.control = control
        self.calls = 0
        self.deadline: Deadline | None = None

    def download(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        sink: ByteSink,
        max_bytes: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> FileDownloadOutcome:
        del carrier, trusted_root_path, relative_path, max_bytes, plan, runtime_selection
        self.calls += 1
        self.deadline = deadline
        if self.outcome.status is not FileDownloadStatus.ABSENT:
            assert sink.try_write(memoryview(self.data)) == len(self.data)
        if self.control is not None:
            raise self.control from FileDownloadControlFact(self.outcome)
        return self.outcome


def _run_fake(
    destination: Path,
    operation: _FakeOperation,
    *,
    deadline: Deadline | None = None,
    condition: Create | Replace = _CREATE,
) -> local.FileLocalDownloadOutcome:
    return local.download_to_local_file(
        LocalCarrier(),
        trusted_root_path="/approved",
        relative_path="source",
        destination=destination,
        max_bytes=1024,
        plan=_PLAN,
        deadline=deadline or Deadline.after(30),
        runtime_selection=_RUNTIME,
        operation=cast("FileOperation", operation),
        condition=condition,
    )


@pytest.fixture
def plan() -> IdentityPlan:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    source = tmp_path / "source-root"
    source.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    return source, scratch


@pytest.mark.parametrize("data", [b"", b"abc" * (MAX_SNAPSHOT_CHUNK_BYTES + 1)], ids=["empty", "multichunk"])
def test_real_owned_download_publishes_only_complete_bytes(
    tmp_path: Path, roots: tuple[Path, Path], plan: IdentityPlan, data: bytes
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(data)
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner, target_for_owner(operation_owner))
    destination = tmp_path / "destination"
    try:
        outcome = local.download_to_local_file(
            LocalCarrier(),
            trusted_root_path=str(source),
            relative_path="source",
            destination=destination,
            max_bytes=max(1, len(data)),
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            operation=operation,
        )
        assert outcome.download is not None and outcome.download.status is FileDownloadStatus.COMPLETE
        assert outcome.published and not outcome.publication_uncertain
        assert not outcome.cleanup_uncertain and not outcome.cleanup_failed
        assert not outcome.deadline_exceeded and outcome.local_failure is None
        assert destination.read_bytes() == data
        assert not tuple(scratch.iterdir())
        assert list(tmp_path.glob(".agw-download-*")) == []
        operation_owner.seal_lifecycle_obligations()
        operation_owner.record_effects_resolved()
        operation_owner.close()
    finally:
        database.close()


def test_real_absent_source_leaves_destination_unchanged(
    tmp_path: Path, roots: tuple[Path, Path], plan: IdentityPlan
) -> None:
    source, scratch = roots
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner, target_for_owner(operation_owner))
    destination = tmp_path / "destination"
    try:
        outcome = local.download_to_local_file(
            LocalCarrier(),
            trusted_root_path=str(source),
            relative_path="absent",
            destination=destination,
            max_bytes=1,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            operation=operation,
        )
        assert outcome.download is not None and outcome.download.status is FileDownloadStatus.ABSENT
        assert not outcome.published and not outcome.publication_uncertain
        assert not destination.exists() and not tuple(scratch.iterdir())
        assert list(tmp_path.glob(".agw-download-*")) == []
        operation_owner.seal_lifecycle_obligations()
        operation_owner.record_effects_resolved()
        operation_owner.close()
    finally:
        database.close()


def test_absent_source_does_not_replace_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.write_bytes(b"old")
    before = destination.stat()
    remote = _download(b"", status=FileDownloadStatus.ABSENT)
    operation = _FakeOperation(remote)
    outcome = _run_fake(destination, operation, condition=Replace())
    assert outcome.download is remote and not outcome.published
    assert destination.read_bytes() == b"old"
    assert destination.stat().st_ino == before.st_ino
    assert not list(tmp_path.glob(".agw-download-*"))


def test_default_create_refuses_existing_destination_before_remote_call(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.write_bytes(b"old")
    operation = _FakeOperation(_download(b"payload"))
    with pytest.raises(FileExistsError) as raised:
        _run_fake(destination, operation)
    fact = raised.value.__cause__
    assert isinstance(fact, local.FileLocalDownloadControlFact)
    assert fact.outcome.download is None and not fact.outcome.published
    assert fact.outcome.local_failure is local.LocalDownloadFailure.STAGING
    assert operation.calls == 0 and destination.read_bytes() == b"old"


def test_explicit_replace_preserves_existing_access_metadata(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.write_bytes(b"old")
    destination.chmod(0o640)
    before = destination.stat()
    operation = _FakeOperation(_download(b"payload"))
    outcome = _run_fake(destination, operation, condition=Replace())
    assert outcome.download is operation.outcome and outcome.published
    assert destination.read_bytes() == b"payload"
    assert destination.stat().st_mode == before.st_mode
    assert not list(tmp_path.glob(".agw-download-*"))


@pytest.mark.parametrize(
    "remote",
    [
        _download(
            b"payload", status=FileDownloadStatus.FAILED, failure=FileDownloadFailure.CLEANUP, cleanup_debt=_DEBT
        ),
        _download(b"payload", deadline_exceeded=True),
        _download(b"payload", requires_owner_retention=True),
    ],
    ids=["cleanup-debt", "remote-deadline", "retained-owner"],
)
def test_remote_failure_facts_block_publication(tmp_path: Path, remote: FileDownloadOutcome) -> None:
    destination = tmp_path / "destination"
    operation = _FakeOperation(remote)
    outcome = _run_fake(destination, operation)
    assert outcome.download is remote
    assert not outcome.published and not outcome.publication_uncertain
    assert not destination.exists() and not list(tmp_path.glob(".agw-download-*"))
    assert outcome.deadline_exceeded is remote.deadline_exceeded


def test_expiry_before_staging_refuses_without_remote_call(tmp_path: Path) -> None:
    operation = _FakeOperation(_download(b"payload"))
    outcome = _run_fake(tmp_path / "destination", operation, deadline=Deadline.after(0))
    assert outcome.download is None and outcome.local_failure is local.LocalDownloadFailure.DEADLINE
    assert operation.calls == 0 and not list(tmp_path.iterdir())


def test_expiry_after_remote_completion_blocks_publication(tmp_path: Path) -> None:
    deadline = Deadline.after(30)

    class ExpiringOperation(_FakeOperation):
        def download(self, *args: object, **kwargs: object) -> FileDownloadOutcome:
            outcome = super().download(*args, **kwargs)  # type: ignore[arg-type]
            object.__setattr__(deadline, "expires_at", time.monotonic() - 1)
            return outcome

    operation = ExpiringOperation(_download(b"payload"))
    outcome = _run_fake(tmp_path / "destination", operation, deadline=deadline)
    assert outcome.download is operation.outcome
    assert outcome.deadline_exceeded and outcome.local_failure is local.LocalDownloadFailure.DEADLINE
    assert not outcome.published and not list(tmp_path.iterdir())


def test_late_deadline_keeps_confirmed_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deadline = Deadline.after(30)

    class ExpiringWriter(LocalDownloadPublication):
        def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
            super().commit(verified_complete=verified_complete, size=size, sha256=sha256, deadline=deadline)
            object.__setattr__(deadline, "expires_at", time.monotonic() - 1)

    monkeypatch.setattr(local, "LocalDownloadPublication", ExpiringWriter)
    operation = _FakeOperation(_download(b"payload"))
    destination = tmp_path / "destination"
    outcome = _run_fake(destination, operation, deadline=deadline)
    assert outcome.download is operation.outcome
    assert outcome.published and outcome.deadline_exceeded
    assert outcome.local_failure is local.LocalDownloadFailure.DEADLINE
    assert destination.read_bytes() == b"payload"


def test_fsync_failure_keeps_remote_result_and_refuses_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_sync(fd: int) -> None:
        del fd
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", fail_sync)
    operation = _FakeOperation(_download(b"payload"))
    destination = tmp_path / "destination"
    with pytest.raises(OSError) as raised:
        _run_fake(destination, operation)
    fact = raised.value.__cause__
    assert isinstance(fact, local.FileLocalDownloadControlFact)
    assert fact.outcome.download is operation.outcome
    assert not fact.outcome.published and not fact.outcome.publication_uncertain
    assert fact.outcome.local_failure is local.LocalDownloadFailure.PUBLICATION
    assert not destination.exists() and not list(tmp_path.iterdir())


def test_fsync_expiry_keeps_remote_result_but_refuses_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadline = Deadline.after(30)
    sync = os.fsync

    def expire_after_sync(fd: int) -> None:
        sync(fd)
        object.__setattr__(deadline, "expires_at", time.monotonic() - 1)

    monkeypatch.setattr(os, "fsync", expire_after_sync)
    operation = _FakeOperation(_download(b"payload"))
    destination = tmp_path / "destination"
    with pytest.raises(TimeoutError) as raised:
        _run_fake(destination, operation, deadline=deadline)
    fact = raised.value.__cause__
    assert isinstance(fact, local.FileLocalDownloadControlFact)
    assert fact.outcome.download is operation.outcome
    assert fact.outcome.deadline_exceeded
    assert fact.outcome.local_failure is local.LocalDownloadFailure.DEADLINE
    assert not fact.outcome.published and not fact.outcome.publication_uncertain
    assert not destination.exists() and not list(tmp_path.iterdir())


def test_exceptional_remote_control_retains_its_outcome_and_aborts_stage(tmp_path: Path) -> None:
    remote = _download(b"payload", status=FileDownloadStatus.UNCERTAIN, cleanup_debt=_DEBT)
    control = ControlStop("remote-stop")
    operation = _FakeOperation(remote, control=control)
    with pytest.raises(ControlStop) as raised:
        _run_fake(tmp_path / "destination", operation)
    assert raised.value is control
    fact = raised.value.__cause__
    assert isinstance(fact, local.FileLocalDownloadControlFact)
    assert fact.outcome.download is remote
    assert isinstance(fact.__cause__, FileDownloadControlFact)
    assert fact.__cause__.outcome is remote
    assert not fact.outcome.published and not list(tmp_path.iterdir())


def test_publication_interrupt_after_link_retains_confirmed_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = os.link

    def interrupted_link(*args: object, **kwargs: object) -> None:
        link(*args, **kwargs)  # type: ignore[arg-type]
        raise ControlStop("after-link")

    monkeypatch.setattr(os, "link", interrupted_link)
    operation = _FakeOperation(_download(b"payload"))
    destination = tmp_path / "destination"
    with pytest.raises(ControlStop) as raised:
        _run_fake(destination, operation)
    fact = raised.value.__cause__
    assert isinstance(fact, local.FileLocalDownloadControlFact)
    assert fact.outcome.download is operation.outcome
    assert fact.outcome.published and not fact.outcome.publication_uncertain
    assert destination.read_bytes() == b"payload"
    assert not list(tmp_path.glob(".agw-download-*"))


def test_unconfirmed_publication_attempt_stays_uncertain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ControlStop("link-return-lost")

    monkeypatch.setattr(os, "link", interrupted_link)
    operation = _FakeOperation(_download(b"payload"))
    destination = tmp_path / "destination"
    with pytest.raises(ControlStop) as raised:
        _run_fake(destination, operation)
    fact = raised.value.__cause__
    assert isinstance(fact, local.FileLocalDownloadControlFact)
    assert fact.outcome.download is operation.outcome
    assert not fact.outcome.published and fact.outcome.publication_uncertain
    assert fact.outcome.local_failure is local.LocalDownloadFailure.PUBLICATION
    assert not destination.exists() and not list(tmp_path.iterdir())


def test_ambiguous_stage_close_retains_local_cleanup_fact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    close = os.close
    interrupted = False

    def ambiguous_close(fd: int) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            close(fd)
            raise ControlStop("close-return-lost")
        close(fd)

    monkeypatch.setattr(os, "close", ambiguous_close)
    operation = _FakeOperation(_download(b"payload", status=FileDownloadStatus.FAILED))
    destination = tmp_path / "destination"
    with pytest.raises(ControlStop) as raised:
        _run_fake(destination, operation)
    fact = raised.value.__cause__
    assert isinstance(fact, local.FileLocalDownloadControlFact)
    assert fact.outcome.download is operation.outcome
    assert fact.outcome.cleanup_uncertain and fact.outcome.cleanup_failed
    assert fact.outcome.unfinished_stage is not None
    assert not fact.outcome.published and not destination.exists()
    monkeypatch.setattr(os, "close", close)
    with pytest.raises(OSError):
        fact.outcome.unfinished_stage.abort()
    assert not list(tmp_path.iterdir())


def test_ambiguous_close_after_publication_never_claims_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    close = os.close
    interrupted = False

    def ambiguous_close(fd: int) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            close(fd)
            raise ControlStop("close-return-lost")
        close(fd)

    monkeypatch.setattr(os, "close", ambiguous_close)
    operation = _FakeOperation(_download(b"payload"))
    destination = tmp_path / "destination"
    with pytest.raises(ControlStop) as raised:
        _run_fake(destination, operation)
    fact = raised.value.__cause__
    assert isinstance(fact, local.FileLocalDownloadControlFact)
    assert fact.outcome.download is operation.outcome
    assert fact.outcome.published and not fact.outcome.publication_uncertain
    assert fact.outcome.cleanup_uncertain and fact.outcome.cleanup_failed
    assert fact.outcome.local_failure is local.LocalDownloadFailure.CLEANUP
    assert fact.outcome.unfinished_stage is not None
    assert destination.read_bytes() == b"payload"
    monkeypatch.setattr(os, "close", close)
    with pytest.raises(OSError):
        fact.outcome.unfinished_stage.abort()
    assert list(tmp_path.iterdir()) == [destination]
