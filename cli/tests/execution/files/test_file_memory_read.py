"""Bounded in-memory read composition over owned snapshot downloads."""

from __future__ import annotations

import os
import sys
from builtins import bytes as builtin_bytes
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, OperationClaimState
from agentworks.errors import ValidationError
from agentworks.execution import _file_memory_read
from agentworks.execution._file_download import (
    FileDownloadBinding,
    FileDownloadControlFact,
    FileDownloadFailure,
    FileDownloadOutcome,
    FileDownloadStatus,
)
from agentworks.execution._file_memory_read import FileMemoryReadOutcome, read_file
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_snapshot_protocol import MAX_SNAPSHOT_CHUNK_BYTES
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._scratch_receipt import ScratchCleanupDebt
from agentworks.execution.carrier import Deadline
from tests.execution.files._file_download_support import LostCallStdoutCarrier, owner
from tests.execution.files._file_snapshot_support import LocalCarrier, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection

if TYPE_CHECKING:
    from agentworks.execution.carrier import ByteSink, Carrier

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private snapshot helper requires Linux")

_QGA_RESPONSE_BOUND = 8 * 1024 * 1024
_PLAN = IdentityPlan(IdentityExpectation(1001, 1002, (1002,)), IdentityMode.DIRECT)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
_BINDING = FileDownloadBinding("/approved", "target", _QGA_RESPONSE_BOUND + 1, _PLAN, _RUNTIME)
_DEBT = ScratchCleanupDebt("private", None, None, None, None, (0o400,), 1001, 1002)


class ControlStop(BaseException):
    pass


class AllocationStop(Exception):
    pass


