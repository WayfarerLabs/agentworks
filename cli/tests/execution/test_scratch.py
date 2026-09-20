"""Private descriptor-relative scratch transfer behavior."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentworks.execution._scratch as scratch_module
import agentworks.execution._scratch_receipt as receipt_module
from agentworks.execution._scratch import (
    ReadyScratchReference,
    ScratchFailureKind,
    ScratchPhase,
    ScratchTransferError,
    cleanup_scratch,
    iter_ready_scratch,
    read_scratch_range,
    verify_scratch,
    write_scratch_chunk,
)
from agentworks.execution._scratch import (
    begin_scratch as _begin_scratch,
)
from agentworks.execution._scratch_receipt import ScratchOperation, ScratchOwnership, current_receipt_context

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "pread") or not hasattr(os, "pwrite"),
    reason="POSIX descriptor-relative scratch transfer",
)


def _digest(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def begin_scratch(
    parent_fd: int,
    expected_length: int,
    *,
    expires_at: float | None = None,
) -> scratch_module.ScratchReference:
    token = bytes.fromhex(secrets.token_hex(16))
    context = current_receipt_context(ScratchOperation.STAGE)
    return _begin_scratch(parent_fd, expected_length, token, context, expires_at=expires_at)


def _ownership(reference: scratch_module.ScratchReference) -> ScratchOwnership:
    return reference._ownership


def _reference_name(reference: scratch_module.ScratchReference) -> str:
    return scratch_module.scratch_name(_ownership(reference)._token)


def _set_clock(monkeypatch: pytest.MonkeyPatch, monotonic: Callable[[], float]) -> None:
    clock = SimpleNamespace(monotonic=monotonic)
    monkeypatch.setattr(scratch_module, "time", clock)
    monkeypatch.setattr(receipt_module, "time", clock)


def _open_parent(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _scratch_directory(path: Path) -> Path:
    entries = list(path.iterdir())
    assert len(entries) == 1
    return entries[0]


def _failure(function: object, *args: object) -> ScratchTransferError:
    with pytest.raises(ScratchTransferError) as raised:
        function(*args)  # type: ignore[operator]
    return raised.value


def _scratch_cause(error: BaseException) -> ScratchTransferError:
    assert isinstance(error.__cause__, ScratchTransferError)
    return error.__cause__


def test_binary_roundtrip_reopens_each_operation_and_accepts_duplicate_retry(tmp_path: Path) -> None:
    content = bytes(range(256)) * 97
    first = content[: scratch_module._MAX_CHUNK_BYTES]
    second = content[len(first) :]
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    scratch = _scratch_directory(tmp_path)
    data_path = scratch / scratch_module._DATA_NAME
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
    assert stat.S_IMODE(data_path.stat().st_mode) == 0o600
    assert (scratch.stat().st_uid, scratch.stat().st_gid) == (os.geteuid(), os.getegid())
    assert (data_path.stat().st_uid, data_path.stat().st_gid) == (os.geteuid(), os.getegid())
    os.close(parent_fd)

    parent_fd = _open_parent(tmp_path)
    write_scratch_chunk(parent_fd, reference, 0, first, _digest(first))
    before_retry = data_path.stat()
    write_scratch_chunk(parent_fd, reference, 0, first, _digest(first))
    assert data_path.stat().st_mtime_ns == before_retry.st_mtime_ns
    write_scratch_chunk(parent_fd, reference, len(first), second, _digest(second))
    ready = verify_scratch(parent_fd, reference, _digest(content))
    assert isinstance(ready, ReadyScratchReference)
    assert read_scratch_range(parent_fd, ready, 251, 4096) == content[251 : 251 + 4096]
    assert read_scratch_range(parent_fd, ready, len(content), 0) == b""
    cleanup_scratch(parent_fd, ready)
    os.fstat(parent_fd)
    os.close(parent_fd)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("operation", "phase"),
    [
        ("write", ScratchPhase.WRITE),
        ("verify", ScratchPhase.VERIFY),
        ("read", ScratchPhase.READ),
    ],
)
def test_reopened_operation_close_interrupt_attempts_both_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    phase: ScratchPhase,
) -> None:
    content = b"reopened close"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    write_scratch_chunk(parent_fd, reference, 0, content, _digest(content))
    ready = verify_scratch(parent_fd, reference, _digest(content))
    original_close = scratch_module._close_fd
    closed: list[int] = []

    def close_then_interrupt(descriptor: int) -> None:
        original_close(descriptor)
        closed.append(descriptor)
        if len(closed) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(scratch_module, "_close_fd", close_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        if operation == "write":
            write_scratch_chunk(parent_fd, reference, 0, content, _digest(content))
        elif operation == "verify":
            verify_scratch(parent_fd, reference, _digest(content))
        else:
            read_scratch_range(parent_fd, ready, 0, len(content))
    cause = _scratch_cause(raised.value)
    debt = cause.cleanup_debt
    assert cause.kind is ScratchFailureKind.IO
    assert cause.phase is phase
    assert debt is not None
    assert debt._name == _reference_name(reference)
    assert debt._directory == _ownership(reference)._directory
    assert debt._object == _ownership(reference)._data
    assert len(closed) == 2
    for descriptor in closed:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    monkeypatch.setattr(scratch_module, "_close_fd", original_close)
    cleanup_scratch(parent_fd, debt)
    os.close(parent_fd)


def test_begin_preserves_setgid_until_data_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o2700)
    if not tmp_path.stat().st_mode & stat.S_ISGID:
        pytest.skip("filesystem does not retain setgid on the parent directory")
    original_open = os.open
    directory_modes_at_data_creation: list[int] = []

    def observe_data_creation(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == scratch_module._DATA_NAME:
            assert dir_fd is not None
            directory_modes_at_data_creation.append(os.fstat(dir_fd).st_mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", observe_data_creation)

    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 0)
    directory = _scratch_directory(tmp_path)
    data_path = directory / scratch_module._DATA_NAME
    assert len(directory_modes_at_data_creation) == 1
    assert directory_modes_at_data_creation[0] & stat.S_ISGID
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(data_path.stat().st_mode) == 0o600
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_reference_can_be_reopened_in_a_fresh_process(tmp_path: Path) -> None:
    content = b"fresh process\x00binary\xff"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    os.close(parent_fd)
    ownership = _ownership(reference)
    fixture = json.dumps(
        {
            "token": ownership._token.hex(),
            "parent": [ownership._parent.device, ownership._parent.inode],
            "directory": [ownership._directory.device, ownership._directory.inode],
            "data": [ownership._data.device, ownership._data.inode],
            "receipt": [reference._ownership._receipt.device, reference._ownership._receipt.inode],
            "gid": ownership._gid,
            "length": ownership._length,
            "identity": {
                "euid": ownership._context.identity.euid,
                "egid": ownership._context.identity.egid,
                "groups": list(ownership._context.identity.groups),
            },
            "digest": _digest(content).hex(),
            "content": content.hex(),
        }
    ).encode()
    code = """
