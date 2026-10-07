"""Control interruption boundaries for publication ownership receipts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import agentworks.execution._publication_receipt as publication_receipt
from agentworks.execution._publication_receipt import (
    PublicationReceiptError,
    PublicationReceiptFailureKind,
    PublicationRecordAcquisition,
    admit_publication_stage,
    cleanup_publication_stage,
    reconcile_publication_stage,
    record_publication_stage,
)
from agentworks.execution._scratch import cleanup_scratch
from agentworks.execution._scratch_receipt import scratch_name
from tests.execution.files._publication_test_support import open_parent as _open_parent
from tests.execution.files._publication_test_support import ready_scratch as _ready_scratch
from tests.execution.files._publication_test_support import record_stage as _record_stage

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux publication receipts")


def test_record_write_interruption_retains_exact_cleanup_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("11" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    admission = admit_publication_stage(scratch_parent_fd, ready, publication_parent_fd)
    stage_fd = os.open(
        admission.stage_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=publication_parent_fd,
    )
    interrupt = KeyboardInterrupt()

    def interrupt_write(descriptor: int, content: bytes, expires_at: float | None) -> None:
        raise interrupt

    monkeypatch.setattr(publication_receipt, "_write_all", interrupt_write)
    with pytest.raises(KeyboardInterrupt) as raised:
        record_publication_stage(
            admission,
            publication_parent_fd,
            stage_fd,
            PublicationRecordAcquisition(),
        )
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, PublicationReceiptError)
    assert cause.kind is PublicationReceiptFailureKind.IO
    debt = cause.cleanup_debt
    assert debt is not None and not debt._stage_removed
    observed = os.stat(admission.stage_name, dir_fd=publication_parent_fd, follow_symlinks=False)
    assert (debt.device, debt.inode) == (observed.st_dev, observed.st_ino)

    os.close(stage_fd)
    assert admission.close() is None
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, debt)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_record_close_interruption_preserves_prior_receipt_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("16" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    admission = admit_publication_stage(scratch_parent_fd, ready, publication_parent_fd)
    stage_fd = os.open(
        admission.stage_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=publication_parent_fd,
    )
    original_open = os.open
    original_close = os.close
    record_fd: int | None = None
    interrupt = KeyboardInterrupt()

    def capture_record(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal record_fd
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == publication_receipt._RECORD_NAME:
            record_fd = descriptor
        return descriptor

    def conflict_write(descriptor: int, content: bytes, expires_at: float | None) -> None:
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)

    def interrupt_record_close(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor == record_fd:
            raise interrupt

    monkeypatch.setattr(os, "open", capture_record)
    monkeypatch.setattr(os, "close", interrupt_record_close)
    monkeypatch.setattr(publication_receipt, "_write_all", conflict_write)
    with pytest.raises(KeyboardInterrupt) as raised:
        record_publication_stage(
            admission,
            publication_parent_fd,
            stage_fd,
            PublicationRecordAcquisition(),
        )
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, PublicationReceiptError)
    assert cause.kind is PublicationReceiptFailureKind.CONFLICT
    debt = cause.cleanup_debt
    assert debt is not None and not debt._stage_removed

    monkeypatch.setattr(os, "close", original_close)
    os.close(stage_fd)
    assert admission.close() is None
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, debt)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_cleanup_close_interruption_preserves_prior_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    wrong_destination = tmp_path / "wrong-destination"
    scratch.mkdir()
    destination.mkdir()
    wrong_destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    wrong_parent_fd = _open_parent(wrong_destination)
    token = bytes.fromhex("17" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    opened_directory = publication_receipt._OpenedScratchDirectory
    original_close = opened_directory.close
    interrupt = KeyboardInterrupt()

    def close_then_interrupt(opened: publication_receipt._OpenedScratchDirectory) -> BaseException | None:
        original_close(opened)
        return interrupt

    monkeypatch.setattr(opened_directory, "close", close_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        cleanup_publication_stage(scratch_parent_fd, wrong_parent_fd, ownership)
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, PublicationReceiptError)
    assert cause.kind is PublicationReceiptFailureKind.CONFLICT
    assert cause.cleanup_debt is not None and not cause.cleanup_debt._stage_removed

    monkeypatch.setattr(opened_directory, "close", original_close)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(wrong_parent_fd)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_reconciliation_close_interruption_preserves_prior_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("18" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    opened_directory = publication_receipt._OpenedScratchDirectory
    original_close = opened_directory.close
    interrupt = KeyboardInterrupt()

    def expire_during_reconciliation(*args: object, **kwargs: object) -> None:
        raise PublicationReceiptError(PublicationReceiptFailureKind.DEADLINE)

    def close_then_interrupt(opened: publication_receipt._OpenedScratchDirectory) -> BaseException | None:
        original_close(opened)
        return interrupt

    monkeypatch.setattr(publication_receipt, "_reconcile_opened_publication_stage", expire_during_reconciliation)
    monkeypatch.setattr(opened_directory, "close", close_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        reconcile_publication_stage(
            scratch_parent_fd,
            ready._reference,
            publication_parent_fd,
            expires_at=float("inf"),
        )
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, PublicationReceiptError)
    assert cause.kind is PublicationReceiptFailureKind.DEADLINE
    assert cause.cleanup_debt is None

    monkeypatch.setattr(opened_directory, "close", original_close)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_record_descriptor_close_interruption_preserves_reconciliation_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("1a" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    original_open_record = publication_receipt._open_record
    original_close = os.close
    record_fd: int | None = None
    interrupt = KeyboardInterrupt()

    def capture_record(directory_fd: int) -> int:
        nonlocal record_fd
        record_fd = original_open_record(directory_fd)
        return record_fd

    def expire_record_read(descriptor: int, expires_at: float | None) -> bytes:
        raise PublicationReceiptError(PublicationReceiptFailureKind.DEADLINE)

    def interrupt_record_close(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor == record_fd:
            raise interrupt

    monkeypatch.setattr(publication_receipt, "_open_record", capture_record)
    monkeypatch.setattr(publication_receipt, "_pread_bounded", expire_record_read)
    monkeypatch.setattr(os, "close", interrupt_record_close)
    with pytest.raises(KeyboardInterrupt) as raised:
        reconcile_publication_stage(
            scratch_parent_fd,
            ready._reference,
            publication_parent_fd,
            expires_at=float("inf"),
        )
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, PublicationReceiptError)
    assert cause.kind is PublicationReceiptFailureKind.DEADLINE
    assert cause.cleanup_debt is None

    monkeypatch.setattr(os, "close", original_close)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_cleanup_preserves_open_validation_conflict_across_close_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("1b" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    original_validate = publication_receipt._validate_scratch_receipt
    opened_directory = publication_receipt._OpenedScratchDirectory
    original_close = opened_directory.close
    interrupt = KeyboardInterrupt()

    def reject_receipt(
        directory_fd: int,
        scratch_ownership: object,
        expires_at: float | None,
    ) -> None:
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)

    def close_then_interrupt(opened: publication_receipt._OpenedScratchDirectory) -> BaseException | None:
        original_close(opened)
        return interrupt

    monkeypatch.setattr(publication_receipt, "_validate_scratch_receipt", reject_receipt)
    monkeypatch.setattr(opened_directory, "close", close_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, PublicationReceiptError)
    assert cause.kind is PublicationReceiptFailureKind.CONFLICT
    assert cause.cleanup_debt is not None and not cause.cleanup_debt._stage_removed

    monkeypatch.setattr(publication_receipt, "_validate_scratch_receipt", original_validate)
    monkeypatch.setattr(opened_directory, "close", original_close)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_remove_record_close_interruption_preserves_prior_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("19" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    record = scratch / scratch_name(token) / publication_receipt._RECORD_NAME
    record.chmod(0o600)
    opened_directory = publication_receipt._OpenedScratchDirectory
    original_close = opened_directory.close
    interrupt = KeyboardInterrupt()

    def close_then_interrupt(opened: publication_receipt._OpenedScratchDirectory) -> BaseException | None:
        original_close(opened)
        return interrupt

    monkeypatch.setattr(opened_directory, "close", close_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        publication_receipt.remove_publication_record(scratch_parent_fd, publication_parent_fd, ownership)
    assert raised.value is interrupt
    cause = raised.value.__cause__
    assert isinstance(cause, PublicationReceiptError)
    assert cause.kind is PublicationReceiptFailureKind.CONFLICT
    assert cause.cleanup_debt is not None and cause.cleanup_debt._stage_removed

    monkeypatch.setattr(opened_directory, "close", original_close)
    record.chmod(publication_receipt._RECORD_MODE)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)
