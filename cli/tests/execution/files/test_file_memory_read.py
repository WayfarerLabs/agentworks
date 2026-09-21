"""Bounded in-memory read composition over owned snapshot downloads."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, OperationClaimState
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _file_memory_read
from agentworks.execution._file_download import (
    FileDownloadBinding,
    FileDownloadControlFact,
    FileDownloadFailure,
    FileDownloadOutcome,
    FileDownloadStatus,
)
from agentworks.execution._file_memory_read import FileMemoryReadOutcome, read_file
from agentworks.execution._file_snapshot_protocol import MAX_SNAPSHOT_CHUNK_BYTES
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._scratch_receipt import ScratchCleanupDebt
from agentworks.execution.carrier import Deadline
from tests.execution.files._file_download_support import owner
from tests.execution.files._file_snapshot_support import LocalCarrier, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection

if TYPE_CHECKING:
    from agentworks.execution.carrier import ByteSink, Carrier
    from agentworks.operations import OperationBorrow

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private snapshot helper requires Linux")

_QGA_RESPONSE_BOUND = 8 * 1024 * 1024
_PLAN = IdentityPlan(IdentityExpectation(1001, 1002, (1002,)), IdentityMode.DIRECT)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
_BINDING = FileDownloadBinding("/approved", "target", _QGA_RESPONSE_BOUND + 1, _PLAN, _RUNTIME)
_DEBT = ScratchCleanupDebt("private", None, None, None, None, (0o400,), 1001, 1002)


class ControlStop(BaseException):
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
        borrow: OperationBorrow,
    ) -> FileDownloadOutcome:
        del carrier, trusted_root_path, relative_path, max_bytes, plan, deadline, runtime_selection, borrow
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
    borrow: OperationBorrow,
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
        borrow=borrow,
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
    borrow = operation_owner.borrow()
    try:
        outcome = _read(LocalCarrier(), borrow, source, "target", max(1, len(content)), plan)

        assert outcome.download.status is FileDownloadStatus.COMPLETE
        assert outcome.download.stream_verified
        assert outcome.download.accepted_bytes == len(content)
        assert outcome.data == content
        assert not outcome.download.requires_owner_retention
        assert not tuple(scratch.iterdir())
        assert repr(content) not in repr(outcome)
        borrow.close()
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
    borrow = operation_owner.borrow()
    try:
        outcome = _read(LocalCarrier(), borrow, source, "absent", 1, plan)

        assert outcome.download.status is FileDownloadStatus.ABSENT
        assert outcome.download.accepted_bytes == 0
        assert outcome.data is None
        assert not outcome.download.requires_owner_retention
        assert not tuple(scratch.iterdir())
        borrow.close()
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
    borrow = operation_owner.borrow()
    try:
        outcome = _read(LocalCarrier(), borrow, source, "target", 4, plan)

        assert outcome.download.status is FileDownloadStatus.FAILED
        assert outcome.download.failure is FileDownloadFailure.SNAPSHOT
        assert outcome.download.accepted_bytes == 0
        assert outcome.data is None
        assert not outcome.download.requires_owner_retention
        assert not tuple(scratch.iterdir())
        borrow.close()
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
    borrow = operation_owner.borrow()
    fake = _FakeDownload(download, (b"partial",))
    monkeypatch.setattr(_file_memory_read, "download_file", fake)
    try:
        outcome = read_file(
            LocalCarrier(),
            trusted_root_path="/approved",
            relative_path="target",
            max_bytes=64,
            plan=_PLAN,
            deadline=Deadline.after(30),
            runtime_selection=_RUNTIME,
            borrow=borrow,
        )

        assert outcome.download is download
        assert outcome.data is None
        assert fake.sink is not None
        assert isinstance(fake.sink, _file_memory_read._MemorySink)  # noqa: SLF001
        assert fake.sink.data == bytearray()
        with pytest.raises(StateError):
            operation_owner.close()
        borrow.close()
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
        borrow: OperationBorrow,
    ) -> FileDownloadOutcome:
        nonlocal sink_seen
        del carrier, trusted_root_path, relative_path, max_bytes, plan, deadline, runtime_selection, borrow
        sink_seen = sink
        assert sink.try_write(memoryview(b"partial")) == 7
        raise control from fact

    monkeypatch.setattr(_file_memory_read, "download_file", fail_download)
    borrow = operation_owner.borrow()
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
                borrow=borrow,
            )

        assert raised.value is control and raised.value.__cause__ is fact
        assert fact.outcome is download
        assert isinstance(sink_seen, _file_memory_read._MemorySink)  # noqa: SLF001
        assert sink_seen.data == bytearray()
        assert "private-control-canary" not in repr(fact)
        with pytest.raises(StateError):
            operation_owner.borrow()
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_invalid_caller_bound_preserves_borrow_and_never_dispatches(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
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
                borrow=borrow,
            )

        assert carrier.calls == 0
        claim = database.operations.inspect(operation_owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.RESERVED
        with pytest.raises(StateError):
            operation_owner.close()
        borrow.close()
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
    monkeypatch.setattr(_file_memory_read, "download_file", fake)
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    try:
        outcome = read_file(
            LocalCarrier(),
            trusted_root_path="/approved",
            relative_path="target",
            max_bytes=len(content),
            plan=_PLAN,
            deadline=Deadline.after(30),
            runtime_selection=_RUNTIME,
            borrow=borrow,
        )

        assert outcome.download is download
        assert outcome.data == content
        borrow.close()
        operation_owner.close()
    finally:
        database.close()