import hashlib
import json
import os
import sys
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._scratch import ScratchReference, write_scratch_chunk
from agentworks.execution._scratch_receipt import (
    ScratchOperation, ScratchOwnership, ScratchReceiptContext, _Identity,
)
fixture = json.load(sys.stdin)
identity = fixture["identity"]
context = ScratchReceiptContext(
    ScratchOperation.STAGE,
    IdentityExpectation(identity["euid"], identity["egid"], tuple(identity["groups"])),
)
ownership = ScratchOwnership(
    bytes.fromhex(fixture["token"]), context, _Identity(*fixture["parent"]),
    _Identity(*fixture["directory"]), _Identity(*fixture["data"]),
    fixture["gid"], fixture["length"], _Identity(*fixture["receipt"]),
)
reference = ScratchReference(ownership)
content = bytes.fromhex(fixture["content"])
parent_fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    write_scratch_chunk(parent_fd, reference, 0, content, hashlib.sha256(content).digest())
finally:
    os.close(parent_fd)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        input=fixture,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0
    parent_fd = _open_parent(tmp_path)
    ready = verify_scratch(parent_fd, reference, _digest(content))
    assert read_scratch_range(parent_fd, ready, 0, len(content)) == content
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_gap_overlap_and_different_duplicate_refuse_without_writing(tmp_path: Path) -> None:
    content = b"abcdef"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    gap = _failure(write_scratch_chunk, parent_fd, reference, 1, b"b", _digest(b"b"))
    assert gap.kind is ScratchFailureKind.CONFLICT
    write_scratch_chunk(parent_fd, reference, 0, b"abc", _digest(b"abc"))
    duplicate = _failure(write_scratch_chunk, parent_fd, reference, 0, b"abd", _digest(b"abd"))
    overlap = _failure(write_scratch_chunk, parent_fd, reference, 2, b"cde", _digest(b"cde"))
    assert duplicate.kind is ScratchFailureKind.CONFLICT
    assert overlap.kind is ScratchFailureKind.CONFLICT
    assert (_scratch_directory(tmp_path) / scratch_module._DATA_NAME).read_bytes() == b"abc"
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_short_pwrite_completes_the_exact_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"short writes must still complete"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    original_pwrite = os.pwrite

    def short_pwrite(descriptor: int, data: bytes, offset: int) -> int:
        return original_pwrite(descriptor, data[:2], offset)

    monkeypatch.setattr(os, "pwrite", short_pwrite)
    write_scratch_chunk(parent_fd, reference, 0, content, _digest(content))
    ready = verify_scratch(parent_fd, reference, _digest(content))
    assert read_scratch_range(parent_fd, ready, 0, len(content)) == content
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_chunk_digest_and_declared_length_bounds_are_enforced(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 3)
    wrong_digest = _failure(write_scratch_chunk, parent_fd, reference, 0, b"abc", _digest(b"abd"))
    empty = _failure(write_scratch_chunk, parent_fd, reference, 0, b"", _digest(b""))
    beyond = _failure(write_scratch_chunk, parent_fd, reference, 1, b"abc", _digest(b"abc"))
    too_large = b"x" * (scratch_module._MAX_CHUNK_BYTES + 1)
    oversized = _failure(write_scratch_chunk, parent_fd, reference, 0, too_large, _digest(too_large))
    assert wrong_digest.kind is ScratchFailureKind.INTEGRITY
    assert {empty.kind, beyond.kind, oversized.kind} == {ScratchFailureKind.LIMIT}
    assert (_scratch_directory(tmp_path) / scratch_module._DATA_NAME).stat().st_size == 0
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_invalid_length_refuses_before_creating_scratch(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    with pytest.raises(ValueError):
        begin_scratch(parent_fd, -1)
    assert not list(tmp_path.iterdir())
    os.close(parent_fd)


def test_verify_requires_complete_length_and_matching_whole_digest(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    incomplete = begin_scratch(parent_fd, 3)
    length_error = _failure(verify_scratch, parent_fd, incomplete, _digest(b"abc"))
    assert length_error.kind is ScratchFailureKind.INTEGRITY
    assert length_error.cleanup_debt is not None
    cleanup_scratch(parent_fd, incomplete)

    wrong_whole = begin_scratch(parent_fd, 3)
    write_scratch_chunk(parent_fd, wrong_whole, 0, b"abc", _digest(b"abc"))
    digest_error = _failure(verify_scratch, parent_fd, wrong_whole, _digest(b"abd"))
    assert digest_error.kind is ScratchFailureKind.INTEGRITY
    assert digest_error.phase is ScratchPhase.VERIFY
    cleanup_scratch(parent_fd, wrong_whole)
    os.close(parent_fd)


def test_final_digest_is_validated_only_when_transfer_is_verified(tmp_path: Path) -> None:
    content = b"digest learned after transfer"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    write_scratch_chunk(parent_fd, reference, 0, content, _digest(content))

    with pytest.raises(ValueError):
        verify_scratch(parent_fd, reference, b"short")
    digest_error = _failure(verify_scratch, parent_fd, reference, _digest(b"different"))
    assert digest_error.kind is ScratchFailureKind.INTEGRITY

    ready = verify_scratch(parent_fd, reference, _digest(content))
    assert read_scratch_range(parent_fd, ready, 0, len(content)) == content
    cleanup_scratch(parent_fd, ready)
    os.close(parent_fd)


def test_begin_collision_is_one_attempt_and_never_claims_or_removes_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_hex = "00" * 16
    collision = tmp_path / scratch_module.scratch_name(bytes.fromhex(token_hex))
    collision.mkdir(mode=0o700)
    sentinel = collision / "sentinel"
    sentinel.write_bytes(b"not owned")
    parent_fd = _open_parent(tmp_path)
    clock = [0.0]
    original_mkdir = os.mkdir

    def collide_then_expire(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        try:
            original_mkdir(path, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        finally:
            clock[0] = 10.0

    monkeypatch.setattr(secrets, "token_hex", lambda _length: token_hex)
    monkeypatch.setattr(os, "mkdir", collide_then_expire)
    _set_clock(monkeypatch, lambda: clock[0])

    with pytest.raises(ScratchTransferError) as raised:
        begin_scratch(parent_fd, 0, expires_at=5.0)
    assert raised.value.kind is ScratchFailureKind.CONFLICT
    assert raised.value.phase is ScratchPhase.BEGIN
    assert raised.value.cleanup_debt is None
    assert sentinel.read_bytes() == b"not owned"
    os.close(parent_fd)


def test_begin_expiry_after_mkdir_normalizes_for_cleanup_without_creating_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o2700)
    if not tmp_path.stat().st_mode & stat.S_ISGID:
        pytest.skip("filesystem does not retain setgid on the parent directory")
    parent_fd = _open_parent(tmp_path)
    clock = [0.0]
    original_mkdir = os.mkdir
    original_open = os.open
    data_created = False
    acquired_modes: list[int] = []

    def create_then_expire(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        acquired_modes.append(os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode)  # type: ignore[arg-type]
        clock[0] = 10.0

    def observe_data_creation(path: object, *args: object, **kwargs: object) -> int:
        nonlocal data_created
        if path == scratch_module._DATA_NAME:
            data_created = True
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "mkdir", create_then_expire)
    monkeypatch.setattr(os, "open", observe_data_creation)
    _set_clock(monkeypatch, lambda: clock[0])

    previous_umask = os.umask(0o077)
    try:
        with pytest.raises(ScratchTransferError) as raised:
            begin_scratch(parent_fd, 0, expires_at=5.0)
    finally:
        os.umask(previous_umask)
    assert raised.value.kind is ScratchFailureKind.DEADLINE
    assert raised.value.cleanup_debt is None
    assert acquired_modes[0] & stat.S_ISGID
    assert not data_created
    assert not list(tmp_path.iterdir())
    os.close(parent_fd)


def test_begin_expiry_retains_exact_debt_when_cleanup_normalization_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o2700)
    if not tmp_path.stat().st_mode & stat.S_ISGID:
        pytest.skip("filesystem does not retain setgid on the parent directory")
    parent_fd = _open_parent(tmp_path)
    clock = [0.0]
    original_mkdir = os.mkdir
    original_set_mode = scratch_module._set_mode

    def create_then_expire(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        clock[0] = 10.0

    def refuse_cleanup_normalization(descriptor: int, mode: int, phase: ScratchPhase) -> None:
        if clock[0] >= 5.0 and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ScratchTransferError(ScratchFailureKind.IO, phase)
        original_set_mode(descriptor, mode, phase)

    monkeypatch.setattr(os, "mkdir", create_then_expire)
    monkeypatch.setattr(scratch_module, "_set_mode", refuse_cleanup_normalization)
    _set_clock(monkeypatch, lambda: clock[0])

    with pytest.raises(ScratchTransferError) as raised:
        begin_scratch(parent_fd, 0, expires_at=5.0)
    debt = raised.value.cleanup_debt
    assert raised.value.kind is ScratchFailureKind.DEADLINE
    assert debt is not None and debt._directory is not None and debt._object is None
    directory = _scratch_directory(tmp_path)
    assert directory.stat().st_mode & stat.S_ISGID
    assert not list(directory.iterdir())

    directory.chmod(0o700)
    cleanup_scratch(parent_fd, debt)
    os.close(parent_fd)


def test_begin_expiry_during_data_creation_performs_only_cleanup_bookkeeping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    clock = [0.0]
    original_open = os.open
    original_list = scratch_module._list_directory
    data_created = False
    listed_phases: list[ScratchPhase] = []

    def create_data_then_expire(path: object, *args: object, **kwargs: object) -> int:
        nonlocal data_created
        descriptor = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == scratch_module._DATA_NAME:
            data_created = True
            clock[0] = 10.0
        return descriptor

    def record_listing(directory_fd: int, phase: ScratchPhase) -> set[str]:
        listed_phases.append(phase)
        return original_list(directory_fd, phase)

    monkeypatch.setattr(os, "open", create_data_then_expire)
    monkeypatch.setattr(scratch_module, "_list_directory", record_listing)
    _set_clock(monkeypatch, lambda: clock[0])

    with pytest.raises(ScratchTransferError) as raised:
        begin_scratch(parent_fd, 0, expires_at=5.0)
    assert raised.value.kind is ScratchFailureKind.DEADLINE
    assert raised.value.cleanup_debt is None
    assert data_created
    assert ScratchPhase.BEGIN not in listed_phases
    assert not list(tmp_path.iterdir())
    os.close(parent_fd)


def test_short_pwrite_loop_stops_at_deadline_with_exact_cleanup_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"partial write"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    clock = [0.0]
    original_pwrite = os.pwrite

    def write_once_then_expire(descriptor: int, data: bytes, offset: int) -> int:
        written = original_pwrite(descriptor, data[:1], offset)
        clock[0] = 10.0
        return written

    monkeypatch.setattr(os, "pwrite", write_once_then_expire)
    _set_clock(monkeypatch, lambda: clock[0])

    with pytest.raises(ScratchTransferError) as raised:
        write_scratch_chunk(
            parent_fd,
            reference,
            0,
            content,
            _digest(content),
            expires_at=5.0,
        )
    assert raised.value.kind is ScratchFailureKind.DEADLINE
    assert raised.value.phase is ScratchPhase.WRITE
    assert raised.value.cleanup_debt == scratch_module._cleanup_debt(reference)
    assert (_scratch_directory(tmp_path) / scratch_module._DATA_NAME).read_bytes() == content[:1]
    cleanup_scratch(parent_fd, raised.value.cleanup_debt)
    os.close(parent_fd)


def test_verify_hash_loop_stops_between_bounded_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = os.urandom(scratch_module._HASH_READ_BYTES + 1)
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    for offset in range(0, len(content), scratch_module._MAX_CHUNK_BYTES):
        chunk = content[offset : offset + scratch_module._MAX_CHUNK_BYTES]
        write_scratch_chunk(parent_fd, reference, offset, chunk, _digest(chunk))
    clock = [0.0]
    original_pread_exact = scratch_module._pread_exact
    reads = 0

    def read_once_then_expire(
        descriptor: int,
        offset: int,
        length: int,
        phase: ScratchPhase,
        expires_at: float | None,
    ) -> bytes:
        nonlocal reads
        block = original_pread_exact(descriptor, offset, length, phase, expires_at)
        reads += 1
        clock[0] = 10.0
        return block

    monkeypatch.setattr(scratch_module, "_pread_exact", read_once_then_expire)
    _set_clock(monkeypatch, lambda: clock[0])

    with pytest.raises(ScratchTransferError) as raised:
        verify_scratch(parent_fd, reference, _digest(content), expires_at=5.0)
    assert raised.value.kind is ScratchFailureKind.DEADLINE
    assert raised.value.phase is ScratchPhase.VERIFY
    assert reads == 1
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_read_checks_deadline_after_its_last_pread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"last read deadline"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    write_scratch_chunk(parent_fd, reference, 0, content, _digest(content))
    ready = verify_scratch(parent_fd, reference, _digest(content))
    clock = [0.0]
    original_pread = os.pread

    def read_then_expire(descriptor: int, length: int, offset: int) -> bytes:
        block = original_pread(descriptor, length, offset)
        clock[0] = 10.0
        return block

    monkeypatch.setattr(os, "pread", read_then_expire)
    _set_clock(monkeypatch, lambda: clock[0])

    with pytest.raises(ScratchTransferError) as raised:
        read_scratch_range(parent_fd, ready, 0, len(content), expires_at=5.0)
    assert raised.value.kind is ScratchFailureKind.DEADLINE
    assert raised.value.phase is ScratchPhase.READ
    assert raised.value.cleanup_debt == scratch_module._cleanup_debt(reference)
    cleanup_scratch(parent_fd, ready)
    os.close(parent_fd)


def test_iteration_checks_deadline_after_the_last_yield(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"one range"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    write_scratch_chunk(parent_fd, reference, 0, content, _digest(content))
    ready = verify_scratch(parent_fd, reference, _digest(content))
    clock = [0.0]
    _set_clock(monkeypatch, lambda: clock[0])
    chunks = iter_ready_scratch(parent_fd, ready, expires_at=5.0)

    assert next(chunks) == content
    clock[0] = 10.0
    with pytest.raises(ScratchTransferError) as raised:
        next(chunks)
    assert raised.value.kind is ScratchFailureKind.DEADLINE
    assert raised.value.cleanup_debt == scratch_module._cleanup_debt(reference)
    cleanup_scratch(parent_fd, ready)
    os.close(parent_fd)


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "directory"])
def test_observed_special_object_is_refused_and_never_cleaned(
    tmp_path: Path,
    replacement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 1)
    directory = _scratch_directory(tmp_path)
    data_path = directory / scratch_module._DATA_NAME
    saved = directory / "saved"
    data_path.rename(saved)
    if replacement == "symlink":
        data_path.symlink_to(saved)
    elif replacement == "fifo":
        os.mkfifo(data_path)
    else:
        data_path.mkdir()
    original_open = os.open
    writable_object_opened = False

    def recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal writable_object_opened
        if path == scratch_module._DATA_NAME and flags & os.O_RDWR:
            writable_object_opened = True
        return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", recording_open)
    error = _failure(write_scratch_chunk, parent_fd, reference, 0, b"x", _digest(b"x"))
    cleanup_error = _failure(cleanup_scratch, parent_fd, reference)
    assert error.kind is ScratchFailureKind.UNSUPPORTED
    assert cleanup_error.cleanup_debt is not None
    assert not writable_object_opened
    assert data_path.exists() or data_path.is_symlink()
    assert saved.read_bytes() == b""
    if replacement in {"symlink", "fifo"}:
        data_path.unlink()
    else:
        data_path.rmdir()
    saved.rename(data_path)
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_hard_link_and_mode_changes_are_refused(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    hardlinked = begin_scratch(parent_fd, 1)
    directory = _scratch_directory(tmp_path)
    data_path = directory / scratch_module._DATA_NAME
    other = directory / "other"
    os.link(data_path, other)
    link_error = _failure(write_scratch_chunk, parent_fd, hardlinked, 0, b"x", _digest(b"x"))
    cleanup_error = _failure(cleanup_scratch, parent_fd, hardlinked)
    assert link_error.kind is ScratchFailureKind.UNSUPPORTED
    assert cleanup_error.kind is ScratchFailureKind.CONFLICT
    assert data_path.exists() and other.exists()
    other.unlink()
    cleanup_scratch(parent_fd, hardlinked)

    changed_mode = begin_scratch(parent_fd, 1)
    directory = _scratch_directory(tmp_path)
    data_path = directory / scratch_module._DATA_NAME
    data_path.chmod(0o640)
    mode_error = _failure(write_scratch_chunk, parent_fd, changed_mode, 0, b"x", _digest(b"x"))
    assert mode_error.kind is ScratchFailureKind.CONFLICT
    data_path.chmod(0o600)
    directory.chmod(0o750)
    directory_error = _failure(write_scratch_chunk, parent_fd, changed_mode, 0, b"x", _digest(b"x"))
    assert directory_error.kind is ScratchFailureKind.CONFLICT
    directory.chmod(0o700)
    cleanup_scratch(parent_fd, changed_mode)
    os.close(parent_fd)


def test_regular_object_substitution_is_refused_without_deleting_impostor(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 1)
    directory = _scratch_directory(tmp_path)
    data_path = directory / scratch_module._DATA_NAME
    saved = directory / "saved"
    data_path.rename(saved)
    data_path.write_bytes(b"impostor")
    data_path.chmod(0o600)
    error = _failure(write_scratch_chunk, parent_fd, reference, 0, b"x", _digest(b"x"))
    cleanup_error = _failure(cleanup_scratch, parent_fd, reference)
    assert error.kind is ScratchFailureKind.CONFLICT
    assert cleanup_error.kind is ScratchFailureKind.CONFLICT
    assert data_path.read_bytes() == b"impostor"
    data_path.unlink()
    saved.rename(data_path)
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_directory_substitution_is_refused_without_deleting_impostor(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 1)
    directory = _scratch_directory(tmp_path)
    saved = tmp_path / "saved"
    directory.rename(saved)
    directory.mkdir(mode=0o700)
    impostor = directory / scratch_module._DATA_NAME
    impostor.write_bytes(b"impostor")
    impostor.chmod(0o600)
    error = _failure(write_scratch_chunk, parent_fd, reference, 0, b"x", _digest(b"x"))
    cleanup_error = _failure(cleanup_scratch, parent_fd, reference)
    assert error.kind is ScratchFailureKind.CONFLICT
    assert cleanup_error.kind is ScratchFailureKind.CONFLICT
    assert impostor.read_bytes() == b"impostor"
    impostor.unlink()
    directory.rmdir()
    saved.rename(directory)
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_cleanup_refuses_unknown_entries_and_is_idempotent(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 0)
    directory = _scratch_directory(tmp_path)
    unknown = directory / "unknown"
    unknown.write_bytes(b"leave me")
    error = _failure(cleanup_scratch, parent_fd, reference)
    assert error.kind is ScratchFailureKind.CONFLICT
    assert unknown.read_bytes() == b"leave me"
    assert (directory / scratch_module._DATA_NAME).exists()
    unknown.unlink()
    cleanup_scratch(parent_fd, reference)
    cleanup_scratch(parent_fd, reference)
    os.fstat(parent_fd)
    os.close(parent_fd)


def test_ready_read_refuses_changed_object_and_out_of_range_request(tmp_path: Path) -> None:
    content = b"verified"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content))
    write_scratch_chunk(parent_fd, reference, 0, content, _digest(content))
    ready = verify_scratch(parent_fd, reference, _digest(content))
    range_error = _failure(read_scratch_range, parent_fd, ready, len(content), 1)
    assert range_error.kind is ScratchFailureKind.LIMIT
    data_path = _scratch_directory(tmp_path) / scratch_module._DATA_NAME
    data_path.write_bytes(b"changed!")
    data_path.chmod(0o600)
    os.utime(data_path, ns=(ready._modified_ns + 1_000_000_000, ready._modified_ns + 1_000_000_000))
    changed = _failure(read_scratch_range, parent_fd, ready, 0, len(content))
    assert changed.kind is ScratchFailureKind.CONFLICT
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_begin_identity_failure_retains_unknown_object_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_fstat = os.fstat

    def fail_regular_identity(descriptor: int) -> os.stat_result:
        observed = original_fstat(descriptor)
        if stat.S_ISREG(observed.st_mode):
            raise OSError(errno.EIO, "fixture identity failure")
        return observed

    monkeypatch.setattr(os, "fstat", fail_regular_identity)
    error = _failure(begin_scratch, parent_fd, 0)
    debt = error.cleanup_debt
    assert error.kind is ScratchFailureKind.IO
    assert debt is not None and debt._directory is not None and debt._object is None
    monkeypatch.setattr(os, "fstat", original_fstat)
    cleanup_error = _failure(cleanup_scratch, parent_fd, debt)
    assert cleanup_error.kind is ScratchFailureKind.CONFLICT
    directory = _scratch_directory(tmp_path)
    (directory / scratch_module._DATA_NAME).unlink()
    directory.rmdir()
    os.close(parent_fd)


def test_begin_interrupt_after_object_acquisition_closes_fd_and_retains_unknown_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_fstat = os.fstat
    object_fd: int | None = None

    def interrupt_regular_identity(descriptor: int) -> os.stat_result:
        nonlocal object_fd
        observed = original_fstat(descriptor)
        if stat.S_ISREG(observed.st_mode):
            object_fd = descriptor
            raise KeyboardInterrupt
        return observed

    monkeypatch.setattr(os, "fstat", interrupt_regular_identity)
    with pytest.raises(KeyboardInterrupt) as raised:
        begin_scratch(parent_fd, 0)
    cause = _scratch_cause(raised.value)
    debt = cause.cleanup_debt
    assert cause.kind is ScratchFailureKind.IO
    assert cause.phase is ScratchPhase.BEGIN
    assert debt is not None and debt._directory is not None and debt._object is None
    monkeypatch.setattr(os, "fstat", original_fstat)
    assert object_fd is not None
    with pytest.raises(OSError):
        os.fstat(object_fd)
    directory = _scratch_directory(tmp_path)
    assert (directory / scratch_module._DATA_NAME).exists()
    cleanup_error = _failure(cleanup_scratch, parent_fd, debt)
    assert cleanup_error.kind is ScratchFailureKind.CONFLICT
    (directory / scratch_module._DATA_NAME).unlink()
    directory.rmdir()
    os.close(parent_fd)


def test_begin_fstat_failure_then_close_interrupt_retains_unknown_object_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_fstat = os.fstat
    original_close = scratch_module._close_fd
    closed: list[int] = []

    def fail_regular_identity(descriptor: int) -> os.stat_result:
        observed = original_fstat(descriptor)
        if stat.S_ISREG(observed.st_mode):
            raise OSError(errno.EIO, "fixture identity failure")
        return observed

    def close_then_interrupt(descriptor: int) -> None:
        original_close(descriptor)
        closed.append(descriptor)
        if len(closed) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "fstat", fail_regular_identity)
    monkeypatch.setattr(scratch_module, "_close_fd", close_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        begin_scratch(parent_fd, 0)
    cause = _scratch_cause(raised.value)
    debt = cause.cleanup_debt
    assert cause.kind is ScratchFailureKind.IO
    assert cause.phase is ScratchPhase.BEGIN
    assert debt is not None and debt._directory is not None and debt._object is None
    assert len(closed) == 2
    monkeypatch.setattr(os, "fstat", original_fstat)
    for descriptor in closed:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    directory = _scratch_directory(tmp_path)
    assert (directory / scratch_module._DATA_NAME).exists()
    cleanup_error = _failure(cleanup_scratch, parent_fd, debt)
    assert cleanup_error.kind is ScratchFailureKind.CONFLICT
    (directory / scratch_module._DATA_NAME).unlink()
    directory.rmdir()
    os.close(parent_fd)


def test_begin_preserves_identified_debt_and_original_control_when_cleanup_interrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_set_mode = scratch_module._set_mode
    original_cleanup = scratch_module._cleanup_once
    descriptors: list[int] = []
    cleanup_calls = 0

    def interrupt_after_identity(descriptor: int, mode: int, phase: ScratchPhase) -> None:
        descriptors.append(descriptor)
        if mode == scratch_module._OBJECT_MODE:
            raise SystemExit(23)
        original_set_mode(descriptor, mode, phase)

    def interrupt_cleanup(*_args: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(scratch_module, "_set_mode", interrupt_after_identity)
    monkeypatch.setattr(scratch_module, "_cleanup_once", interrupt_cleanup)
    with pytest.raises(SystemExit, match="23") as raised:
        begin_scratch(parent_fd, 0)
    cause = _scratch_cause(raised.value)
    debt = cause.cleanup_debt
    assert cause.kind is ScratchFailureKind.IO
    assert cause.phase is ScratchPhase.BEGIN
    assert debt is not None and debt._directory is not None and debt._object is not None
    assert cleanup_calls == 1
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert (_scratch_directory(tmp_path) / scratch_module._DATA_NAME).exists()
    monkeypatch.setattr(scratch_module, "_cleanup_once", original_cleanup)
    cleanup_scratch(parent_fd, debt)
    os.close(parent_fd)


def test_begin_close_interrupt_retains_complete_debt_and_closes_both_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_close = scratch_module._close_fd
    original_unlink = os.unlink
    closed: list[int] = []

    def close_then_interrupt(descriptor: int) -> None:
        original_close(descriptor)
        closed.append(descriptor)
        if len(closed) == 1:
            raise KeyboardInterrupt

    def deny_data_unlink(path: object, *args: object, **kwargs: object) -> None:
        if path == scratch_module._DATA_NAME:
            raise OSError(errno.EIO, "fixture cleanup failure")
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scratch_module, "_close_fd", close_then_interrupt)
    monkeypatch.setattr(os, "unlink", deny_data_unlink)
    with pytest.raises(KeyboardInterrupt) as raised:
        begin_scratch(parent_fd, 0)
    cause = _scratch_cause(raised.value)
    debt = cause.cleanup_debt
    assert debt is not None and debt._directory is not None and debt._object is not None
    assert len(closed) == 2
    for descriptor in closed:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert (_scratch_directory(tmp_path) / scratch_module._DATA_NAME).exists()
    monkeypatch.setattr(os, "unlink", original_unlink)
    cleanup_scratch(parent_fd, debt)
    os.close(parent_fd)


def test_begin_cleanup_interrupt_preserves_complete_debt_as_closed_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_list = scratch_module._list_directory
    original_cleanup = scratch_module._cleanup_once

    def fail_begin_listing(directory_fd: int, phase: ScratchPhase) -> set[str]:
        if phase is ScratchPhase.BEGIN:
            raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.BEGIN)
        return original_list(directory_fd, phase)

    monkeypatch.setattr(scratch_module, "_list_directory", fail_begin_listing)
    monkeypatch.setattr(scratch_module, "_cleanup_once", lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt) as raised:
        begin_scratch(parent_fd, 0)
    cause = _scratch_cause(raised.value)
    debt = cause.cleanup_debt
    assert cause.kind is ScratchFailureKind.CONFLICT
    assert cause.phase is ScratchPhase.BEGIN
    assert debt is not None and debt._directory is not None and debt._object is not None
    assert (_scratch_directory(tmp_path) / scratch_module._DATA_NAME).exists()
    monkeypatch.setattr(scratch_module, "_cleanup_once", original_cleanup)
    cleanup_scratch(parent_fd, debt)
    os.close(parent_fd)


