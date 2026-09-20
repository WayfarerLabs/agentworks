"""Receipt-backed historical ownership and exact cleanup behavior."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentworks.execution._scratch as scratch_module
import agentworks.execution._scratch_receipt as receipt_module
from agentworks.execution._scratch import (
    ScratchFailureKind,
    ScratchPhase,
    ScratchTransferError,
    begin_scratch,
    cleanup_scratch,
    reconcile_scratch_ownership,
    write_scratch_chunk,
)
from agentworks.execution._scratch_receipt import (
    ScratchHistoricalOwnership,
    ScratchOperation,
    ScratchOwnershipUncertainty,
    current_receipt_context,
    scratch_name,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "pread") or not hasattr(os, "pwrite"),
    reason="POSIX descriptor-relative scratch transfer",
)


def _open_parent(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _begin(parent_fd: int, length: int, token: bytes | None = None) -> tuple[bytes, scratch_module.ScratchReference]:
    actual_token = os.urandom(16) if token is None else token
    context = current_receipt_context(ScratchOperation.STAGE)
    return actual_token, begin_scratch(parent_fd, length, actual_token, context)


def _directory(path: Path, token: bytes) -> Path:
    return path / scratch_name(token)


def _cause(error: BaseException) -> ScratchTransferError:
    assert isinstance(error.__cause__, ScratchTransferError)
    return error.__cause__


def test_receipt_is_bounded_canonical_immutable_and_reconciles_for_cleanup(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    token, reference = _begin(parent_fd, 3)
    directory = _directory(tmp_path, token)
    receipt = directory / receipt_module._RECEIPT_NAME

    content = receipt.read_bytes()
    assert len(content) <= receipt_module._MAX_RECEIPT_BYTES
    assert stat.S_IMODE(receipt.stat().st_mode) == receipt_module._RECEIPT_MODE
    assert json.dumps(json.loads(content), ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode() == content

    historical = reconcile_scratch_ownership(
        parent_fd,
        token,
        current_receipt_context(ScratchOperation.STAGE),
    )
    assert isinstance(historical, ScratchHistoricalOwnership)
    assert historical._ownership == reference._ownership
    cleanup_scratch(parent_fd, historical)
    assert not directory.exists()
    os.close(parent_fd)


def test_reconcile_retains_ownership_when_data_is_incomplete_or_absent(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    token, reference = _begin(parent_fd, 4)
    context = current_receipt_context(ScratchOperation.STAGE)
    write_scratch_chunk(parent_fd, reference, 0, b"ab", hashlib.sha256(b"ab").digest())

    incomplete = reconcile_scratch_ownership(parent_fd, token, context)
    assert isinstance(incomplete, ScratchHistoricalOwnership)
    directory_fd = os.open(_directory(tmp_path, token), os.O_RDONLY | os.O_DIRECTORY)
    os.unlink(receipt_module._DATA_NAME, dir_fd=directory_fd)
    os.close(directory_fd)
    absent = reconcile_scratch_ownership(parent_fd, token, context)
    assert isinstance(absent, ScratchHistoricalOwnership)
    cleanup_scratch(parent_fd, absent)
    os.close(parent_fd)


@pytest.mark.parametrize("receipt_change", ["missing", "invalid"])
def test_missing_or_invalid_receipt_is_uncertainty_not_absence(tmp_path: Path, receipt_change: str) -> None:
    parent_fd = _open_parent(tmp_path)
    token, _ = _begin(parent_fd, 0)
    receipt = _directory(tmp_path, token) / receipt_module._RECEIPT_NAME
    if receipt_change == "missing":
        receipt.unlink()
    else:
        receipt.chmod(0o600)
        receipt.write_bytes(b"{}")
        receipt.chmod(receipt_module._RECEIPT_MODE)

    result = reconcile_scratch_ownership(
        parent_fd,
        token,
        current_receipt_context(ScratchOperation.STAGE),
    )
    assert isinstance(result, ScratchOwnershipUncertainty)
    os.close(parent_fd)


def test_wrong_operation_or_original_parent_never_adopts_receipt(tmp_path: Path) -> None:
    original = tmp_path / "original"
    other = tmp_path / "other"
    original.mkdir()
    other.mkdir()
    original_fd = _open_parent(original)
    other_fd = _open_parent(other)
    token, reference = _begin(original_fd, 0)

    wrong_operation = reconcile_scratch_ownership(
        original_fd,
        token,
        current_receipt_context(ScratchOperation.SNAPSHOT),
    )
    wrong_parent = reconcile_scratch_ownership(
        other_fd,
        token,
        current_receipt_context(ScratchOperation.STAGE),
    )
    assert isinstance(wrong_operation, ScratchOwnershipUncertainty)
    assert isinstance(wrong_parent, ScratchOwnershipUncertainty)
    cleanup_scratch(original_fd, reference)
    os.close(other_fd)
    os.close(original_fd)


@pytest.mark.parametrize("change", ["missing", "replacement"])
def test_follow_on_write_validates_receipt_before_mutating_data(tmp_path: Path, change: str) -> None:
    parent_fd = _open_parent(tmp_path)
    token, reference = _begin(parent_fd, 1)
    directory = _directory(tmp_path, token)
    receipt = directory / receipt_module._RECEIPT_NAME
    content = receipt.read_bytes()
    retained_fd = os.open(receipt, os.O_RDONLY) if change == "replacement" else None
    receipt.unlink()
    if change == "replacement":
        receipt.write_bytes(content)
        receipt.chmod(receipt_module._RECEIPT_MODE)

    with pytest.raises(ScratchTransferError) as raised:
        write_scratch_chunk(parent_fd, reference, 0, b"x", hashlib.sha256(b"x").digest())
    assert raised.value.kind is ScratchFailureKind.CONFLICT
    assert (directory / receipt_module._DATA_NAME).read_bytes() == b""
    if retained_fd is not None:
        os.close(retained_fd)
    os.close(parent_fd)


def test_partial_receipt_creation_failure_is_cleaned_by_exact_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_write = os.write
    calls = 0

    def fail_after_prefix(descriptor: int, content: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, content[:1])
        raise OSError(errno.EIO, "fixture receipt failure")

    monkeypatch.setattr(os, "write", fail_after_prefix)
    with pytest.raises(ScratchTransferError) as raised:
        _begin(parent_fd, 0)
    assert raised.value.kind is ScratchFailureKind.IO
    assert raised.value.phase is ScratchPhase.BEGIN
    assert raised.value.cleanup_debt is None
    assert not list(tmp_path.iterdir())
    os.close(parent_fd)


def test_receipt_short_write_loop_obeys_deadline_but_cleanup_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_fd = _open_parent(tmp_path)
    clock = [0.0]
    original_write = os.write

    def write_then_expire(descriptor: int, content: bytes) -> int:
        written = original_write(descriptor, content[:1])
        clock[0] = 10.0
        return written

    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(os, "write", write_then_expire)
    monkeypatch.setattr(scratch_module, "time", fake_time)
    monkeypatch.setattr(receipt_module, "time", fake_time)
    with pytest.raises(ScratchTransferError) as raised:
        begin_scratch(
            parent_fd,
            0,
            os.urandom(16),
            current_receipt_context(ScratchOperation.STAGE),
            expires_at=5.0,
        )
    assert raised.value.kind is ScratchFailureKind.DEADLINE
    assert raised.value.phase is ScratchPhase.BEGIN
    assert raised.value.cleanup_debt is None
    assert not list(tmp_path.iterdir())
    os.close(parent_fd)


def test_cleanup_preserves_receipt_until_data_is_removed_and_reports_receipt_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    token, reference = _begin(parent_fd, 0)
    directory = _directory(tmp_path, token)
    receipt = directory / receipt_module._RECEIPT_NAME
    original_unlink = os.unlink

    def fail_data(path: object, *args: object, **kwargs: object) -> None:
        if path == receipt_module._DATA_NAME:
            raise OSError(errno.EIO, "fixture data removal failure")
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", fail_data)
    with pytest.raises(ScratchTransferError) as raised:
        cleanup_scratch(parent_fd, reference)
    debt = raised.value.cleanup_debt
    assert debt is not None
    assert receipt.exists()

    def interrupt_receipt(path: object, *args: object, **kwargs: object) -> None:
        if path == receipt_module._RECEIPT_NAME:
            raise KeyboardInterrupt
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", interrupt_receipt)
    with pytest.raises(KeyboardInterrupt) as interrupted:
        cleanup_scratch(parent_fd, debt)
    cause = _cause(interrupted.value)
    assert cause.phase is ScratchPhase.CLEANUP
    assert cause.cleanup_debt == debt
    assert receipt.exists()

    monkeypatch.setattr(os, "unlink", original_unlink)
    cleanup_scratch(parent_fd, debt)
    assert not directory.exists()
    os.close(parent_fd)


def test_reconcile_checks_deadline_after_receipt_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    token, reference = _begin(parent_fd, 0)
    clock = [0.0]
    original_pread = os.pread

    def read_then_expire(descriptor: int, length: int, offset: int) -> bytes:
        block = original_pread(descriptor, length, offset)
        clock[0] = 10.0
        return block

    monkeypatch.setattr(os, "pread", read_then_expire)
    monkeypatch.setattr(receipt_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(ScratchTransferError) as raised:
        reconcile_scratch_ownership(
            parent_fd,
            token,
            current_receipt_context(ScratchOperation.STAGE),
            expires_at=5.0,
        )
    assert raised.value.kind is ScratchFailureKind.DEADLINE
    assert raised.value.phase is ScratchPhase.RECONCILE
    monkeypatch.setattr(os, "pread", original_pread)
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)
