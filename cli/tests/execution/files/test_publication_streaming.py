"""Verified scratch streaming into Linux atomic file publication."""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentworks.execution._file_publication as publication_module
import agentworks.execution._publication_receipt as publication_receipt_module
import agentworks.execution._scratch as scratch_module
import agentworks.execution._scratch_receipt as receipt_module
from agentworks.execution._file_publication import (
    Create,
    CreateMetadata,
    FilePublicationError,
    PublicationFailureKind,
    PublicationPhase,
    ScratchFileSource,
    publish_file,
)
from agentworks.execution._publication_receipt import (
    PublicationStageCleanupDebt,
    PublicationStageHistoricalOwnership,
    cleanup_publication_stage,
    reconcile_publication_stage,
)
from agentworks.execution._scratch import (
    ReadyScratchReference,
    begin_scratch,
    cleanup_scratch,
    verify_scratch,
    write_scratch_chunk,
)
from agentworks.execution._scratch_receipt import (
    _NAME_PREFIX,
    ScratchOperation,
    current_receipt_context,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux file publication")


def _open_parent(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _metadata() -> CreateMetadata:
    return CreateMetadata(os.getuid(), os.getgid(), 0o600)


def _scratch_directory(path: Path) -> Path:
    directories = list(path.glob(f"{_NAME_PREFIX}*"))
    assert len(directories) == 1
    return directories[0]


def _ready_scratch(parent_fd: int, content: bytes) -> ReadyScratchReference:
    reference = begin_scratch(
        parent_fd,
        len(content),
        secrets.token_bytes(16),
        current_receipt_context(ScratchOperation.STAGE),
    )
    digest = hashlib.sha256()
    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + scratch_module._MAX_CHUNK_BYTES]
        write_scratch_chunk(parent_fd, reference, offset, chunk, hashlib.sha256(chunk).digest())
        digest.update(chunk)
        offset += len(chunk)
    return verify_scratch(parent_fd, reference, digest.digest())


def _failure(
    parent_fd: int,
    content: ScratchFileSource,
    *,
    expires_at: float | None = None,
) -> FilePublicationError:
    with pytest.raises(FilePublicationError) as raised:
        publish_file(
            parent_fd,
            "target",
            content,
            condition=Create(),
            create_metadata=_metadata(),
            expires_at=expires_at,
        )
    return raised.value


def test_large_verified_scratch_is_copied_in_bounded_ranges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = os.urandom(5 * scratch_module._MAX_CHUNK_BYTES + 31)
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, content)
    original_write = publication_module._write
    original_read = scratch_module.read_scratch_range
    write_sizes: list[int] = []
    read_sizes: list[int] = []

    def recording_write(descriptor: int, block: memoryview) -> int:
        write_sizes.append(len(block))
        return original_write(descriptor, block)

    def recording_read(
        scratch_parent_fd: int,
        scratch_ready: ReadyScratchReference,
        offset: int,
        length: int,
        *,
        expires_at: float | None = None,
    ) -> bytes:
        read_sizes.append(length)
        return original_read(
            scratch_parent_fd,
            scratch_ready,
            offset,
            length,
            expires_at=expires_at,
        )

    monkeypatch.setattr(publication_module, "_write", recording_write)
    monkeypatch.setattr(scratch_module, "read_scratch_range", recording_read)
    try:
        revision = publish_file(
            parent_fd,
            "target",
            ScratchFileSource(parent_fd, ready),
            condition=Create(),
            create_metadata=_metadata(),
        )
        assert (tmp_path / "target").read_bytes() == content
        assert revision.digest == hashlib.sha256(content).digest()
        assert read_sizes and max(read_sizes) <= scratch_module._MAX_CHUNK_BYTES
        assert sum(read_sizes) == len(content)
        assert write_sizes and max(write_sizes) <= scratch_module._MAX_CHUNK_BYTES
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_scratch_short_write_loop_obeys_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"deadline-sensitive scratch"
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, content)
    original_write = publication_module._write
    moments = iter((0.0, 0.0, 0.0, 0.0, 10.0))
    writes = 0

    def short_write(descriptor: int, block: memoryview) -> int:
        nonlocal writes
        writes += 1
        return original_write(descriptor, block[:1])

    monkeypatch.setattr(publication_module, "_write", short_write)
    monkeypatch.setattr(publication_module, "time", SimpleNamespace(monotonic=lambda: next(moments)))
    clock = SimpleNamespace(monotonic=lambda: 0.0)
    monkeypatch.setattr(publication_receipt_module, "time", clock)
    monkeypatch.setattr(scratch_module, "time", clock)
    monkeypatch.setattr(receipt_module, "time", clock)
    try:
        error = _failure(parent_fd, ScratchFileSource(parent_fd, ready), expires_at=5.0)
        assert error.kind is PublicationFailureKind.DEADLINE
        assert writes == 1
        assert not (tmp_path / "target").exists()
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_empty_verified_scratch_is_validated_and_published(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, b"")
    try:
        revision = publish_file(
            parent_fd,
            "target",
            ScratchFileSource(parent_fd, ready),
            condition=Create(),
            create_metadata=_metadata(),
        )
        assert (tmp_path / "target").read_bytes() == b""
        assert revision.digest == hashlib.sha256(b"").digest()
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_changed_verified_scratch_refuses_without_publication(tmp_path: Path) -> None:
    content = b"verified scratch content"
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, content)
    scratch_data = _scratch_directory(tmp_path) / scratch_module._DATA_NAME
    with scratch_data.open("r+b", buffering=0) as stream:
        stream.write(b"X")
        os.fsync(stream.fileno())
    try:
        error = _failure(parent_fd, ScratchFileSource(parent_fd, ready))
        assert error.kind is PublicationFailureKind.CONFLICT
        assert error.phase is PublicationPhase.CONTENT
        assert not (tmp_path / "target").exists()
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_delayed_publication_after_scratch_cleanup_creates_no_artifact(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, b"verified")
    cleanup_scratch(parent_fd, ready)

    error = _failure(parent_fd, ScratchFileSource(parent_fd, ready))
    assert error.kind is PublicationFailureKind.CONFLICT
    assert error.phase is PublicationPhase.STAGING
    assert not (tmp_path / "target").exists()
    assert not list(tmp_path.glob(f"{publication_module._STAGE_PREFIX}*"))
    os.close(parent_fd)