def test_interrupted_candidate_collision_is_never_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_hex = "00" * 16
    candidate = tmp_path / scratch_module.scratch_name(bytes.fromhex(token_hex))
    candidate.mkdir(mode=0o700)
    sentinel = candidate / "sentinel"
    sentinel.write_bytes(b"not owned")
    parent_fd = _open_parent(tmp_path)
    original_mkdir = os.mkdir

    def interrupt_candidate_mkdir(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == candidate.name:
            raise KeyboardInterrupt
        original_mkdir(path, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets, "token_hex", lambda _length: token_hex)
    monkeypatch.setattr(os, "mkdir", interrupt_candidate_mkdir)
    with pytest.raises(KeyboardInterrupt) as raised:
        begin_scratch(parent_fd, 0)
    assert raised.value.__cause__ is None
    assert sentinel.read_bytes() == b"not owned"
    os.close(parent_fd)
    sentinel.unlink()
    candidate.rmdir()


def test_cleanup_stat_error_is_not_mistaken_for_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 0)
    original_stat = os.stat

    def fail_scratch_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == _reference_name(reference):
            raise OSError(errno.EIO, "fixture cleanup observation failure")
        return original_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", fail_scratch_stat)
    error = _failure(cleanup_scratch, parent_fd, reference)
    assert error.kind is ScratchFailureKind.IO
    assert _scratch_directory(tmp_path).exists()
    monkeypatch.setattr(os, "stat", original_stat)
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_cleanup_unlink_failure_retains_exact_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 0)
    original_unlink = os.unlink

    def fail_data_unlink(path: object, *args: object, **kwargs: object) -> None:
        if path == scratch_module._DATA_NAME:
            raise OSError(errno.EIO, "fixture cleanup failure")
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", fail_data_unlink)
    error = _failure(cleanup_scratch, parent_fd, reference)
    assert error.kind is ScratchFailureKind.IO
    assert error.cleanup_debt is not None
    assert (_scratch_directory(tmp_path) / scratch_module._DATA_NAME).exists()
    monkeypatch.setattr(os, "unlink", original_unlink)
    cleanup_scratch(parent_fd, error.cleanup_debt)
    os.close(parent_fd)


def test_errors_and_references_hide_names_bytes_and_digests(tmp_path: Path) -> None:
    secret = b"private-transfer-bytes"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(secret))
    error = _failure(write_scratch_chunk, parent_fd, reference, 1, secret[:1], _digest(secret[:1]))
    assert error.args == (ScratchFailureKind.CONFLICT.value, ScratchPhase.WRITE.value, True)
    name = _reference_name(reference)
    assert name not in repr(reference)
    assert name not in repr(error)
    assert secret.decode() not in repr(error)
    assert _digest(secret).hex() not in repr(error)
    assert error.cleanup_debt is not None
    assert name not in repr(error.cleanup_debt)
    assert error.__cause__ is None
    assert error.__context__ is None
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)
