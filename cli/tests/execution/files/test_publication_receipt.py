"""Recoverable ownership for token-derived Linux publication stages."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentworks.execution._publication_receipt as publication_receipt
import agentworks.execution._scratch as scratch_module
import agentworks.execution._scratch_receipt as scratch_receipt_module
from agentworks.execution._publication_receipt import (
    PublicationReceiptError,
    PublicationReceiptFailureKind,
    PublicationRecordAcquisition,
    PublicationStageHistoricalOwnership,
    PublicationStageOwnership,
    PublicationStageOwnershipUncertainty,
    admit_publication_stage,
    cleanup_publication_stage,
    publication_stage_name,
    reconcile_publication_stage,
    record_publication_stage,
)
from agentworks.execution._scratch import (
    ReadyScratchReference,
    ScratchFailureKind,
    ScratchTransferError,
    begin_scratch,
    cleanup_scratch,
    verify_scratch,
    write_scratch_chunk,
)
from agentworks.execution._scratch_receipt import ScratchOperation, current_receipt_context, scratch_name

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux publication receipts")


def _open_parent(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _ready_scratch(parent_fd: int, token: bytes, content: bytes = b"content") -> ReadyScratchReference:
    reference = begin_scratch(
        parent_fd,
        len(content),
        token,
        current_receipt_context(ScratchOperation.STAGE),
    )
    if content:
        write_scratch_chunk(parent_fd, reference, 0, content, hashlib.sha256(content).digest())
    return verify_scratch(parent_fd, reference, hashlib.sha256(content).digest())


def _record_stage(
    scratch_parent_fd: int,
    ready: ReadyScratchReference,
    publication_parent_fd: int,
) -> tuple[PublicationStageOwnership, int]:
    admission = admit_publication_stage(scratch_parent_fd, ready, publication_parent_fd)
    stage_fd = os.open(
        admission.stage_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=publication_parent_fd,
    )
    try:
        ownership = record_publication_stage(
            admission,
            publication_parent_fd,
            stage_fd,
            PublicationRecordAcquisition(),
        )
    finally:
        close_error = admission.close()
        assert close_error is None
    return ownership, stage_fd


def test_lost_ack_reconciles_exact_cleanup_ownership(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("01" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)

    record = scratch / scratch_name(token) / publication_receipt._RECORD_NAME
    encoded = record.read_bytes()
    assert len(encoded) <= publication_receipt._MAX_RECORD_BYTES
    assert stat.S_IMODE(record.stat().st_mode) == publication_receipt._RECORD_MODE
    assert json.dumps(json.loads(encoded), ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode() == encoded

    recovered = reconcile_publication_stage(scratch_parent_fd, ready._reference, publication_parent_fd)
    assert isinstance(recovered, PublicationStageHistoricalOwnership)
    assert recovered._ownership == ownership
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, recovered)
    assert not (destination / publication_stage_name(token)).exists()
    assert not record.exists()
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_delayed_admission_after_scratch_cleanup_creates_nothing(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("02" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    cleanup_scratch(scratch_parent_fd, ready)

    with pytest.raises(PublicationReceiptError) as raised:
        admit_publication_stage(scratch_parent_fd, ready, publication_parent_fd)
    assert raised.value.kind is PublicationReceiptFailureKind.CONFLICT
    assert not list(destination.iterdir())
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_partial_record_and_absent_stage_are_uncertain(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("03" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    record = scratch / scratch_name(token) / publication_receipt._RECORD_NAME
    record.chmod(0o600)
    record.write_bytes(record.read_bytes()[:5])
    record.chmod(publication_receipt._RECORD_MODE)

    partial = reconcile_publication_stage(scratch_parent_fd, ready._reference, publication_parent_fd)
    assert isinstance(partial, PublicationStageOwnershipUncertainty)
    assert (destination / publication_stage_name(token)).exists()

    record.unlink()
    missing = reconcile_publication_stage(scratch_parent_fd, ready._reference, publication_parent_fd)
    assert isinstance(missing, PublicationStageOwnershipUncertainty)
    record.write_bytes(publication_receipt._encode_record(ownership))
    record.chmod(publication_receipt._RECORD_MODE)
    (destination / publication_stage_name(token)).unlink()
    absent = reconcile_publication_stage(scratch_parent_fd, ready._reference, publication_parent_fd)
    assert isinstance(absent, PublicationStageOwnershipUncertainty)
    record.unlink()
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


@pytest.mark.parametrize("change", ["replacement", "link"])
def test_changed_stage_identity_refuses_cleanup_and_leaves_unrelated_files(tmp_path: Path, change: str) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("04" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    stage = destination / publication_stage_name(token)
    saved = destination / "saved-stage"
    if change == "replacement":
        stage.rename(saved)
        stage.write_bytes(b"unrelated")
    else:
        os.link(stage, saved)

    reconciled = reconcile_publication_stage(scratch_parent_fd, ready._reference, publication_parent_fd)
    assert isinstance(reconciled, PublicationStageOwnershipUncertainty)
    with pytest.raises(PublicationReceiptError) as raised:
        cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    assert raised.value.kind is PublicationReceiptFailureKind.CONFLICT
    assert raised.value.cleanup_debt is not None
    assert stage.exists()
    assert saved.exists()

    if change == "replacement":
        stage.unlink()
    else:
        saved.unlink()
    if change == "replacement":
        saved.rename(stage)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_reconciliation_requires_original_destination_parent(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    other = tmp_path / "other"
    scratch.mkdir()
    destination.mkdir()
    other.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    other_parent_fd = _open_parent(other)
    token = bytes.fromhex("05" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)

    result = reconcile_publication_stage(scratch_parent_fd, ready._reference, other_parent_fd)
    assert isinstance(result, PublicationStageOwnershipUncertainty)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(other_parent_fd)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_cleanup_removes_stage_before_record_and_carries_record_only_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("06" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    stage = destination / publication_stage_name(token)
    record = scratch / scratch_name(token) / publication_receipt._RECORD_NAME
    original_unlink = os.unlink
    removals: list[str] = []

    def fail_record(path: str, *args: object, **kwargs: object) -> None:
        removals.append(path)
        if path == publication_receipt._RECORD_NAME:
            raise OSError("fixture record cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_record)
    with pytest.raises(PublicationReceiptError) as raised:
        cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    debt = raised.value.cleanup_debt
    assert debt is not None and debt._stage_removed
    assert removals == [publication_stage_name(token), publication_receipt._RECORD_NAME]
    assert not stage.exists()
    assert record.exists()

    monkeypatch.setattr(os, "unlink", original_unlink)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, debt)
    assert not record.exists()
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_scratch_cleanup_refuses_record_before_deleting_data(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("07" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    data = scratch / scratch_name(token) / "data"

    with pytest.raises(ScratchTransferError) as raised:
        cleanup_scratch(scratch_parent_fd, ready)
    assert raised.value.kind is ScratchFailureKind.CONFLICT
    assert data.exists()
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_expired_admission_performs_no_destination_mutation(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("08" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)

    with pytest.raises(PublicationReceiptError) as raised:
        admit_publication_stage(scratch_parent_fd, ready, publication_parent_fd, expires_at=0.0)
    assert raised.value.kind is PublicationReceiptFailureKind.DEADLINE
    assert not list(destination.iterdir())
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_cleanup_allows_intended_stage_metadata_changes(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("09" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.fchmod(stage_fd, 0o640)
    os.close(stage_fd)

    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_expiry_after_record_mutation_carries_exact_cleanup_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("0a" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    admission = admit_publication_stage(scratch_parent_fd, ready, publication_parent_fd)
    stage_fd = os.open(
        admission.stage_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=publication_parent_fd,
    )
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    original_open = os.open
    original_close = os.close
    record_fd: int | None = None

    def record_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal record_fd
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == publication_receipt._RECORD_NAME:
            record_fd = descriptor
        return descriptor

    def close_then_expire(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor == record_fd:
            clock[0] = 10.0

    monkeypatch.setattr(publication_receipt, "time", fake_time)
    monkeypatch.setattr(scratch_receipt_module, "time", fake_time)
    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", close_then_expire)
    with pytest.raises(PublicationReceiptError) as raised:
        record_publication_stage(
            admission,
            publication_parent_fd,
            stage_fd,
            PublicationRecordAcquisition(),
            expires_at=5.0,
        )
    debt = raised.value.cleanup_debt
    assert raised.value.kind is PublicationReceiptFailureKind.DEADLINE
    assert debt is not None and not debt._stage_removed

    monkeypatch.setattr(os, "close", original_close)
    monkeypatch.setattr(os, "open", original_open)
    os.close(stage_fd)
    assert admission.close() is None
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, debt)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_reconciliation_uses_original_reference_without_ready_content_proof(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("0b" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    reference = ready._reference
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    data = scratch / scratch_name(token) / "data"
    data.write_bytes(b"changed")

    recovered = reconcile_publication_stage(scratch_parent_fd, reference, publication_parent_fd)
    assert isinstance(recovered, PublicationStageHistoricalOwnership)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, recovered)
    cleanup_scratch(scratch_parent_fd, reference)
    assert recovered._ownership == ownership
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_expiry_during_final_stage_validation_retains_cleanup_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("0c" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    _, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    original_matches_stage = publication_receipt._matches_stage

    def match_then_expire(observed: os.stat_result, parent_device: int, identity: object) -> bool:
        matches = original_matches_stage(observed, parent_device, identity)  # type: ignore[arg-type]
        clock[0] = 10.0
        return matches

    monkeypatch.setattr(publication_receipt, "time", fake_time)
    monkeypatch.setattr(scratch_receipt_module, "time", fake_time)
    monkeypatch.setattr(publication_receipt, "_matches_stage", match_then_expire)
    with pytest.raises(PublicationReceiptError) as raised:
        reconcile_publication_stage(
            scratch_parent_fd,
            ready._reference,
            publication_parent_fd,
            expires_at=5.0,
        )
    debt = raised.value.cleanup_debt
    assert raised.value.kind is PublicationReceiptFailureKind.DEADLINE
    assert debt is not None and not debt._stage_removed

    monkeypatch.setattr(publication_receipt, "_matches_stage", original_matches_stage)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, debt)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_expiry_during_reconciliation_descriptor_close_retains_cleanup_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("0d" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    _, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    original_close = scratch_module._OpenedScratch.close

    def close_then_expire(opened: scratch_module._OpenedScratch) -> BaseException | None:
        close_control = original_close(opened)
        clock[0] = 10.0
        return close_control

    monkeypatch.setattr(publication_receipt, "time", fake_time)
    monkeypatch.setattr(scratch_receipt_module, "time", fake_time)
    monkeypatch.setattr(scratch_module._OpenedScratch, "close", close_then_expire)
    with pytest.raises(PublicationReceiptError) as raised:
        reconcile_publication_stage(
            scratch_parent_fd,
            ready._reference,
            publication_parent_fd,
            expires_at=5.0,
        )
    debt = raised.value.cleanup_debt
    assert raised.value.kind is PublicationReceiptFailureKind.DEADLINE
    assert debt is not None and not debt._stage_removed

    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, debt)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)


def test_expiry_after_uncertain_reconciliation_carries_no_cleanup_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    destination = tmp_path / "destination"
    scratch.mkdir()
    destination.mkdir()
    scratch_parent_fd = _open_parent(scratch)
    publication_parent_fd = _open_parent(destination)
    token = bytes.fromhex("0e" * 16)
    ready = _ready_scratch(scratch_parent_fd, token)
    ownership, stage_fd = _record_stage(scratch_parent_fd, ready, publication_parent_fd)
    os.close(stage_fd)
    stage = destination / publication_stage_name(token)
    saved = destination / "saved-stage"
    stage.rename(saved)
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    original_close = scratch_module._OpenedScratch.close

    def close_then_expire(opened: scratch_module._OpenedScratch) -> BaseException | None:
        close_control = original_close(opened)
        clock[0] = 10.0
        return close_control

    monkeypatch.setattr(publication_receipt, "time", fake_time)
    monkeypatch.setattr(scratch_receipt_module, "time", fake_time)
    monkeypatch.setattr(scratch_module._OpenedScratch, "close", close_then_expire)
    with pytest.raises(PublicationReceiptError) as raised:
        reconcile_publication_stage(
            scratch_parent_fd,
            ready._reference,
            publication_parent_fd,
            expires_at=5.0,
        )
    assert raised.value.kind is PublicationReceiptFailureKind.DEADLINE
    assert raised.value.cleanup_debt is None

    saved.rename(stage)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, ownership)
    cleanup_scratch(scratch_parent_fd, ready)
    os.close(publication_parent_fd)
    os.close(scratch_parent_fd)