def test_lost_record_ack_reconciles_cleanup_without_replaying_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, b"verified")
    original_record = publication_module.record_publication_stage

    def record_then_interrupt(*args: object, **kwargs: object) -> None:
        original_record(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(publication_module, "record_publication_stage", record_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        publish_file(
            parent_fd,
            "target",
            ScratchFileSource(parent_fd, ready),
            condition=Create(),
            create_metadata=_metadata(),
        )
    assert not (tmp_path / "target").exists()
    stages = list(tmp_path.glob(f"{publication_module._STAGE_PREFIX}*"))
    assert len(stages) == 1

    recovered = reconcile_publication_stage(parent_fd, ready._reference, parent_fd)
    assert isinstance(recovered, PublicationStageHistoricalOwnership)
    cleanup_publication_stage(parent_fd, parent_fd, recovered)
    cleanup_scratch(parent_fd, ready)
    assert not stages[0].exists()
    os.close(parent_fd)


def test_record_write_interruption_is_chained_to_closed_staging_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, b"verified")
    interrupt = KeyboardInterrupt()

    def interrupt_write(descriptor: int, content: bytes, expires_at: float | None) -> None:
        raise interrupt

    monkeypatch.setattr(publication_receipt_module, "_write_all", interrupt_write)
    with pytest.raises(KeyboardInterrupt) as raised:
        publish_file(
            parent_fd,
            "target",
            ScratchFileSource(parent_fd, ready),
            condition=Create(),
            create_metadata=_metadata(),
        )
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, FilePublicationError)
    assert cause.kind is PublicationFailureKind.IO
    assert cause.phase is PublicationPhase.STAGING
    assert cause.cleanup_debt is None
    assert not list(tmp_path.glob(f"{publication_module._STAGE_PREFIX}*"))
    assert not (_scratch_directory(tmp_path) / publication_receipt_module._RECORD_NAME).exists()

    cleanup_scratch(parent_fd, ready)
    os.close(parent_fd)