class _FakeDownload:
    def __init__(self, outcome: FileDownloadOutcome, chunks: tuple[bytes, ...]) -> None:
        self.outcome = outcome
        self.chunks = chunks
        self.sink: ByteSink | None = None

    def __call__(
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
        del carrier, trusted_root_path, relative_path, max_bytes, plan, deadline, runtime_selection
        self.sink = sink
        for chunk in self.chunks:
            assert sink.try_write(memoryview(chunk)) == len(chunk)
        return self.outcome


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


def _read(
    carrier: Carrier,
    operation: FileOperation,
    source: Path,
    relative_path: str,
    max_bytes: int,
    plan: IdentityPlan,
) -> FileMemoryReadOutcome:
    return read_file(
        carrier,
        trusted_root_path=str(source),
        relative_path=relative_path,
        max_bytes=max_bytes,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        operation=operation,
    )


@pytest.mark.parametrize(
    "content",
    [b"", bytes(range(256)) * ((MAX_SNAPSHOT_CHUNK_BYTES * 2 // 256) + 1)],
    ids=["empty", "multichunk"],
)
def test_real_linux_read_returns_only_complete_verified_data(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    content: bytes,
) -> None:
    source, scratch = roots
    source.joinpath("target").write_bytes(content)
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner)
    try:
        outcome = _read(LocalCarrier(), operation, source, "target", max(1, len(content)), plan)

        assert outcome.download.status is FileDownloadStatus.COMPLETE
        assert outcome.download.stream_verified
        assert outcome.download.accepted_bytes == len(content)
        assert outcome.data == content
        assert not outcome.download.requires_owner_retention
        assert not tuple(scratch.iterdir())
        assert repr(content) not in repr(outcome)
        operation_owner.close()
    finally:
        database.close()


def test_real_linux_absence_returns_no_data(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner)
    try:
        outcome = _read(LocalCarrier(), operation, source, "absent", 1, plan)

        assert outcome.download.status is FileDownloadStatus.ABSENT
        assert outcome.download.accepted_bytes == 0
        assert outcome.data is None
        assert not outcome.download.requires_owner_retention
        assert not tuple(scratch.iterdir())
        operation_owner.close()
    finally:
        database.close()


def test_real_linux_caller_bound_refusal_exposes_no_partial_data(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("target").write_bytes(b"bounded-content")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner)
    try:
        outcome = _read(LocalCarrier(), operation, source, "target", 4, plan)

        assert outcome.download.status is FileDownloadStatus.FAILED
        assert outcome.download.failure is FileDownloadFailure.SNAPSHOT
        assert outcome.download.accepted_bytes == 0
        assert outcome.data is None
        assert not outcome.download.requires_owner_retention
        assert not tuple(scratch.iterdir())
        operation_owner.close()
    finally:
        database.close()


@pytest.mark.parametrize(
    "download",
    [
        FileDownloadOutcome(
            FileDownloadStatus.FAILED,
            _BINDING,
            b"s" * 16,
            7,
            failure=FileDownloadFailure.SINK,
        ),
        FileDownloadOutcome(
            FileDownloadStatus.FAILED,
            _BINDING,
            b"c" * 16,
            7,
            stream_verified=True,
            cleanup_debt=_DEBT,
            failure=FileDownloadFailure.CLEANUP,
            requires_owner_retention=True,
        ),
        FileDownloadOutcome(
            FileDownloadStatus.FAILED,
            _BINDING,
            b"d" * 16,
            7,
            cleanup_debt=_DEBT,
            deadline_exceeded=True,
            failure=FileDownloadFailure.DEADLINE,
            requires_owner_retention=True,
        ),
    ],
    ids=["sink", "cleanup", "deadline"],
)
def test_partial_bytes_are_discarded_without_changing_download_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    download: FileDownloadOutcome,
) -> None:
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner)
    fake = _FakeDownload(download, (b"partial",))
    monkeypatch.setattr(operation, "download", fake)
    try:
        outcome = read_file(
            LocalCarrier(),
            trusted_root_path="/approved",
            relative_path="target",
            max_bytes=64,
            plan=_PLAN,
            deadline=Deadline.after(30),
            runtime_selection=_RUNTIME,
            operation=operation,
        )

        assert outcome.download is download
        assert outcome.data is None
        assert fake.sink is not None
        assert isinstance(fake.sink, _file_memory_read._MemorySink)  # noqa: SLF001
        assert fake.sink.data == bytearray()
        operation_owner.close()
    finally:
        database.close()


def test_original_control_and_download_fact_escape_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    download = FileDownloadOutcome(
        FileDownloadStatus.UNCERTAIN,
        _BINDING,
        b"u" * 16,
        7,
        pending_remote_effects=True,
        requires_owner_retention=True,
    )
    control = ControlStop("private-control-canary")
    fact = FileDownloadControlFact(download)
    sink_seen: ByteSink | None = None

    def fail_download(
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
        nonlocal sink_seen
        del carrier, trusted_root_path, relative_path, max_bytes, plan, deadline, runtime_selection
        sink_seen = sink
        assert sink.try_write(memoryview(b"partial")) == 7
        raise control from fact

    operation = FileOperation(operation_owner)
    monkeypatch.setattr(operation, "download", fail_download)
    try:
        with pytest.raises(ControlStop) as raised:
            read_file(
                LocalCarrier(),
                trusted_root_path="/approved",
                relative_path="target",
                max_bytes=64,
                plan=_PLAN,
                deadline=Deadline.after(30),
                runtime_selection=_RUNTIME,
                operation=operation,
            )

        assert raised.value is control and raised.value.__cause__ is fact
        assert fact.outcome is download
        assert isinstance(sink_seen, _file_memory_read._MemorySink)  # noqa: SLF001
        assert sink_seen.data == bytearray()
        assert "private-control-canary" not in repr(fact)
        operation_owner.close()
    finally:
        database.close()


def test_invalid_caller_bound_releases_borrow_and_never_dispatches(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner)
    carrier = LocalCarrier()
    try:
        with pytest.raises(ValidationError):
            read_file(
                carrier,
                trusted_root_path="/approved",
                relative_path="target",
                max_bytes=0,
                plan=_PLAN,
                deadline=Deadline.after(30),
                runtime_selection=_RUNTIME,
                operation=operation,
            )

        assert carrier.calls == 0
        claim = database.operations.inspect(operation_owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.RESERVED
        operation_owner.close()
    finally:
        database.close()


def test_result_allocation_failure_preserves_inert_download_custody(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = roots
    source.joinpath("target").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner)
    carrier = LostCallStdoutCarrier(3)
    sink = _file_memory_read._MemorySink()  # noqa: SLF001
    stop = AllocationStop("result-allocation-canary")

    def fail_result(download: FileDownloadOutcome, data: bytes | None) -> FileMemoryReadOutcome:
        assert data is None
        retained = operation.unfinished_downloads
        assert len(retained) == 1
        assert retained[0].carrier is carrier
        assert retained[0].outcome is download
        raise stop

    monkeypatch.setattr(_file_memory_read, "_MemorySink", lambda: sink)
    monkeypatch.setattr(_file_memory_read, "FileMemoryReadOutcome", fail_result)
    try:
        with pytest.raises(AllocationStop) as raised:
            _read(carrier, operation, source, "target", 64, plan)

        assert raised.value is stop
        retained = operation.unfinished_downloads
        assert len(retained) == 1
        assert retained[0].carrier is carrier
        assert retained[0].outcome.failure is FileDownloadFailure.CLEANUP
        assert retained[0].outcome.cleanup_debt is not None
        assert sink.data == bytearray()
        borrow = operation_owner.borrow()
        borrow.close()
    finally:
        database.close()


def test_complete_byte_allocation_failure_releases_borrow_after_cleanup(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, scratch = roots
    source.joinpath("target").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner)
    sink = _file_memory_read._MemorySink()  # noqa: SLF001
    downloads: list[FileDownloadOutcome] = []
    download = operation.download
    stop = AllocationStop("byte-allocation-canary")

    def observe_download(
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
        outcome = download(
            carrier,
            trusted_root_path=trusted_root_path,
            relative_path=relative_path,
            sink=sink,
            max_bytes=max_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
        )
        downloads.append(outcome)
        return outcome

    def fail_bytes(data: bytearray) -> bytes:
        assert builtin_bytes(data) == b"payload"
        borrow = operation_owner.borrow()
        borrow.close()
        raise stop

    monkeypatch.setattr(operation, "download", observe_download)
    monkeypatch.setattr(_file_memory_read, "_MemorySink", lambda: sink)
    monkeypatch.setattr(_file_memory_read, "bytes", fail_bytes, raising=False)
    try:
        with pytest.raises(AllocationStop) as raised:
            _read(LocalCarrier(), operation, source, "target", 64, plan)

        assert raised.value is stop
        assert len(downloads) == 1
        assert downloads[0].status is FileDownloadStatus.COMPLETE
        assert downloads[0].cleanup_debt is None
        assert not downloads[0].requires_owner_retention
        assert operation.active_downloads == ()
        assert operation.unfinished_downloads == ()
        assert sink.data == bytearray()
        assert not tuple(scratch.iterdir())
        operation_owner.close()
    finally:
        database.close()


def test_bounded_fake_collects_more_than_one_qga_response_without_native_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"q" * (_QGA_RESPONSE_BOUND + 1)
    chunks = tuple(content[offset : offset + 64 * 1024] for offset in range(0, len(content), 64 * 1024))
    download = FileDownloadOutcome(
        FileDownloadStatus.COMPLETE,
        _BINDING,
        b"q" * 16,
        len(content),
        stream_verified=True,
    )
    fake = _FakeDownload(download, chunks)
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    operation = FileOperation(operation_owner)
    monkeypatch.setattr(operation, "download", fake)
    try:
        outcome = read_file(
            LocalCarrier(),
            trusted_root_path="/approved",
            relative_path="target",
            max_bytes=len(content),
            plan=_PLAN,
            deadline=Deadline.after(30),
            runtime_selection=_RUNTIME,
            operation=operation,
        )

        assert outcome.download is download
        assert outcome.data == content
        operation_owner.close()
    finally:
        database.close()
