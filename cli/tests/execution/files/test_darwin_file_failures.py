"""Darwin descriptor and publication fault boundaries with native inspection emulated."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import time
from pathlib import Path

import pytest

from agentworks.execution import _darwin_descriptors as darwin
from agentworks.execution import _darwin_metadata as darwin_metadata
from agentworks.execution import _file_metadata as metadata
from agentworks.execution import _file_paths as paths
from agentworks.execution import _file_publication as publication
from agentworks.execution._file_publication import (
    CreateMetadata,
    FilePublicationError,
    PublicationFailureKind,
    PublicationPhase,
    Replace,
    ScratchFileSource,
    publish_file,
)
from agentworks.execution._publication_receipt import PublicationStageCleanupDebt, cleanup_publication_stage
from agentworks.execution._scratch import cleanup_scratch
from tests.execution.files._publication_test_support import ready_scratch
from tests.execution.files.test_darwin_file_substrate import darwin_inspection as darwin_inspection
from tests.execution.files.test_darwin_file_substrate import parent as parent

_NATIVE_ACL_INSPECTION = darwin.extended_acl_tags
_NATIVE_XATTR_INSPECTION = darwin_metadata.has_xattrs


@pytest.mark.parametrize("inspect", [_NATIVE_ACL_INSPECTION, _NATIVE_XATTR_INSPECTION])
def test_unavailable_native_python_module_is_a_closed_refusal(inspect, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "ctypes", None)
    with pytest.raises(darwin.DarwinMetadataError):
        inspect(42)
    assert sys.modules["ctypes"] is None


def _metadata() -> CreateMetadata:
    return CreateMetadata(os.geteuid(), os.getegid(), 0o640)


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "fstat"), KeyboardInterrupt()])
def test_walk_failure_closes_current_and_previous_handles(tmp_path: Path, monkeypatch, failure) -> None:
    real_open, real_fstat = os.open, os.fstat
    opened: list[int] = []

    def track_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_fstat(descriptor):
        if len(opened) >= 2 and descriptor == opened[-1]:
            raise failure
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {os.open})
    monkeypatch.setattr(os, "fstat", fail_fstat)
    with pytest.raises(paths.ConfinedOpenError if isinstance(failure, OSError) else KeyboardInterrupt):
        paths.open_file_root(str(tmp_path))
    assert len(opened) == 2
    for descriptor in opened:
        with pytest.raises(OSError) as raised:
            real_fstat(descriptor)
        assert raised.value.errno == errno.EBADF


def test_private_stage_acl_is_admitted_before_any_payload_write(tmp_path: Path, parent: int, monkeypatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    inode = target.stat().st_ino
    monkeypatch.setattr(darwin, "extended_acl_tags", lambda fd: () if os.fstat(fd).st_ino == inode else (1,))
    monkeypatch.setattr(publication, "_write", lambda *args: pytest.fail("unadmitted stage received payload bytes"))
    with pytest.raises(FilePublicationError) as raised:
        publish_file(parent, "target", b"private bytes", condition=Replace(), create_metadata=_metadata())
    assert raised.value.kind is PublicationFailureKind.METADATA
    assert target.read_bytes() == b"old" and list(tmp_path.iterdir()) == [target]


def test_control_cause_after_target_mutation_is_always_uncertain(tmp_path: Path, parent: int, monkeypatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    inode = target.stat().st_ino
    real_write = os.write

    def interrupted_write(fd, data):
        written = real_write(fd, data)
        if os.fstat(fd).st_ino == inode:
            raise KeyboardInterrupt from FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONTENT)
        return written

    monkeypatch.setattr(os, "write", interrupted_write)
    with pytest.raises(KeyboardInterrupt) as raised:
        publish_file(parent, "target", b"new", condition=Replace(), create_metadata=_metadata())
    assert isinstance(raised.value.__cause__, FilePublicationError)
    assert raised.value.__cause__.kind is PublicationFailureKind.UNCERTAIN
    assert target.read_bytes() == b"new" and list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "failure,kind",
    [(TimeoutError(), PublicationFailureKind.DEADLINE), (OSError(errno.EIO, "read"), PublicationFailureKind.IO)],
)
def test_stage_verification_fault_is_classified_before_target_effect(
    tmp_path: Path, parent: int, monkeypatch, failure, kind
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    inode = target.stat().st_ino
    real_pread = os.pread

    def fail_stage_read(fd, size, offset):
        if os.fstat(fd).st_ino != inode:
            raise failure
        return real_pread(fd, size, offset)

    monkeypatch.setattr(os, "pread", fail_stage_read)
    with pytest.raises(FilePublicationError) as raised:
        publish_file(parent, "target", b"new", condition=Replace(), create_metadata=_metadata())
    assert raised.value.kind is kind and raised.value.phase is PublicationPhase.CONTENT
    assert target.read_bytes() == b"old" and list(tmp_path.iterdir()) == [target]


def test_deadline_after_first_write_keeps_uncertainty(tmp_path: Path, parent: int, monkeypatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    inode = target.stat().st_ino
    clock = [time.monotonic()]
    expires_at = clock[0] + 30
    real_write = os.write

    def expire_write(fd, data):
        written = real_write(fd, data)
        if os.fstat(fd).st_ino == inode:
            clock[0] = expires_at + 1
        return written

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(os, "write", expire_write)
    with pytest.raises(FilePublicationError) as raised:
        publish_file(parent, "target", b"new", condition=Replace(), create_metadata=_metadata(), expires_at=expires_at)
    assert raised.value.kind is PublicationFailureKind.UNCERTAIN and target.read_bytes() == b"new"


def test_record_only_cleanup_progress_is_retained(tmp_path: Path, parent: int, monkeypatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    ready = ready_scratch(parent, bytes(range(16)), b"new")
    real_unlink = os.unlink

    def fail_record(path, *args, **kwargs):
        if path == "publication-receipt":
            raise OSError(errno.EIO, "record cleanup")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_record)
        with pytest.raises(FilePublicationError) as raised:
            publish_file(
                parent, "target", ScratchFileSource(parent, ready), condition=Replace(), create_metadata=_metadata()
            )
    debt = raised.value.cleanup_debt
    assert raised.value.kind is PublicationFailureKind.UNCERTAIN
    assert isinstance(debt, PublicationStageCleanupDebt) and debt._stage_removed
    assert not (tmp_path / debt.name).exists()
    cleanup_publication_stage(parent, parent, debt)
    cleanup_scratch(parent, ready)
    assert list(tmp_path.iterdir()) == [target]


def test_scratch_backed_close_failure_does_not_invent_stage_debt(tmp_path: Path, parent: int, monkeypatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    inode = target.stat().st_ino
    ready = ready_scratch(parent, bytes(range(16)), b"new")
    real_close = os.close
    retained: list[int] = []

    def fail_target_close(fd):
        if os.fstat(fd).st_ino == inode:
            retained.append(fd)
            raise OSError(errno.EIO, "ambiguous close")
        return real_close(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "close", fail_target_close)
        with pytest.raises(FilePublicationError) as raised:
            publish_file(
                parent, "target", ScratchFileSource(parent, ready), condition=Replace(), create_metadata=_metadata()
            )
    assert raised.value.kind is PublicationFailureKind.UNCERTAIN and raised.value.cleanup_debt is None
    for descriptor in retained:
        real_close(descriptor)
    cleanup_scratch(parent, ready)
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("mode", [0o2770, 0o1700, 0o3770])
def test_explicit_directory_special_bits_remain_supported(tmp_path: Path, parent: int, mode: int) -> None:
    result = metadata.create_directory(
        parent, "directory", uid=os.geteuid(), gid=os.getegid(), mode=mode, expires_at=None
    )
    assert result.changed and result.revision.stat.mode & 0o7777 == mode


def test_missing_exclusive_rename_api_refuses_without_effect(tmp_path: Path, parent: int, monkeypatch) -> None:
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: object())
    with pytest.raises(FilePublicationError) as raised:
        publish_file(parent, "target", b"new", condition=publication.Create(), create_metadata=_metadata())
    assert raised.value.kind is PublicationFailureKind.UNSUPPORTED and not list(tmp_path.iterdir())
