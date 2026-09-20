"""Linux atomic publication from a borrowed trusted parent descriptor."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import agentworks.execution._file_publication as publication_module
from agentworks.execution._file_publication import (
    CreateMetadata,
    FilePublicationError,
    PublicationFailureKind,
    PublicationPhase,
    publish_file,
    retry_publication_cleanup,
)
from agentworks.execution._file_snapshot import FileSnapshot, read_snapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux file publication")


def _open_parent(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _metadata(mode: int = 0o600, *, uid: int | None = None, gid: int | None = None) -> CreateMetadata:
    return CreateMetadata(os.getuid() if uid is None else uid, os.getgid() if gid is None else gid, mode)


def _snapshot(parent_fd: int, name: str) -> FileSnapshot:
    result = read_snapshot(parent_fd, name, 1024 * 1024)
    assert result is not None
    return result


def _publish_replace(parent_fd: int, name: str, content: bytes, expected: FileSnapshot) -> None:
    publish_file(parent_fd, name, content, expected=expected, create_metadata=_metadata())


def _failure(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    expected: FileSnapshot | None,
    create_metadata: CreateMetadata | None = None,
) -> FilePublicationError:
    with pytest.raises(FilePublicationError) as raised:
        publish_file(
            parent_fd,
            name,
            content,
            expected=expected,
            create_metadata=create_metadata or _metadata(),
        )
    return raised.value


def _stage_names(path: Path) -> list[Path]:
    return list(path.glob(f"{publication_module._STAGE_PREFIX}*"))


def _publication_cause(error: BaseException) -> FilePublicationError:
    assert isinstance(error.__cause__, FilePublicationError)
    return error.__cause__


def test_create_only_publishes_all_bytes_with_requested_metadata(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    content = os.urandom(192 * 1024 + 17)
    try:
        publish_file(parent_fd, "new", content, expected=None, create_metadata=_metadata(0o640))
        os.fstat(parent_fd)
    finally:
        os.close(parent_fd)

    target = tmp_path / "new"
    observed = target.stat()
    assert target.read_bytes() == content
    assert (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)) == (os.getuid(), os.getgid(), 0o640)
    assert not _stage_names(tmp_path)


def test_non_linux_platform_refuses_before_parent_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    os.close(parent_fd)
    monkeypatch.setattr("agentworks.execution._file_publication.sys.platform", "darwin")
    error = _failure(parent_fd, "target", b"content", expected=None)
    assert error.kind is PublicationFailureKind.UNSUPPORTED
    assert error.phase is PublicationPhase.STAGING


def test_create_uses_exclusive_nofollow_0600_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    original_open = os.open
    staging_calls: list[tuple[int, int]] = []

    def recording_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path.startswith(publication_module._STAGE_PREFIX):
            staging_calls.append((flags, mode))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)
    try:
        publish_file(parent_fd, "new", b"content", expected=None, create_metadata=_metadata())
    finally:
        os.close(parent_fd)

    assert len(staging_calls) == 1
    flags, mode = staging_calls[0]
    assert flags & (os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW) == os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    assert mode == 0o600


def test_create_retains_default_acl_as_direct_create_would(tmp_path: Path) -> None:
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        pytest.skip("setfacl is required to provision the ACL fixture")
    group_name = str(os.getgid())
    configured = subprocess.run(
        [setfacl, "-d", "-m", f"u::rwx,g::r-x,g:{group_name}:rw-,m::rwx,o::---", str(tmp_path)],
        check=False,
        capture_output=True,
    )
    if configured.returncode != 0:
        pytest.skip("the test filesystem cannot establish the default ACL fixture")

    parent_fd = _open_parent(tmp_path)
    control_fd = os.open("control", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640, dir_fd=parent_fd)
    os.close(control_fd)
    control_acl = os.getxattr(tmp_path / "control", "system.posix_acl_access")
    try:
        publish_file(parent_fd, "published", b"content", expected=None, create_metadata=_metadata(0o640))
    finally:
        os.close(parent_fd)

    assert os.getxattr(tmp_path / "published", "system.posix_acl_access") == control_acl
    assert stat.S_IMODE((tmp_path / "published").stat().st_mode) == 0o640


def test_replace_matching_snapshot_preserves_access_profile(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    target.chmod(0o640)
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        pytest.skip("setfacl is required to provision the ACL fixture")
    configured = subprocess.run([setfacl, "-m", f"g:{os.getgid()}:r--", str(target)], check=False, capture_output=True)
    if configured.returncode != 0:
        pytest.skip("the test filesystem cannot establish the access ACL fixture")
    before = target.stat()
    acl = os.getxattr(target, "system.posix_acl_access")
    parent_fd = _open_parent(tmp_path)
    try:
        expected = _snapshot(parent_fd, "target")
        _publish_replace(parent_fd, "target", b"new bytes", expected)
    finally:
        os.close(parent_fd)

    after = target.stat()
    assert target.read_bytes() == b"new bytes"
    assert after.st_ino != before.st_ino
    assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
    )
    assert os.getxattr(target, "system.posix_acl_access") == acl


def test_replace_removes_staging_inherited_acl_when_existing_file_has_none(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    with pytest.raises(OSError) as absent:
        os.getxattr(target, "system.posix_acl_access")
    assert absent.value.errno in {errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)}
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        pytest.skip("setfacl is required to provision the ACL fixture")
    configured = subprocess.run(
        [setfacl, "-d", "-m", f"u::rwx,g::r-x,g:{os.getgid()}:rw-,m::rwx,o::---", str(tmp_path)],
        check=False,
        capture_output=True,
    )
    if configured.returncode != 0:
        pytest.skip("the test filesystem cannot establish the default ACL fixture")

    parent_fd = _open_parent(tmp_path)
    try:
        _publish_replace(parent_fd, "target", b"new", _snapshot(parent_fd, "target"))
    finally:
        os.close(parent_fd)
    with pytest.raises(OSError) as still_absent:
        os.getxattr(target, "system.posix_acl_access")
    assert still_absent.value.errno in {errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)}


def test_create_conflict_keeps_existing_inode_and_bytes(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"existing")
    before = target.stat()
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(parent_fd, "target", b"replacement", expected=None)
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.CONFLICT
    assert error.phase is PublicationPhase.PUBLICATION
    assert target.read_bytes() == b"existing"
    assert target.stat().st_ino == before.st_ino
    assert not _stage_names(tmp_path)


def test_create_refuses_when_renameat2_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    monkeypatch.setattr("agentworks.execution._file_publication.ctypes.CDLL", lambda *_args, **_kwargs: object())
    try:
        error = _failure(parent_fd, "target", b"content", expected=None)
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.UNSUPPORTED
    assert error.phase is PublicationPhase.PUBLICATION
    assert not (tmp_path / "target").exists()
    assert not _stage_names(tmp_path)


def test_stale_snapshot_conflicts_without_replacing_newer_inode(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"first")
    parent_fd = _open_parent(tmp_path)
    try:
        stale = _snapshot(parent_fd, "target")
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"second")
        os.replace(replacement, target)
        newer = target.stat()
        error = _failure(parent_fd, "target", b"third", expected=stale)
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.CONFLICT
    assert target.read_bytes() == b"second"
    assert target.stat().st_ino == newer.st_ino
    assert not _stage_names(tmp_path)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_replacement_refuses_links_and_special_objects(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    parent_fd = _open_parent(tmp_path)
    try:
        expected = _snapshot(parent_fd, "target")
        if kind == "symlink":
            other = tmp_path / "other"
            other.write_bytes(b"other")
            target.unlink()
            target.symlink_to(other)
        elif kind == "hardlink":
            os.link(target, tmp_path / "other-name")
        elif kind == "fifo":
            target.unlink()
            os.mkfifo(target)
        else:
            target.unlink()
            target.mkdir()
        error = _failure(parent_fd, "target", b"new", expected=expected)
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.UNSUPPORTED
    assert not _stage_names(tmp_path)


def test_observed_fifo_is_refused_before_writable_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    parent_fd = _open_parent(tmp_path)
    expected = _snapshot(parent_fd, "target")
    target.unlink()
    os.mkfifo(target)
    original_open = os.open
    writable_leaf_opened = False

    def recording_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal writable_leaf_opened
        if path == "target" and flags & os.O_RDWR:
            writable_leaf_opened = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    try:
        error = _failure(parent_fd, "target", b"new", expected=expected)
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.UNSUPPORTED
    assert not writable_leaf_opened


def test_read_only_destination_requires_ordinary_write_authority(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses ordinary mode-bit write checks")
    target = tmp_path / "target"
    target.write_bytes(b"old")
    target.chmod(0o400)
    before = target.stat()
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(parent_fd, "target", b"new", expected=_snapshot(parent_fd, "target"))
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.WRITE_AUTHORITY
    assert target.read_bytes() == b"old"
    assert target.stat().st_ino == before.st_ino


def test_unavailable_create_ownership_refuses_before_publication(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can establish the deliberately unavailable owner fixture")
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(
            parent_fd,
            "target",
            b"content",
            expected=None,
            create_metadata=_metadata(uid=os.getuid() + 1),
        )
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.METADATA
    assert not (tmp_path / "target").exists()
    assert not _stage_names(tmp_path)


def test_set_id_destination_is_refused_before_replacement(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    target.chmod(0o4644)
    if not target.stat().st_mode & stat.S_ISUID:
        pytest.skip("the test filesystem does not retain set-ID on the fixture")
    before = target.stat()
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(parent_fd, "target", b"new", expected=_snapshot(parent_fd, "target"))
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.METADATA
    assert target.read_bytes() == b"old"
    assert target.stat().st_ino == before.st_ino


def test_unknown_extended_metadata_is_refused_without_blind_copy(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    try:
        os.setxattr(target, "user.agentworks-publication-test", b"required")
    except OSError:
        pytest.skip("the test filesystem cannot establish a user xattr fixture")
    before = target.stat()
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(parent_fd, "target", b"new", expected=_snapshot(parent_fd, "target"))
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.METADATA
    assert target.read_bytes() == b"old"
    assert target.stat().st_ino == before.st_ino
    assert os.getxattr(target, "user.agentworks-publication-test") == b"required"


def test_partial_write_failure_leaves_old_inode_and_cleans_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    before = target.stat()
    parent_fd = _open_parent(tmp_path)
    original_write = publication_module._write
    calls = 0

    def failing_write(descriptor: int, content: memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, content[:3])
        raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONTENT)

    monkeypatch.setattr(publication_module, "_write", failing_write)
    try:
        error = _failure(parent_fd, "target", b"replacement", expected=_snapshot(parent_fd, "target"))
        os.fstat(parent_fd)
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.IO
    assert error.phase is PublicationPhase.CONTENT
    assert error.cleanup_debt is None
    assert target.read_bytes() == b"old"
    assert target.stat().st_ino == before.st_ino
    assert not _stage_names(tmp_path)


def test_metadata_failure_leaves_old_inode_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    before = target.stat()
    parent_fd = _open_parent(tmp_path)

    def fail_mode(_descriptor: int, _mode: int) -> None:
        raise FilePublicationError(PublicationFailureKind.METADATA, PublicationPhase.METADATA)

    monkeypatch.setattr(os, "fchmod", fail_mode)
    try:
        error = _failure(parent_fd, "target", b"new", expected=_snapshot(parent_fd, "target"))
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.METADATA
    assert target.read_bytes() == b"old"
    assert target.stat().st_ino == before.st_ino
    assert not _stage_names(tmp_path)


def test_cleanup_failure_is_bounded_and_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    original_unlink = os.unlink

    def deny_stage_unlink(path: str, *, dir_fd: int | None = None) -> None:
        if path.startswith(publication_module._STAGE_PREFIX):
            raise OSError(errno.EIO, "fixture cleanup failure")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", deny_stage_unlink)
    monkeypatch.setattr(
        publication_module,
        "_write",
        lambda *_args: (_ for _ in ()).throw(FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONTENT)),
    )
    error = _failure(parent_fd, "target", b"content", expected=None)
    debt = _stage_names(tmp_path)
    assert error.kind is PublicationFailureKind.IO
    assert error.cleanup_debt is not None
    assert len(debt) == 1
    assert (error.cleanup_debt.device, error.cleanup_debt.inode) == (debt[0].stat().st_dev, debt[0].stat().st_ino)
    assert debt[0].name not in repr(error.cleanup_debt)
    monkeypatch.setattr(os, "unlink", original_unlink)
    assert retry_publication_cleanup(parent_fd, error.cleanup_debt)
    os.close(parent_fd)
    assert not debt[0].exists()


def test_unidentified_stage_debt_retains_exact_name_but_refuses_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_fstat = os.fstat

    def fail_regular_fstat(descriptor: int) -> os.stat_result:
        observed = original_fstat(descriptor)
        if stat.S_ISREG(observed.st_mode):
            raise OSError(errno.EIO, "fixture stage identity failure")
        return observed

    monkeypatch.setattr(os, "fstat", fail_regular_fstat)
    error = _failure(parent_fd, "target", b"content", expected=None)
    debt = error.cleanup_debt
    assert debt is not None and debt.device is None and debt.inode is None
    assert debt.name not in repr(debt)
    assert not retry_publication_cleanup(parent_fd, debt)
    os.close(parent_fd)
    (tmp_path / debt.name).unlink()


def test_stage_identity_interrupt_releases_fd_and_carries_unknown_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_fstat = os.fstat
    stage_fd: int | None = None

    def interrupt_regular_fstat(descriptor: int) -> os.stat_result:
        nonlocal stage_fd
        observed = original_fstat(descriptor)
        if stat.S_ISREG(observed.st_mode):
            stage_fd = descriptor
            raise KeyboardInterrupt
        return observed

    monkeypatch.setattr(os, "fstat", interrupt_regular_fstat)
    with pytest.raises(KeyboardInterrupt) as raised:
        publish_file(parent_fd, "target", b"content", expected=None, create_metadata=_metadata())
    cause = _publication_cause(raised.value)
    assert cause.phase is PublicationPhase.CLEANUP
    assert cause.cleanup_debt is not None and cause.cleanup_debt.device is None
    monkeypatch.setattr(os, "fstat", original_fstat)
    assert stage_fd is not None
    with pytest.raises(OSError):
        os.fstat(stage_fd)
    os.close(parent_fd)
    (tmp_path / cause.cleanup_debt.name).unlink()


def test_second_cleanup_interrupt_escapes_with_known_debt_and_closed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_fd = _open_parent(tmp_path)
    stage_fd: int | None = None

    def interrupt_write(descriptor: int, _content: memoryview) -> int:
        nonlocal stage_fd
        stage_fd = descriptor
        raise KeyboardInterrupt

    original_stat = os.stat

    def interrupt_cleanup(path: str, *args: object, **kwargs: object) -> os.stat_result:
        if path.startswith(publication_module._STAGE_PREFIX):
            raise SystemExit
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(publication_module, "_write", interrupt_write)
    monkeypatch.setattr(os, "stat", interrupt_cleanup)
    with pytest.raises(SystemExit) as raised:
        publish_file(parent_fd, "target", b"content", expected=None, create_metadata=_metadata())
    cause = _publication_cause(raised.value)
    debt = cause.cleanup_debt
    assert cause.phase is PublicationPhase.CLEANUP
    assert debt is not None and debt.device is not None and debt.inode is not None
    assert stage_fd is not None
    with pytest.raises(OSError):
        os.fstat(stage_fd)
    monkeypatch.setattr(os, "stat", original_stat)
    assert retry_publication_cleanup(parent_fd, debt)
    os.close(parent_fd)


def test_cleanup_never_unlinks_a_replaced_stage_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    saved_stage = tmp_path / "saved-stage"

    def substitute_stage(_descriptor: int, _content: memoryview) -> int:
        stage = _stage_names(tmp_path)
        assert len(stage) == 1
        stage[0].rename(saved_stage)
        stage[0].write_bytes(b"not owned")
        raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONTENT)

    monkeypatch.setattr(publication_module, "_write", substitute_stage)
    error = _failure(parent_fd, "target", b"content", expected=None)
    assert error.cleanup_debt is not None
    assert not retry_publication_cleanup(parent_fd, error.cleanup_debt)
    os.close(parent_fd)
    impostors = _stage_names(tmp_path)
    assert len(impostors) == 1
    assert impostors[0].read_bytes() == b"not owned"
    impostors[0].unlink()
    saved_stage.unlink()


def test_post_rename_failure_reports_uncertainty_without_blind_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    parent_fd = _open_parent(tmp_path)
    expected = _snapshot(parent_fd, "target")
    original_rename = os.rename

    def rename_then_fail(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        original_rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        raise OSError(errno.EIO, "fixture lost acknowledgement")

    monkeypatch.setattr(os, "rename", rename_then_fail)
    try:
        error = _failure(parent_fd, "target", b"new", expected=expected)
    finally:
        os.close(parent_fd)
    assert error.kind is PublicationFailureKind.UNCERTAIN
    assert error.phase is PublicationPhase.PUBLICATION
    assert error.cleanup_debt is None
    assert target.read_bytes() == b"new"
    assert not _stage_names(tmp_path)


def test_post_rename_interrupt_preserves_control_type_with_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    parent_fd = _open_parent(tmp_path)
    expected = _snapshot(parent_fd, "target")
    original_rename = os.rename

    def rename_then_interrupt(*args: object, **kwargs: object) -> None:
        original_rename(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "rename", rename_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        _publish_replace(parent_fd, "target", b"new", expected)
    os.close(parent_fd)
    cause = _publication_cause(raised.value)
    assert cause.kind is PublicationFailureKind.UNCERTAIN
    assert cause.phase is PublicationPhase.PUBLICATION
    assert cause.cleanup_debt is None
    assert target.read_bytes() == b"new"


def test_interrupt_after_successful_rename_during_close_retains_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    parent_fd = _open_parent(tmp_path)
    expected = _snapshot(parent_fd, "target")
    original_close = publication_module._close
    close_count = 0

    def close_then_interrupt(descriptor: int) -> None:
        nonlocal close_count
        close_count += 1
        original_close(descriptor)
        if close_count == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(publication_module, "_close", close_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        _publish_replace(parent_fd, "target", b"new", expected)
    os.close(parent_fd)
    cause = _publication_cause(raised.value)
    assert cause.kind is PublicationFailureKind.UNCERTAIN
    assert cause.phase is PublicationPhase.PUBLICATION
    assert cause.cleanup_debt is None
    assert close_count == 2
    assert target.read_bytes() == b"new"


def test_errors_have_fixed_secret_free_exception_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_name = "private-target-name"
    parent_fd = _open_parent(tmp_path)
    monkeypatch.setattr(
        publication_module,
        "_write",
        lambda *_args: (_ for _ in ()).throw(FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONTENT)),
    )
    try:
        error = _failure(parent_fd, secret_name, b"private-content", expected=None)
    finally:
        os.close(parent_fd)
    assert error.args == (PublicationFailureKind.IO.value, PublicationPhase.CONTENT.value, False)
    assert secret_name not in repr(error)
    assert "private-content" not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_values_are_frozen_and_inputs_reject_before_io(tmp_path: Path) -> None:
    metadata = _metadata()
    with pytest.raises(FrozenInstanceError):
        metadata.mode = 0o777  # type: ignore[misc]
    parent_fd = _open_parent(tmp_path)
    os.close(parent_fd)
    for name in ("", ".", "..", "nested/name", "nul\x00name"):
        with pytest.raises(ValueError):
            publish_file(parent_fd, name, b"content", expected=None, create_metadata=metadata)
    with pytest.raises(ValueError):
        publish_file(parent_fd, "target", bytearray(b"content"), expected=None, create_metadata=metadata)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CreateMetadata(os.getuid(), os.getgid(), 0o4755)