def test_content_failure_and_cleanup_interruption_preserve_prior_and_exact_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, b"verified")
    interrupt = KeyboardInterrupt()
    original_unlink = os.unlink

    def fail_content(descriptor: int, content: memoryview) -> int:
        raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONTENT)

    def interrupt_stage_unlink(path: str, *args: object, **kwargs: object) -> None:
        if path.startswith(publication_module._STAGE_PREFIX):
            raise interrupt
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(publication_module, "_write", fail_content)
    monkeypatch.setattr(os, "unlink", interrupt_stage_unlink)
    with pytest.raises(KeyboardInterrupt) as raised:
        publish_file(
            parent_fd,
            "target",
            ScratchFileSource(parent_fd, ready),
            condition=Create(),
            create_metadata=_metadata(),
        )
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, FilePublicationError)
    assert cause.kind is PublicationFailureKind.IO
    assert cause.phase is PublicationPhase.CONTENT
    debt = cause.cleanup_debt
    assert isinstance(debt, PublicationStageCleanupDebt)
    stage = tmp_path / debt.name
    observed = stage.stat()
    assert (debt.device, debt.inode) == (observed.st_dev, observed.st_ino)

    monkeypatch.setattr(os, "unlink", original_unlink)
    cleanup_publication_stage(parent_fd, parent_fd, debt)
    cleanup_scratch(parent_fd, ready)
    os.close(parent_fd)


def test_content_failure_and_cleanup_close_interruption_preserve_prior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, b"verified")
    interrupt = KeyboardInterrupt()
    opened_directory = publication_receipt_module._OpenedScratchDirectory
    original_close = opened_directory.close
    closes = 0

    def fail_content(
        descriptor: int,
        content: bytes | ScratchFileSource,
        expires_at: float | None,
    ) -> tuple[int, bytes]:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONTENT)

    def interrupt_cleanup_close(
        opened: publication_receipt_module._OpenedScratchDirectory,
    ) -> BaseException | None:
        nonlocal closes
        closes += 1
        result = original_close(opened)
        return interrupt if closes == 1 else result

    monkeypatch.setattr(publication_module, "_write_content", fail_content)
    monkeypatch.setattr(opened_directory, "close", interrupt_cleanup_close)
    with pytest.raises(KeyboardInterrupt) as raised:
        publish_file(
            parent_fd,
            "target",
            ScratchFileSource(parent_fd, ready),
            condition=Create(),
            create_metadata=_metadata(),
        )
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, FilePublicationError)
    assert cause.kind is PublicationFailureKind.CONFLICT
    assert cause.phase is PublicationPhase.CONTENT
    assert cause.cleanup_debt is None

    cleanup_scratch(parent_fd, ready)
    os.close(parent_fd)


def test_record_unlink_interruption_after_rename_preserves_uncertainty_and_exact_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, b"verified")
    interrupt = KeyboardInterrupt()
    original_unlink = os.unlink
    record_unlinks = 0

    def interrupt_then_fail_record_unlink(path: str, *args: object, **kwargs: object) -> None:
        nonlocal record_unlinks
        if path != publication_receipt_module._RECORD_NAME:
            original_unlink(path, *args, **kwargs)
            return
        record_unlinks += 1
        if record_unlinks == 1:
            raise interrupt
        raise OSError("fixture cleanup failure")

    monkeypatch.setattr(os, "unlink", interrupt_then_fail_record_unlink)
    with pytest.raises(KeyboardInterrupt) as raised:
        publish_file(
            parent_fd,
            "target",
            ScratchFileSource(parent_fd, ready),
            condition=Create(),
            create_metadata=_metadata(),
        )
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, FilePublicationError)
    assert cause.kind is PublicationFailureKind.UNCERTAIN
    assert cause.phase is PublicationPhase.PUBLICATION
    debt = cause.cleanup_debt
    assert isinstance(debt, PublicationStageCleanupDebt)
    assert debt._stage_removed
    assert (tmp_path / "target").read_bytes() == b"verified"

    monkeypatch.setattr(os, "unlink", original_unlink)
    cleanup_publication_stage(parent_fd, parent_fd, debt)
    cleanup_scratch(parent_fd, ready)
    os.close(parent_fd)


def test_scratch_iteration_deadline_remains_a_publication_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"scratch deadline"
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, content)
    expires_at = time.monotonic() + 30.0
    clock = SimpleNamespace(monotonic=lambda: expires_at)
    monkeypatch.setattr(scratch_module, "time", clock)
    monkeypatch.setattr(receipt_module, "time", clock)
    monkeypatch.setattr(publication_receipt_module, "time", clock)
    try:
        error = _failure(parent_fd, ScratchFileSource(parent_fd, ready), expires_at=expires_at)
        assert error.kind is PublicationFailureKind.DEADLINE
        assert error.phase is PublicationPhase.STAGING
        assert not (tmp_path / "target").exists()
        assert not list(tmp_path.glob(f"{publication_module._STAGE_PREFIX}*"))
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)
