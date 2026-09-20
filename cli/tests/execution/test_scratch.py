"""Private descriptor-relative scratch transfer behavior."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import agentworks.execution._scratch as scratch_module
from agentworks.execution._scratch import (
    ReadyScratchReference,
    ScratchFailureKind,
    ScratchPhase,
    ScratchTransferError,
    begin_scratch,
    cleanup_scratch,
    read_scratch_range,
    verify_scratch,
    write_scratch_chunk,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "pread") or not hasattr(os, "pwrite"),
    reason="POSIX descriptor-relative scratch transfer",
)


def _digest(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


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


def test_binary_roundtrip_reopens_each_operation_and_accepts_duplicate_retry(tmp_path: Path) -> None:
    content = bytes(range(256)) * 97
    first = content[: scratch_module._MAX_CHUNK_BYTES]
    second = content[len(first) :]
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content), _digest(content))
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
    ready = verify_scratch(parent_fd, reference)
    assert isinstance(ready, ReadyScratchReference)
    assert read_scratch_range(parent_fd, ready, 251, 4096) == content[251 : 251 + 4096]
    assert read_scratch_range(parent_fd, ready, len(content), 0) == b""
    cleanup_scratch(parent_fd, ready)
    os.fstat(parent_fd)
    os.close(parent_fd)
    assert not list(tmp_path.iterdir())


def test_reference_can_be_reopened_in_a_fresh_process(tmp_path: Path) -> None:
    content = b"fresh process\x00binary\xff"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content), _digest(content))
    os.close(parent_fd)
    fixture = json.dumps(
        {
            "name": reference._name,
            "directory": [reference._directory.device, reference._directory.inode],
            "object": [reference._object.device, reference._object.inode],
            "uid": reference._uid,
            "gid": reference._gid,
            "length": reference._length,
            "digest": reference._digest.hex(),
            "content": content.hex(),
        }
    ).encode()
    code = """
import hashlib
import json
import os
import sys
from agentworks.execution._scratch import ScratchReference, _Identity, write_scratch_chunk
fixture = json.load(sys.stdin)
reference = ScratchReference(
    fixture["name"], _Identity(*fixture["directory"]), _Identity(*fixture["object"]),
    fixture["uid"], fixture["gid"], fixture["length"], bytes.fromhex(fixture["digest"]),
)
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
    ready = verify_scratch(parent_fd, reference)
    assert read_scratch_range(parent_fd, ready, 0, len(content)) == content
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_gap_overlap_and_different_duplicate_refuse_without_writing(tmp_path: Path) -> None:
    content = b"abcdef"
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, len(content), _digest(content))
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
    reference = begin_scratch(parent_fd, len(content), _digest(content))
    original_pwrite = os.pwrite

    def short_pwrite(descriptor: int, data: bytes, offset: int) -> int:
        return original_pwrite(descriptor, data[:2], offset)

    monkeypatch.setattr(os, "pwrite", short_pwrite)
    write_scratch_chunk(parent_fd, reference, 0, content, _digest(content))
    ready = verify_scratch(parent_fd, reference)
    assert read_scratch_range(parent_fd, ready, 0, len(content)) == content
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)


def test_chunk_digest_and_declared_length_bounds_are_enforced(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 3, _digest(b"abc"))
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


def test_invalid_whole_contract_refuses_before_creating_scratch(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    with pytest.raises(ValueError):
        begin_scratch(parent_fd, -1, _digest(b""))
    with pytest.raises(ValueError):
        begin_scratch(parent_fd, 0, b"short")
    assert not list(tmp_path.iterdir())
    os.close(parent_fd)


def test_verify_requires_complete_length_and_matching_whole_digest(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    incomplete = begin_scratch(parent_fd, 3, _digest(b"abc"))
    length_error = _failure(verify_scratch, parent_fd, incomplete)
    assert length_error.kind is ScratchFailureKind.INTEGRITY
    assert length_error.cleanup_debt is not None
    cleanup_scratch(parent_fd, incomplete)

    wrong_whole = begin_scratch(parent_fd, 3, _digest(b"abd"))
    write_scratch_chunk(parent_fd, wrong_whole, 0, b"abc", _digest(b"abc"))
    digest_error = _failure(verify_scratch, parent_fd, wrong_whole)
    assert digest_error.kind is ScratchFailureKind.INTEGRITY
    assert digest_error.phase is ScratchPhase.VERIFY
    cleanup_scratch(parent_fd, wrong_whole)
    os.close(parent_fd)


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "directory"])
def test_observed_special_object_is_refused_and_never_cleaned(
    tmp_path: Path,
    replacement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 1, _digest(b"x"))
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
    hardlinked = begin_scratch(parent_fd, 1, _digest(b"x"))
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

    changed_mode = begin_scratch(parent_fd, 1, _digest(b"x"))
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
    reference = begin_scratch(parent_fd, 1, _digest(b"x"))
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
    reference = begin_scratch(parent_fd, 1, _digest(b"x"))
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
    reference = begin_scratch(parent_fd, 0, _digest(b""))
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
    reference = begin_scratch(parent_fd, len(content), _digest(content))
    write_scratch_chunk(parent_fd, reference, 0, content, _digest(content))
    ready = verify_scratch(parent_fd, reference)
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
    error = _failure(begin_scratch, parent_fd, 0, _digest(b""))
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


def test_cleanup_stat_error_is_not_mistaken_for_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_parent(tmp_path)
    reference = begin_scratch(parent_fd, 0, _digest(b""))
    original_stat = os.stat

    def fail_scratch_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == reference._name:
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
    reference = begin_scratch(parent_fd, 0, _digest(b""))
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
    reference = begin_scratch(parent_fd, len(secret), _digest(secret))
    error = _failure(write_scratch_chunk, parent_fd, reference, 1, secret[:1], _digest(secret[:1]))
    assert error.args == (ScratchFailureKind.CONFLICT.value, ScratchPhase.WRITE.value, True)
    assert reference._name not in repr(reference)
    assert reference._name not in repr(error)
    assert secret.decode() not in repr(error)
    assert _digest(secret).hex() not in repr(error)
    assert error.cleanup_debt is not None
    assert reference._name not in repr(error.cleanup_debt)
    assert error.__cause__ is None
    assert error.__context__ is None
    cleanup_scratch(parent_fd, reference)
    os.close(parent_fd)
