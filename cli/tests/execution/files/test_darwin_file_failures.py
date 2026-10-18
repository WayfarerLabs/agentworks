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
from agentworks.execution._publication_receipt import (
    PublicationStageAdmission,
    PublicationStageCleanupDebt,
    cleanup_publication_stage,
)
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


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_scratch_cleanup_close_control_retains_publication_fact_without_prior(
    tmp_path: Path,
    parent: int,
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    ready = ready_scratch(parent, bytes(range(16)), b"new")
    scratch_inode = next(path for path in tmp_path.iterdir() if path.is_dir()).stat().st_ino
    interrupt = control_type()
    real_unlink, real_close = os.unlink, os.close
    record_removed = False
    interrupted: list[int] = []

    def remove_artifact(path: str, *args, **kwargs) -> None:
        nonlocal record_removed
        real_unlink(path, *args, **kwargs)
        if path == "publication-receipt":
            record_removed = True

    def interrupt_initial_cleanup_close(descriptor: int) -> None:
        is_scratch = os.fstat(descriptor).st_ino == scratch_inode
        real_close(descriptor)
        if is_scratch and record_removed and not interrupted:
            interrupted.append(descriptor)
            raise interrupt

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", remove_artifact)
        patch.setattr(os, "close", interrupt_initial_cleanup_close)
        with pytest.raises(BaseException) as raised:
            publish_file(
                parent, "target", ScratchFileSource(parent, ready), condition=Replace(), create_metadata=_metadata()
            )
    assert raised.value is interrupt and len(interrupted) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(interrupted[0])
    assert closed.value.errno == errno.EBADF
    assert target.read_bytes() == b"new"
    assert not list(tmp_path.glob(".agentworks-stage-*"))
    assert not list(tmp_path.rglob("publication-receipt"))
    fact = raised.value.__cause__
    assert isinstance(fact, FilePublicationError)
    assert fact.kind is PublicationFailureKind.UNCERTAIN and fact.phase is PublicationPhase.PUBLICATION
    assert fact.cleanup_debt is None
    cleanup_scratch(parent, ready)
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "first,second,second_control",
    [
        ("target", "stage", False),
        ("stage", "target", False),
        ("stage", "admission", False),
        ("target", "stage", True),
        ("target", "admission", True),
    ],
)
@pytest.mark.parametrize("remaining", ["none", "stage", "record"])
def test_mixed_close_failures_preserve_first_control_and_exact_cleanup(
    tmp_path: Path,
    parent: int,
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
    first: str,
    second: str,
    second_control: bool,
    remaining: str,
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    target_inode = target.stat().st_ino
    ready = ready_scratch(parent, bytes(range(16)), b"new")
    interrupt = control_type()
    later = SystemExit() if control_type is KeyboardInterrupt else KeyboardInterrupt()
    failures = {first: interrupt, second: later if second_control else OSError(errno.EIO, "close")}
    real_create, real_close, real_unlink = publication._create_stage, os.close, os.unlink
    real_admission_close = PublicationStageAdmission.close
    stage_fd: int | None = None
    stage_name: str | None = None
    closed: list[str] = []

    def capture_stage(parent_fd: int, stage: publication._StageOwnership, *, exact_name: str | None) -> None:
        nonlocal stage_fd, stage_name
        real_create(parent_fd, stage, exact_name=exact_name)
        stage_fd, stage_name = stage.descriptor, stage.name

    def mixed_close(descriptor: int) -> None:
        role = "stage" if descriptor == stage_fd else "target" if os.fstat(descriptor).st_ino == target_inode else None
        real_close(descriptor)
        if role is not None:
            closed.append(role)
            if role in failures:
                raise failures[role]

    def admission_close(admission: PublicationStageAdmission) -> BaseException | None:
        assert real_admission_close(admission) is None
        closed.append("admission")
        return failures.get("admission")

    def refuse_cleanup(path: str, *args, **kwargs) -> None:
        if (remaining == "stage" and path == stage_name) or (remaining == "record" and path == "publication-receipt"):
            raise OSError(errno.EIO, "cleanup")
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_create_stage", capture_stage)
        patch.setattr(os, "close", mixed_close)
        patch.setattr(PublicationStageAdmission, "close", admission_close)
        patch.setattr(os, "unlink", refuse_cleanup)
        with pytest.raises(BaseException) as raised:
            publish_file(
                parent, "target", ScratchFileSource(parent, ready), condition=Replace(), create_metadata=_metadata()
            )
    assert raised.value is interrupt
    assert closed == ["target", "stage", "admission"]
    fact = raised.value.__cause__
    assert isinstance(fact, FilePublicationError) and fact.kind is PublicationFailureKind.UNCERTAIN
    assert target.read_bytes() == b"new"
    debt = fact.cleanup_debt
    if remaining == "none":
        assert debt is None
        assert stage_name is not None and not (tmp_path / stage_name).exists()
    else:
        assert isinstance(debt, PublicationStageCleanupDebt)
        assert debt._stage_removed is (remaining == "record")
        assert (tmp_path / debt.name).exists() is (remaining == "stage")
        assert len(list(tmp_path.rglob("publication-receipt"))) == 1
        cleanup_publication_stage(parent, parent, debt)
    assert not list(tmp_path.rglob("publication-receipt"))
    cleanup_scratch(parent, ready)
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("close_is_control", [False, True])
def test_content_control_survives_later_close_failure(
    tmp_path: Path,
    parent: int,
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
    close_is_control: bool,
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    target_inode = target.stat().st_ino
    interrupt = control_type()
    later = SystemExit() if control_type is KeyboardInterrupt else KeyboardInterrupt()
    real_write, real_close = os.write, os.close

    def interrupt_write(descriptor: int, data: bytes) -> int:
        written = real_write(descriptor, data)
        if os.fstat(descriptor).st_ino == target_inode:
            raise interrupt
        return written

    def fail_close(descriptor: int) -> None:
        is_target = os.fstat(descriptor).st_ino == target_inode
        real_close(descriptor)
        if is_target:
            raise later if close_is_control else OSError(errno.EIO, "close")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", interrupt_write)
        patch.setattr(os, "close", fail_close)
        with pytest.raises(BaseException) as raised:
            publish_file(parent, "target", b"new", condition=Replace(), create_metadata=_metadata())
    assert raised.value is interrupt
    fact = raised.value.__cause__
    assert isinstance(fact, FilePublicationError) and fact.kind is PublicationFailureKind.UNCERTAIN
    assert fact.cleanup_debt is None
    assert target.read_bytes() == b"new" and list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_close_control_survives_later_cleanup_control(
    tmp_path: Path,
    parent: int,
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    target_inode = target.stat().st_ino
    ready = ready_scratch(parent, bytes(range(16)), b"new")
    interrupt = control_type()
    later = SystemExit() if control_type is KeyboardInterrupt else KeyboardInterrupt()
    real_close, real_unlink = os.close, os.unlink
    cleanup_attempts = 0

    def interrupt_close(descriptor: int) -> None:
        is_target = os.fstat(descriptor).st_ino == target_inode
        real_close(descriptor)
        if is_target:
            raise interrupt

    def interrupt_cleanup(path: str, *args, **kwargs) -> None:
        nonlocal cleanup_attempts
        if path.startswith(".agentworks-stage-"):
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise OSError(errno.EIO, "cleanup")
            raise later
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "close", interrupt_close)
        patch.setattr(os, "unlink", interrupt_cleanup)
        with pytest.raises(BaseException) as raised:
            publish_file(
                parent, "target", ScratchFileSource(parent, ready), condition=Replace(), create_metadata=_metadata()
            )
    assert raised.value is interrupt and cleanup_attempts == 2
    fact = raised.value.__cause__
    assert isinstance(fact, FilePublicationError) and fact.kind is PublicationFailureKind.UNCERTAIN
    debt = fact.cleanup_debt
    assert isinstance(debt, PublicationStageCleanupDebt) and not debt._stage_removed
    assert target.read_bytes() == b"new" and (tmp_path / debt.name).exists()
    cleanup_publication_stage(parent, parent, debt)
    cleanup_scratch(parent, ready)
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("final_close_failure", ["none", "io", "control"])
@pytest.mark.parametrize(
    "interrupt_at,remaining",
    [("stage", "none"), ("record", "none"), ("stage", "stage"), ("stage", "record"), ("record", "record")],
)
def test_final_cleanup_reduces_interrupted_publication_to_latest_debt(
    tmp_path: Path,
    parent: int,
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
    final_close_failure: str,
    interrupt_at: str,
    remaining: str,
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    ready = ready_scratch(parent, bytes(range(16)), b"new")
    scratch_inode = next(path for path in tmp_path.iterdir() if path.is_dir()).stat().st_ino
    interrupt = control_type()
    later = SystemExit() if control_type is KeyboardInterrupt else KeyboardInterrupt()
    real_unlink, real_close = os.unlink, os.close
    attempts = 0
    final_closed: list[int] = []

    def interrupt_then_cleanup(path: str, *args, **kwargs) -> None:
        nonlocal attempts
        artifact = (
            "stage" if path.startswith(".agentworks-stage-") else "record" if path == "publication-receipt" else None
        )
        if artifact == interrupt_at:
            attempts += 1
            if attempts == 1:
                raise interrupt
        if artifact == remaining:
            raise OSError(errno.EIO, "final cleanup")
        real_unlink(path, *args, **kwargs)

    def fail_final_cleanup_close(descriptor: int) -> None:
        is_scratch = os.fstat(descriptor).st_ino == scratch_inode
        real_close(descriptor)
        if is_scratch and attempts == 2:
            final_closed.append(descriptor)
            if final_close_failure == "control":
                raise later
            if final_close_failure == "io":
                raise OSError(errno.EIO, "final cleanup close")

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", interrupt_then_cleanup)
        patch.setattr(os, "close", fail_final_cleanup_close)
        with pytest.raises(BaseException) as raised:
            publish_file(
                parent, "target", ScratchFileSource(parent, ready), condition=Replace(), create_metadata=_metadata()
            )
    assert raised.value is interrupt and attempts == 2
    assert len(final_closed) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(final_closed[0])
    assert closed.value.errno == errno.EBADF
    fact = raised.value.__cause__
    assert isinstance(fact, FilePublicationError)
    assert fact.kind is PublicationFailureKind.UNCERTAIN and fact.phase is PublicationPhase.PUBLICATION
    assert target.read_bytes() == b"new"
    debt = fact.cleanup_debt
    if remaining == "none":
        assert not list(tmp_path.glob(".agentworks-stage-*"))
        assert not list(tmp_path.rglob("publication-receipt"))
        assert debt is None
    else:
        assert isinstance(debt, PublicationStageCleanupDebt)
        assert debt._stage_removed is (remaining == "record")
        stage = tmp_path / debt.name
        assert stage.exists() is (remaining == "stage")
        if stage.exists():
            observed = stage.stat()
            assert (observed.st_dev, observed.st_ino) == (debt._ownership._stage.device, debt._ownership._stage.inode)
        records = list(tmp_path.rglob("publication-receipt"))
        assert len(records) == 1
        record = records[0].stat()
        assert (record.st_dev, record.st_ino) == (debt._ownership._record.device, debt._ownership._record.inode)
        cleanup_publication_stage(parent, parent, debt)
    cleanup_scratch(parent, ready)
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("cleanup_failure", ["none", "io", "control"])
def test_final_cleanup_reduces_unrecorded_stage_debt(
    tmp_path: Path,
    parent: int,
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
    cleanup_failure: str,
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    interrupt = control_type()
    later = SystemExit() if control_type is KeyboardInterrupt else KeyboardInterrupt()
    real_unlink = os.unlink
    attempts = 0

    def interrupt_then_cleanup(path: str, *args, **kwargs) -> None:
        nonlocal attempts
        if path.startswith(".agentworks-stage-"):
            attempts += 1
            if attempts == 1:
                raise interrupt
            if cleanup_failure == "control":
                raise later
            if cleanup_failure == "io":
                raise OSError(errno.EIO, "final cleanup")
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", interrupt_then_cleanup)
        with pytest.raises(BaseException) as raised:
            publish_file(parent, "target", b"new", condition=Replace(), create_metadata=_metadata())
    assert raised.value is interrupt and attempts == 2
    fact = raised.value.__cause__
    assert isinstance(fact, FilePublicationError)
    assert fact.kind is PublicationFailureKind.UNCERTAIN and fact.phase is PublicationPhase.PUBLICATION
    assert target.read_bytes() == b"new"
    debt = fact.cleanup_debt
    if cleanup_failure == "none":
        assert list(tmp_path.iterdir()) == [target]
        assert debt is None
    else:
        assert isinstance(debt, publication.PublicationCleanupDebt)
        observed = (tmp_path / debt.name).stat()
        assert (observed.st_dev, observed.st_ino) == (debt.device, debt.inode)
        assert publication.retry_publication_cleanup(parent, debt)
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
